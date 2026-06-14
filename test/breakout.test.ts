import { describe, expect, test } from "bun:test";
import {
  detectBreakouts,
  detectThresholdCrossings,
  evaluateWatchState,
  filterRecent24h,
  ONE_DAY_MS,
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

describe("filterRecent24h", () => {
  const NOW = new Date("2026-05-15T12:00:00Z");

  test("includes videos posted in the last 24h", () => {
    const items = [
      { posted_at: new Date(NOW.getTime() - 1 * 3600_000).toISOString() }, // 1h ago
      { posted_at: new Date(NOW.getTime() - 23 * 3600_000).toISOString() }, // 23h ago
    ];
    expect(filterRecent24h(items, NOW)).toHaveLength(2);
  });

  test("excludes videos at 25h, 7 days, 24 days", () => {
    const items = [
      { posted_at: new Date(NOW.getTime() - 25 * 3600_000).toISOString() },
      { posted_at: new Date(NOW.getTime() - 7 * ONE_DAY_MS).toISOString() },
      { posted_at: new Date(NOW.getTime() - 24 * ONE_DAY_MS).toISOString() }, // regression: must NOT include
    ];
    expect(filterRecent24h(items, NOW)).toHaveLength(0);
  });

  test("boundary at exactly 24h is included", () => {
    const items = [{ posted_at: new Date(NOW.getTime() - ONE_DAY_MS).toISOString() }];
    expect(filterRecent24h(items, NOW)).toHaveLength(1);
  });
});

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

  // watches.created_at comes from SQLite datetime('now'): UTC in
  // "YYYY-MM-DD HH:MM:SS" form with no zone marker. JS parses that form as
  // LOCAL time, which skewed the gate by the host's TZ offset (and made a
  // just-created watch report negative age east of UTC).
  describe("SQLite bare-UTC created_at parsing", () => {
    test("exactly 7 UTC days ago is active, regardless of host timezone", () => {
      const status = evaluateWatchState("2026-05-08 12:00:00", 50, NOW);
      expect(status.state).toBe("active");
      expect(status.days_since_added).toBeCloseTo(7, 5);
    });

    test("6d23h ago (UTC) is still warming up with a sane remaining-days reason", () => {
      const status = evaluateWatchState("2026-05-08 13:00:00", 50, NOW);
      expect(status.state).toBe("warming_up");
      expect(status.reason).toBe("1d remaining in warmup");
      expect(status.days_since_added).toBeGreaterThan(0); // never negative
    });

    test("a watch created 'now' has ~0 days_since_added (no TZ-offset skew)", () => {
      const status = evaluateWatchState("2026-05-15 12:00:00", 0, NOW);
      expect(status.days_since_added).toBeCloseTo(0, 5);
      expect(status.reason).toBe("7d remaining in warmup");
    });
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

describe("detectThresholdCrossings (absolute view-milestone alerts)", () => {
  test("fires for every video at or above the threshold, not the ones below", () => {
    const videos = [v(50_000, 1), v(99_999, 2), v(100_000, 3), v(250_000, 4)];
    const out = detectThresholdCrossings(videos, 100_000);
    expect(out.map((c) => c.video.id).sort()).toEqual([1003, 1004]);
  });

  test("ratio is views ÷ threshold (how far past the bar)", () => {
    const out = detectThresholdCrossings([v(300_000, 9)], 100_000);
    expect(out[0]!.ratio).toBe(3);
    expect(out[0]!.threshold).toBe(100_000);
  });

  test("is NOT relative to a baseline and NOT limited to recent — old high-view videos still fire", () => {
    // Unlike detectBreakouts, no median, no 24h filter — a long-past video that's
    // over the bar is a valid crossing (the per-video dedup makes it once-only).
    const old = v(500_000, 5);
    old.posted_at = new Date(Date.now() - 90 * ONE_DAY_MS).toISOString();
    expect(detectThresholdCrossings([old], 100_000).map((c) => c.video.id)).toEqual([1005]);
  });

  test("a non-positive or non-finite threshold yields nothing (guards a NULL/0 binding)", () => {
    expect(detectThresholdCrossings([v(1_000_000, 1)], 0)).toEqual([]);
    expect(detectThresholdCrossings([v(1_000_000, 1)], Number.NaN)).toEqual([]);
  });
});
