import { describe, expect, test } from "bun:test";
import { MockProvider } from "../src/providers/mock.ts";

describe("MockProvider", () => {
  test("is deterministic per (handle, platform)", async () => {
    const p = new MockProvider();
    const a = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const b = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    expect(a).toEqual(b);
  });

  test("different handles yield different data", async () => {
    const p = new MockProvider();
    const a = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const b = await p.fetchRecentVideos("@rarebeauty", "tiktok", 30);
    expect(a[0]?.external_id).not.toBe(b[0]?.external_id);
  });

  test("strips leading @ from handle in URL", async () => {
    const p = new MockProvider();
    const videos = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    expect(videos[0]?.video_url).toContain("@glossier/");
    expect(videos[0]?.external_id).not.toContain("@");
  });

  test("returns at least one breakout-shaped video for alert dev", async () => {
    const p = new MockProvider();
    const videos = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const max = Math.max(...videos.map((v) => v.view_count));
    const median = [...videos].sort((a, b) => a.view_count - b.view_count)[
      Math.floor(videos.length / 2)
    ]?.view_count ?? 0;
    expect(max).toBeGreaterThan(median * 2);
  });
});
