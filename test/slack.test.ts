import { describe, expect, test } from "bun:test";
import type { BreakoutCandidate } from "../src/lib/breakout.ts";
import {
  escapeMrkdwn,
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

  test("without a remix brand, the CTA carries a [your-brand] placeholder", () => {
    const text = formatThresholdReminder(competitor, crossing, null);
    expect(text).toContain("/ugcspy-rebrand 1 [your-brand]");
  });

  test("escapes mrkdwn in the remix brand so a mention/link can't form (< > & neutralized)", () => {
    const text = formatThresholdReminder(competitor, crossing, "<!channel> Be`Freed");
    // The mention syntax requires a real `<` — escaped to &lt; it's inert text.
    expect(text).not.toContain("<!channel>");
    expect(text).toContain("&lt;!channel&gt;"); // rendered as literal, not a broadcast
    expect(text).not.toContain("`Be"); // backtick can't break out of the inline-code CTA
  });

  test("the POSTed payload — both blocks — is escaped (no live <!channel>)", async () => {
    const orig = globalThis.fetch;
    let captured = "";
    globalThis.fetch = (async (_url: string, init: { body: string }) => {
      captured = init.body;
      return new Response("ok", { status: 200 });
    }) as unknown as typeof fetch;
    try {
      await postThresholdReminder("http://example.test/hook", competitor, crossing, "<!channel> Be`Freed");
    } finally {
      globalThis.fetch = orig;
    }
    const payload = JSON.parse(captured);
    expect(JSON.stringify(payload)).not.toContain("<!channel>"); // nowhere in lead OR footer
    const footer = payload.blocks[1].elements[0].text as string;
    expect(footer).toContain("&lt;!channel&gt;");
    expect(footer).not.toContain("`");
    // Every mrkdwn object must set verbatim:true — the STRUCTURAL fix that stops
    // Slack auto-parsing bare @here/@everyone and bare URLs (which escaping alone
    // doesn't catch). Section + context block both.
    expect((payload.blocks[0].text as { verbatim: boolean }).verbatim).toBe(true);
    expect((payload.blocks[1].elements[0] as { verbatim: boolean }).verbatim).toBe(true);
  });
});

describe("escapeMrkdwn — every untrusted field reaching Slack is neutralized", () => {
  // codex follow-up: not just the brand — a malicious creator HANDLE or video
  // caption (hook_text) also flows into the mrkdwn. Both alert formatters must
  // escape them so a handle like "<!channel>" or a caption with <url|text> can't
  // inject a broadcast/phishing link.
  const evilVideo: VideoRecord = {
    ...video,
    hook_text: "<!channel> click <https://evil.test|here> & win",
    format_tag: "<!here>",
    video_url: "https://www.tiktok.com/@x/video/1",
  };
  const evilCompetitor: Competitor = { id: 1, handle: "<!channel>", platform: "tiktok", added_at: "" };

  test("escapeMrkdwn replaces & < > with entities", () => {
    expect(escapeMrkdwn("a & b < c > d")).toBe("a &amp; b &lt; c &gt; d");
  });

  test("breakout alert escapes a malicious handle, caption, and format tag", () => {
    const t = formatAlert(evilCompetitor, { video: evilVideo, ratio: 3, threshold: 100 });
    expect(t).not.toContain("<!channel>");
    expect(t).not.toContain("<!here>");
    expect(t).not.toContain("<https://evil.test|here>"); // pipe-link can't form without raw <
    expect(t).toContain("&lt;!channel&gt;");
  });

  test("threshold reminder escapes a malicious handle and caption", () => {
    const t = formatThresholdReminder(evilCompetitor, { video: evilVideo, ratio: 2, threshold: 100 }, "BeFreed");
    expect(t).not.toContain("<!channel>");
    expect(t).not.toContain("<https://evil.test|here>");
    expect(t).toContain("&lt;!channel&gt;");
  });
});
