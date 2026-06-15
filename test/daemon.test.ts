import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { claimAlert } from "../src/commands/daemon.ts";
import { detectThresholdCrossings } from "../src/lib/breakout.ts";
import { migrate } from "../src/db/schema.ts";
import type { VideoRecord } from "../src/types.ts";

function seededDb(): { db: Database; videoId: number; watchId: number } {
  const db = new Database(":memory:");
  migrate(db);
  const competitorId = (
    db
      .prepare(`INSERT INTO competitors (handle, platform) VALUES ('@x','tiktok') RETURNING id`)
      .get() as { id: number }
  ).id;
  const videoId = (
    db
      .prepare(
        `INSERT INTO videos (competitor_id, platform, external_id, posted_at)
         VALUES (?, 'tiktok', 'v1', '2026-06-01T00:00:00.000Z') RETURNING id`,
      )
      .get(competitorId) as { id: number }
  ).id;
  const watchId = (
    db
      .prepare(
        `INSERT INTO watches (competitor_id, slack_webhook_url) VALUES (?, 'https://h/x') RETURNING id`,
      )
      .get(competitorId) as { id: number }
  ).id;
  return { db, videoId, watchId };
}

describe("claimAlert (claim-before-send dedupe, at-most-once)", () => {
  test("first claim wins; every repeat claim for the same (video, watch) loses", () => {
    const { db, videoId, watchId } = seededDb();
    expect(claimAlert(db, videoId, watchId)).toBe(true);
    expect(claimAlert(db, videoId, watchId)).toBe(false);
    expect(claimAlert(db, videoId, watchId)).toBe(false);
    expect(
      (db.prepare(`SELECT COUNT(*) n FROM alerts_fired`).get() as { n: number }).n,
    ).toBe(1);
  });

  test("claims are scoped per watch — a second watch can still claim the same video", () => {
    const { db, videoId, watchId } = seededDb();
    const watch2 = (
      db
        .prepare(
          `SELECT competitor_id FROM watches WHERE id = ?`,
        )
        .get(watchId) as { competitor_id: number }
    ).competitor_id;
    const watchId2 = (
      db
        .prepare(
          `INSERT INTO watches (competitor_id, slack_webhook_url) VALUES (?, 'https://h/y') RETURNING id`,
        )
        .get(watch2) as { id: number }
    ).id;
    expect(claimAlert(db, videoId, watchId)).toBe(true);
    expect(claimAlert(db, videoId, watchId2)).toBe(true);
  });
});

describe("Instagram view-threshold alert path (daemon, platform-agnostic)", () => {
  test("an instagram video over the bar is detected and claims exactly once", () => {
    const db = new Database(":memory:");
    migrate(db);
    const cid = (
      db
        .prepare(`INSERT INTO competitors (handle, platform) VALUES ('@nike','instagram') RETURNING id`)
        .get() as { id: number }
    ).id;
    // Two IG videos: one over 1M views, one under.
    db.prepare(
      `INSERT INTO videos (competitor_id, platform, external_id, posted_at, view_count, video_url)
       VALUES (?, 'instagram', 'DZbig', '2026-06-10T00:00:00.000Z', 12700000, 'https://www.instagram.com/reel/DZbig/'),
              (?, 'instagram', 'DZsmall', '2026-06-10T00:00:00.000Z', 500000, 'https://www.instagram.com/reel/DZsmall/')`,
    ).run(cid, cid);
    const watchId = (
      db
        .prepare(
          `INSERT INTO watches (competitor_id, slack_webhook_url, view_threshold, remix_brand)
           VALUES (?, 'https://h/x', 1000000, 'BeFreed') RETURNING id`,
        )
        .get(cid) as { id: number }
    ).id;

    // The daemon's exact threshold call: scan ALL tracked, detect crossings.
    const tracked = db
      .prepare(`SELECT * FROM videos WHERE competitor_id = ?`)
      .all(cid) as VideoRecord[];
    const crossings = detectThresholdCrossings(tracked, 1_000_000);
    // Only the 12.7M video crosses; the 500K one does not.
    expect(crossings).toHaveLength(1);
    expect(crossings[0]!.video.view_count).toBe(12_700_000);

    // At-most-once: claim the crossing, repeat loses.
    expect(claimAlert(db, crossings[0]!.video.id, watchId)).toBe(true);
    expect(claimAlert(db, crossings[0]!.video.id, watchId)).toBe(false);
  });
});
