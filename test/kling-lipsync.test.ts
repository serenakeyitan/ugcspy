import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { KlingProvider } from "../src/render/kling.ts";

/**
 * Lightweight contract tests for KlingProvider.lipSyncClip. We do NOT
 * call the real API — instead we monkey-patch global.fetch and assert
 * the request URL + body shape + auth header. This catches the kinds
 * of bugs that broke Kling auth in 875fad0 (wrong base URL, wrong body
 * field names) without burning real Kling credits.
 */

interface CapturedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
}

let captured: CapturedRequest[];
let originalFetch: typeof fetch;

interface MockResponse {
  status?: number;
  json?: unknown;
  bodyBuffer?: ArrayBuffer;
}

function makeMockFetch(responses: MockResponse[]) {
  let i = 0;
  return async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
        ? input.toString()
        : input.url;
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

describe("KlingProvider.lipSyncClip", () => {
  const audioDir = join(tmpdir(), "ugcspy-test-lipsync");
  const audioPath = join(audioDir, "fake.mp3");

  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
    mkdirSync(audioDir, { recursive: true });
    // Tiny fake mp3 — 100 bytes, well under the 5MB cap
    writeFileSync(audioPath, Buffer.alloc(100, 0x42));
  });

  afterEach(() => {
    global.fetch = originalFetch;
    try {
      rmSync(audioDir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  test("posts to /v1/videos/lip-sync with audio2video + base64 audio_file", async () => {
    // The provider's polling loop sleeps 5s between status checks (matches
    // the real Kling API rate-limit guidance). Bump the test timeout so the
    // first sleep can complete; the mock returns "succeed" on the first
    // poll so total wall time stays just over 5s.
    global.fetch = makeMockFetch([
      // submit response
      { status: 200, json: { code: 0, data: { task_id: "lip-task-123" } } },
      // poll response — succeed immediately
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { videos: [{ url: "https://kling.cdn/video.mp4", duration: "5" }] },
          },
        },
      },
      // download — just bytes
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;

    const provider = new KlingProvider("access-key", "secret-key");
    const result = await provider.lipSyncClip({ video_id: "src-task-789", audio_path: audioPath });

    // Submit request shape
    const submit = captured.find((r) => r.url.endsWith("/v1/videos/lip-sync"));
    expect(submit).toBeDefined();
    expect(submit!.method).toBe("POST");
    expect(submit!.headers.authorization).toMatch(/^Bearer eyJ/); // JWT starts with eyJ
    const body = JSON.parse(submit!.body) as {
      input: { video_id: string; mode: string; audio_type: string; audio_file: string };
    };
    expect(body.input.video_id).toBe("src-task-789");
    expect(body.input.mode).toBe("audio2video");
    expect(body.input.audio_type).toBe("file");
    expect(body.input.audio_file).toBeDefined();
    // Base64 of 100 bytes = ceil(100/3)*4 = 136 chars (well-formed)
    expect(body.input.audio_file.length).toBeGreaterThanOrEqual(136);
    expect(body.input.audio_file.length).toBeLessThan(200);

    // Poll request shape
    const poll = captured.find((r) => r.url.includes("/v1/videos/lip-sync/lip-task-123"));
    expect(poll).toBeDefined();

    // Result
    expect(result.external_id).toBe("lip-task-123");
    expect(result.mp4_path).toMatch(/kling-lipsync-lip-task-123\.mp4$/);
    // 5 seconds * $0.084/sec
    expect(result.cost_usd).toBeCloseTo(0.42, 5);
  }, 15000);

  test("throws clear error when audio file is too big (raw way over cap)", async () => {
    const bigPath = join(audioDir, "big.mp3");
    writeFileSync(bigPath, Buffer.alloc(6 * 1024 * 1024, 0)); // 6MB raw → 8MB base64
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncClip({ video_id: "x", audio_path: bigPath }),
    ).rejects.toThrow(/5MB/);
  });

  test("throws when raw passes but base64 exceeds 5MB cap (the bug Codex caught)", async () => {
    // 4MB raw passes a naive raw-bytes check (4MB < 5MB) but inflates to
    // ~5.33MB base64, which Kling would reject mid-pipeline. Before the
    // fix in #14, this slipped through our local check and burned the
    // round-trip. Now we reject upfront with a clear error.
    const trickyPath = join(audioDir, "tricky.mp3");
    writeFileSync(trickyPath, Buffer.alloc(4 * 1024 * 1024, 0)); // 4MB raw
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncClip({ video_id: "x", audio_path: trickyPath }),
    ).rejects.toThrow(/base64/);
  });

  test("accepts a file just under the base64-derived raw cap", async () => {
    // base64 cap is 5MB. Raw cap is ~3.75MB (5 * 3/4). A 3MB raw file
    // should sail through the cap check. We mock the rest of the
    // submit/poll cycle to verify the check passes (we just want to
    // know the cap doesn't false-reject).
    const okPath = join(audioDir, "ok.mp3");
    writeFileSync(okPath, Buffer.alloc(3 * 1024 * 1024, 0)); // 3MB raw → 4MB base64
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "t" } } },
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { videos: [{ url: "https://kling.cdn/v.mp4", duration: "5" }] },
          },
        },
      },
      { status: 200, bodyBuffer: new ArrayBuffer(10) },
    ]) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    // Don't await rejection — this should resolve cleanly
    const result = await provider.lipSyncClip({ video_id: "x", audio_path: okPath });
    expect(result.external_id).toBe("t");
  }, 15000);

  test("throws when credentials are missing", async () => {
    const provider = new KlingProvider("", "");
    await expect(
      provider.lipSyncClip({ video_id: "x", audio_path: audioPath }),
    ).rejects.toThrow(/KLING_ACCESS_KEY/);
  });

  test("surfaces non-zero code from Kling submit", async () => {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 1006, message: "no face detected in source video" } },
    ]) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.lipSyncClip({ video_id: "x", audio_path: audioPath }),
    ).rejects.toThrow(/no face detected/);
  });
});
