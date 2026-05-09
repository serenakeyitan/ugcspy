import { describe, expect, test } from "bun:test";
import {
  detectBreakouts,
  evaluateWatchState,
  trailingMedianViews,
} from "../src/lib/breakout.ts";
import type { VideoRecord } from "../src/types.ts";

function v(view_count: number, idOffset = 0): VideoRecord {
  return {
    id: 1000 + idOffset,
    competitor_id: 1,
    platform: "tiktok",
    external_id: `ext-${idOffset}`,
    posted_at: new Date().toISOString(),
    fetched_at: new Date().toISOString(),
    caption: "",
    thumbnail_url: "",
    video_url: "",
    view_count,
    like_count: 0,
    comment_count: 0,
    share_count: 0,
    hook_source: "none",
    hook_text: "",
    hook_confidence: 0,
    format_tag: null,
    raw_metrics_json: "{}",
  };
}

describe("evaluateWatchState (cold-start gate)", () => {
  const NOW = new Date("2026-05-15T12:00:00Z");

  test("warming up while under 7 days old", () => {
    const created = new Date("2026-05-14T12:00:00Z").toISOString(); // 1 day ago
    const status = evaluateWatchState(created, 50, NOW);
    expect(status.state).toBe("warming_up");
    expect(status.reason).toContain("warmup");
  });

  test("warming up at 7 days but with <5 videos", () => {
    const created = new Date("2026-05-08T12:00:00Z").toISOString(); // 7 days ago
    const status = evaluateWatchState(created, 4, NOW);
    expect(status.state).toBe("warming_up");
    expect(status.reason).toContain("4/5");
  });

  test("active when 7 days AND >=5 videos", () => {
    const created = new Date("2026-05-08T11:00:00Z").toISOString(); // >7 days
    const status = evaluateWatchState(created, 5, NOW);
    expect(status.state).toBe("active");
    expect(status.reason).toBeUndefined();
  });

  test("active well past warmup with many videos", () => {
    const created = new Date("2026-04-01T00:00:00Z").toISOString();
    const status = evaluateWatchState(created, 30, NOW);
    expect(status.state).toBe("active");
  });
});

describe("trailingMedianViews", () => {
  test("returns null below MIN_VIDEOS=5", () => {
    expect(trailingMedianViews([v(100), v(200), v(300), v(400)])).toBeNull();
  });

  test("median of odd-sized window", () => {
    expect(
      trailingMedianViews([v(100), v(200), v(300), v(400), v(500)]),
    ).toBe(300);
  });

  test("median of even-sized window averages middle two", () => {
    expect(
      trailingMedianViews([v(100), v(200), v(300), v(400), v(500), v(600)]),
    ).toBe(350);
  });

  test("unsorted input is sorted internally", () => {
    expect(
      trailingMedianViews([v(500), v(100), v(400), v(200), v(300)]),
    ).toBe(300);
  });
});

describe("detectBreakouts", () => {
  test("flags videos at or above threshold × median", () => {
    // median = 200; 2x threshold = 400
    const baseline = [v(100, 1), v(150, 2), v(200, 3), v(250, 4), v(300, 5)];
    const recent = [v(150, 6), v(450, 7), v(800, 8)];
    const out = detectBreakouts(recent, baseline, 2.0);
    expect(out).toHaveLength(2);
    // Output preserves input order: 450 first, then 800
    expect(out[0]?.video.id).toBe(1007); // 450 views
    expect(out[1]?.video.id).toBe(1008); // 800 views
    expect(out[1]?.ratio).toBe(4); // 800 / 200
    expect(out[0]?.threshold).toBe(400);
  });

  test("returns [] when baseline has too few videos", () => {
    const baseline = [v(100), v(200)];
    const recent = [v(10000)];
    expect(detectBreakouts(recent, baseline, 2.0)).toEqual([]);
  });

  test("returns [] when median is zero", () => {
    const baseline = [v(0), v(0), v(0), v(0), v(0)];
    const recent = [v(1000)];
    expect(detectBreakouts(recent, baseline, 2.0)).toEqual([]);
  });

  test("threshold multiplier is honored", () => {
    const baseline = [v(100, 1), v(100, 2), v(100, 3), v(100, 4), v(100, 5)];
    // median = 100; threshold = 5 → boundary at 500
    const recent = [v(499, 6), v(500, 7), v(1000, 8)];
    const out = detectBreakouts(recent, baseline, 5.0);
    expect(out.map((c) => c.video.id).sort()).toEqual([1007, 1008]);
  });
});
