import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { claimAlert } from "../src/commands/daemon.ts";
import { migrate } from "../src/db/schema.ts";

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
