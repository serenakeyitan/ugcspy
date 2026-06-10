import { describe, expect, test } from "bun:test";
import type { BreakoutCandidate } from "../src/lib/breakout.ts";
import { formatAlert, postBreakoutAlert } from "../src/lib/slack.ts";
import type { Competitor, VideoRecord } from "../src/types.ts";

const competitor: Competitor = { id: 1, handle: "@x", platform: "tiktok", added_at: "" };

const video: VideoRecord = {
  id: 1,
  competitor_id: 1,
  platform: "tiktok",
  external_id: "v1",
  posted_at: "2026-06-01T00:00:00.000Z",
  fetched_at: "2026-06-01T00:00:00.000Z",
  caption: "purple colours #befreed",
  thumbnail_url: "",
  video_url: "https://www.tiktok.com/@x/video/1",
  view_count: 1000,
  like_count: 10,
  comment_count: 1,
  share_count: 0,
  hook_source: "caption",
  hook_text: "purple colours #befreed",
  hook_confidence: 1,
  format_tag: null,
  raw_metrics_json: "{}",
};

const candidate: BreakoutCandidate = { video, ratio: 4.2, threshold: 500 };

describe("postBreakoutAlert failure handling", () => {
  test("an unreachable webhook returns a failed result instead of throwing", async () => {
    // 127.0.0.1:9 (discard) refuses connections immediately — no live network.
    // The daemon relies on this contract: one watch's dead webhook must not
    // abort the alerts/watches after it.
    const r = await postBreakoutAlert("http://127.0.0.1:9/hook", competitor, candidate);
    expect(r.ok).toBe(false);
    expect(r.status).toBe(0);
    expect(r.body.length).toBeGreaterThan(0);
  });
});

describe("formatAlert", () => {
  test("includes handle, ratio, and the video URL", () => {
    const text = formatAlert(competitor, candidate);
    expect(text).toContain("@x");
    expect(text).toContain("4.2x");
    expect(text).toContain(video.video_url);
  });
});
