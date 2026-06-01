import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { KlingProvider } from "../src/render/kling.ts";

/**
 * Contract tests for KlingProvider.lipSyncWithText (issue #24). The
 * text2video mode of Kling's lipsync endpoint bundles TTS + lipsync in
 * one call, capped at 120 chars per call.
 *
 * Like the audio2video tests, we mock global.fetch to verify wire format
 * + auth + happy path without spending Kling credits.
 */

interface MockResponse {
  status?: number;
  json?: unknown;
  bodyBuffer?: ArrayBuffer;
}

interface CapturedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
}

let captured: CapturedRequest[];
let originalFetch: typeof fetch;

function makeMockFetch(responses: MockResponse[]) {
  let i = 0;
  return async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => {
      headers[k] = v;
    });
    captured.push({
      url,
      method: init?.method ?? "GET",
      headers,
      body: typeof init?.body === "string" ? init.body : "",
    });
    const r = responses[i] ?? responses[responses.length - 1];
    i++;
    const status = r?.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => r?.json,
      text: async () => JSON.stringify(r?.json),
      arrayBuffer: async () => r?.bodyBuffer ?? new ArrayBuffer(0),
    } as unknown as Response;
  };
}

describe("KlingProvider.lipSyncWithText", () => {
  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  test("posts to /v1/videos/lip-sync with text2video mode + text + voice fields", async () => {
    global.fetch = makeMockFetch([
      // submit
      { status: 200, json: { code: 0, data: { task_id: "t2v-task-456" } } },
      // poll — succeed immediately
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { videos: [{ url: "https://kling.cdn/v.mp4", duration: "5" }] },
          },
        },
      },
      // download
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;

    const provider = new KlingProvider("access", "secret");
    const result = await provider.lipSyncWithText({
      video_id: "source-task-123",
      text: "Hello from Kling TTS.",
      voice_id: "voice-abc",
      voice_language: "en",
      voice_speed: 1.0,
    });

    // Submit request shape
    const submit = captured.find((r) => r.url.endsWith("/v1/videos/lip-sync"));
    expect(submit).toBeDefined();
    expect(submit!.method).toBe("POST");
    expect(submit!.headers.authorization).toMatch(/^Bearer eyJ/);

    const body = JSON.parse(submit!.body) as {
      input: {
        video_id: string;
        mode: string;
        text: string;
        voice_id?: string;
        voice_language: string;
        voice_speed: number;
      };
    };
    expect(body.input.video_id).toBe("source-task-123");
    expect(body.input.mode).toBe("text2video");
    expect(body.input.text).toBe("Hello from Kling TTS.");
    expect(body.input.voice_id).toBe("voice-abc");
    expect(body.input.voice_language).toBe("en");
    expect(body.input.voice_speed).toBe(1.0);
    // Critically: no audio_file field (this is text2video, not audio2video)
    expect(body.input).not.toHaveProperty("audio_file");
    expect(body.input).not.toHaveProperty("audio_url");

    // Poll
    const poll = captured.find((r) => r.url.includes("/v1/videos/lip-sync/t2v-task-456"));
    expect(poll).toBeDefined();

    // Result
    expect(result.external_id).toBe("t2v-task-456");
    expect(result.mp4_path).toMatch(/kling-lipsync-t2v-t2v-task-456\.mp4$/);
    // 5s × $0.084 = $0.42
    expect(result.cost_usd).toBeCloseTo(0.42, 5);
  }, 15000);

  test("supplies a default voice_id per language when not provided", async () => {
    // text2video mode REQUIRES voice_id — Kling does NOT pick a default from
    // the language alone (omitting it yields 1201 "Voice language not found",
    // confirmed against the live api-singapore endpoint). So the adapter must
    // fill in a known-valid default rather than send a voice-less request.
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "t" } } },
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { videos: [{ id: "v", url: "https://kling.cdn/v.mp4", duration: "5" }] },
          },
        },
      },
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;

    const provider = new KlingProvider("access", "secret");
    await provider.lipSyncWithText({
      video_id: "x",
      text: "Test",
      voice_language: "zh",
    });

    const submit = captured.find((r) => r.url.endsWith("/v1/videos/lip-sync"));
    const body = JSON.parse(submit!.body) as { input: Record<string, unknown> };
    // voice_id MUST be present (the zh default), not omitted.
    expect(body.input.voice_id).toBe("ai_shatang");
    expect(body.input.voice_language).toBe("zh");
    // Default voice_speed of 1.0 should be set
    expect(body.input.voice_speed).toBe(1.0);
  }, 15000);

  test("defaults to a female English voice when language is en and no voice given", async () => {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "t" } } },
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { videos: [{ id: "v", url: "https://kling.cdn/v.mp4", duration: "5" }] },
          },
        },
      },
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;

    const provider = new KlingProvider("access", "secret");
    await provider.lipSyncWithText({ video_id: "x", text: "Hello", voice_language: "en" });

    const submit = captured.find((r) => r.url.endsWith("/v1/videos/lip-sync"));
    const body = JSON.parse(submit!.body) as { input: Record<string, unknown> };
    expect(body.input.voice_id).toBe("girlfriend_4_speech02");
    expect(body.input.voice_language).toBe("en");
  }, 15000);

  test("refuses text > 120 chars with a clear error", async () => {
    const provider = new KlingProvider("access", "secret");
    const longText = "x".repeat(121);
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: longText }),
    ).rejects.toThrow(/120/);
  });

  test("refuses empty text", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "   " }),
    ).rejects.toThrow(/non-empty/);
  });

  test("refuses unsupported voice_language", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      // @ts-expect-error — testing the runtime guard for invalid input
      provider.lipSyncWithText({ video_id: "x", text: "hi", voice_language: "ja" }),
    ).rejects.toThrow(/en/);
  });

  test("refuses voice_speed outside [0.8, 2.0]", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "hi", voice_speed: 0.5 }),
    ).rejects.toThrow(/0\.8/);
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "hi", voice_speed: 2.5 }),
    ).rejects.toThrow(/2\.0/);
  });

  test("throws when credentials are missing", async () => {
    const provider = new KlingProvider("", "");
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "hi" }),
    ).rejects.toThrow(/KLING_ACCESS_KEY/);
  });

  test("surfaces non-zero code from Kling submit", async () => {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 1006, message: "no face detected in source video" } },
    ]) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "hi" }),
    ).rejects.toThrow(/no face detected/);
  });

  test("throws truthful error when succeed response has no URL (issue #30)", async () => {
    // Before #30 the code mis-reported this as "timed out after 8min."
    // Now it surfaces the actual unexpected-shape error with the raw
    // response so the user can file a bug or update the parser.
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "t" } } },
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            // task_result omitted entirely — Kling returned succeed but no video
            task_result: { videos: [] },
          },
        },
      },
    ]) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncWithText({ video_id: "x", text: "hi" }),
    ).rejects.toThrow(/succeed but returned no video URL/);
  }, 15000);

  test("falls back to 10s safe-upper-bound billing when duration is missing (issue #30)", async () => {
    // Pre-#30 hardcoded 5s, under-billing 10s clips by 50%. Now we
    // over-attribute (10s) so internal accounting doesn't drift below
    // the real Kling bill.
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "t" } } },
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            // Duration field missing — Kling response may not always include it
            task_result: { videos: [{ url: "https://kling.cdn/v.mp4" }] },
          },
        },
      },
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    const result = await provider.lipSyncWithText({ video_id: "x", text: "hi" });
    // 10s × $0.084 = $0.84 (safe upper bound)
    expect(result.cost_usd).toBeCloseTo(0.84, 5);
  }, 15000);
});
