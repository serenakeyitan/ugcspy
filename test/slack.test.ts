import { describe, expect, test } from "bun:test";
import type { BreakoutCandidate } from "../src/lib/breakout.ts";
import {
  formatAlert,
  formatThresholdReminder,
  postBreakoutAlert,
  postThresholdReminder,
} from "../src/lib/slack.ts";
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

describe("formatThresholdReminder (absolute-threshold reminder with remix CTA)", () => {
  const crossing: BreakoutCandidate = { video, ratio: 3, threshold: 100_000 };

  test("leads with the crossed-views milestone and the video link", () => {
    const text = formatThresholdReminder(competitor, crossing, null);
    expect(text).toContain("@x");
    expect(text).toContain("100,000");
    expect(text).toContain(video.video_url);
  });

  test("with a remix brand, emits the ready /ugcspy-rebrand command using the DB id", () => {
    const text = formatThresholdReminder(competitor, crossing, "BeFreed");
    expect(text).toContain("/ugcspy-rebrand 1 BeFreed"); // video.id = 1
    expect(text).toContain("BeFreed");
  });

  test("without a remix brand, the CTA carries a <your-brand> placeholder", () => {
    const text = formatThresholdReminder(competitor, crossing, null);
    expect(text).toContain("/ugcspy-rebrand 1 <your-brand>");
  });

  test("sanitizes mrkdwn-injection chars in the remix brand (no <!channel>, no backtick break-out)", () => {
    const text = formatThresholdReminder(competitor, crossing, "<!channel> Be`Freed*");
    expect(text).not.toContain("<!channel>");
    expect(text).not.toContain("`Be"); // backtick can't break out of the inline-code CTA
    // the cleaned brand still appears
    expect(text).toContain("/ugcspy-rebrand 1 !channel BeFreed");
  });

  test("the POSTed payload — BOTH the text lead AND the context footer — is sanitized", async () => {
    // Regression: the context block (blocks[1]) interpolated the brand RAW, so a
    // malicious brand's <!channel> survived in the footer even though the lead was
    // clean. Capture the actual POST body and assert the WHOLE payload is inert.
    const orig = globalThis.fetch;
    let captured = "";
    globalThis.fetch = (async (_url: string, init: { body: string }) => {
      captured = init.body;
      return new Response("ok", { status: 200 });
    }) as unknown as typeof fetch;
    try {
      await postThresholdReminder(
        "http://example.test/hook",
        competitor,
        crossing,
        "<!channel> Be`Freed",
      );
    } finally {
      globalThis.fetch = orig;
    }
    const payload = JSON.parse(captured);
    const whole = JSON.stringify(payload);
    // No live channel-broadcast mention anywhere in the payload (lead OR footer).
    expect(whole).not.toContain("<!channel>");
    // The context footer specifically carries the cleaned brand.
    const footer = payload.blocks[1].elements[0].text as string;
    expect(footer).toContain("remix → *!channel BeFreed*");
    expect(footer).not.toContain("<!channel>");
    expect(footer).not.toContain("`");
  });
});
