import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runWatchAdd } from "../src/commands/watch.ts";
import { migrate } from "../src/db/schema.ts";

const WEBHOOK_A = "https://hooks.slack.com/services/T0/B0/aaa";
const WEBHOOK_B = "https://hooks.slack.com/services/T0/B0/bbb";

function freshDb(): Database {
  const db = new Database(":memory:");
  migrate(db);
  return db;
}

describe("runWatchAdd duplicate handling", () => {
  test("re-adding the same handle+webhook updates the threshold instead of duplicating", async () => {
    // Regression: re-running `watch add @x --threshold 3` (the natural way to
    // change a threshold) used to INSERT a second watch — and since alert
    // dedupe is per (video_id, watch_id), every breakout then alerted twice.
    const db = freshDb();
    await runWatchAdd("@x", { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok" }, db);
    await runWatchAdd("@x", { slackWebhook: WEBHOOK_A, threshold: 3, platform: "tiktok" }, db);

    const rows = db
      .prepare(`SELECT threshold_multiplier FROM watches`)
      .all() as { threshold_multiplier: number }[];
    expect(rows).toHaveLength(1);
    expect(rows[0]!.threshold_multiplier).toBe(3);
  });

  test("a different webhook for the same handle is a legit second channel", async () => {
    const db = freshDb();
    await runWatchAdd("@x", { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok" }, db);
    await runWatchAdd("@x", { slackWebhook: WEBHOOK_B, threshold: 2, platform: "tiktok" }, db);
    expect(
      (db.prepare(`SELECT COUNT(*) n FROM watches`).get() as { n: number }).n,
    ).toBe(2);
  });

  test("handles with and without a leading @ are the same watch", async () => {
    const db = freshDb();
    await runWatchAdd("@x", { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok" }, db);
    await runWatchAdd("x", { slackWebhook: WEBHOOK_A, threshold: 4, platform: "tiktok" }, db);
    expect(
      (db.prepare(`SELECT COUNT(*) n FROM watches`).get() as { n: number }).n,
    ).toBe(1);
  });

  test("changing the trigger (mode/threshold) clears stale fired-alert claims", async () => {
    // Otherwise a video that already alerted under the OLD trigger would be
    // permanently suppressed under the re-tuned one and never fire.
    const db = freshDb();
    await runWatchAdd(
      "@x",
      { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok", viewThreshold: 100000 },
      db,
    );
    const wid = (db.prepare(`SELECT id FROM watches`).get() as { id: number }).id;
    // Seed a competitor video + a fired-alert claim for it.
    const cid = (db.prepare(`SELECT competitor_id FROM watches WHERE id=?`).get(wid) as {
      competitor_id: number;
    }).competitor_id;
    db.prepare(
      `INSERT INTO videos (competitor_id,platform,external_id,posted_at,view_count) VALUES (?,?,?,?,?)`,
    ).run(cid, "tiktok", "v1", new Date().toISOString(), 200000);
    const vid = (db.prepare(`SELECT id FROM videos`).get() as { id: number }).id;
    db.prepare(`INSERT INTO alerts_fired (video_id, watch_id) VALUES (?, ?)`).run(vid, wid);
    expect((db.prepare(`SELECT COUNT(*) n FROM alerts_fired`).get() as { n: number }).n).toBe(1);

    // Re-add with a DIFFERENT view-threshold → claims must clear.
    await runWatchAdd(
      "@x",
      { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok", viewThreshold: 50000 },
      db,
    );
    expect((db.prepare(`SELECT COUNT(*) n FROM alerts_fired`).get() as { n: number }).n).toBe(0);
  });

  test("re-adding with the SAME trigger keeps the dedup claims (no needless re-fire)", async () => {
    const db = freshDb();
    await runWatchAdd(
      "@x",
      { slackWebhook: WEBHOOK_A, threshold: 2, platform: "tiktok", viewThreshold: 100000 },
      db,
    );
    const wid = (db.prepare(`SELECT id FROM watches`).get() as { id: number }).id;
    const cid = (db.prepare(`SELECT competitor_id FROM watches WHERE id=?`).get(wid) as {
      competitor_id: number;
    }).competitor_id;
    db.prepare(
      `INSERT INTO videos (competitor_id,platform,external_id,posted_at,view_count) VALUES (?,?,?,?,?)`,
    ).run(cid, "tiktok", "v1", new Date().toISOString(), 200000);
    const vid = (db.prepare(`SELECT id FROM videos`).get() as { id: number }).id;
    db.prepare(`INSERT INTO alerts_fired (video_id, watch_id) VALUES (?, ?)`).run(vid, wid);

    // Re-add with the SAME view-threshold (e.g. just changing remix-brand) → keep claims.
    await runWatchAdd(
      "@x",
      {
        slackWebhook: WEBHOOK_A,
        threshold: 2,
        platform: "tiktok",
        viewThreshold: 100000,
        remixBrand: "BeFreed",
      },
      db,
    );
    expect((db.prepare(`SELECT COUNT(*) n FROM alerts_fired`).get() as { n: number }).n).toBe(1);
  });
});
