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
});
