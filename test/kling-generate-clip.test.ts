import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { KlingProvider } from "../src/render/kling.ts";

/**
 * Guard the defense-in-depth duration check added in PR #13. Kling text2video
 * only renders 5s or 10s segments; if a caller forgets to round, the adapter
 * should refuse loudly instead of silently truncating a 14s render to 10s.
 *
 * The matching invariant lives in
 * `vendor/video-recipe/scripts/compose.py:kling_billed_duration`. If you
 * change one, change the other and update both tests.
 */
describe("KlingProvider.generateClip duration guard", () => {
  test("rejects duration_sec > 10 with a clear error", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 14 }),
    ).rejects.toThrow(/duration_sec=14/);
  });

  test("rejects duration_sec >> 10 with a clear error", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 47 }),
    ).rejects.toThrow(/Kling std supports 5s or 10s/);
  });

  test("rejects when caller bypasses kling_billed_duration but provides an in-range value", async () => {
    // Boundary case: 10.0001s passes the < int rounding in some langs
    // but fails the > 10 guard. Confirms the strict-greater-than.
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 10.0001 }),
    ).rejects.toThrow(/duration_sec=10.0001/);
  });

  test("rejects prompts longer than Kling's 2500-char cap (issue #30)", async () => {
    // Codex flagged: compose.py's L1 transcript injection truncates the
    // appended portion but doesn't cap total prompt length. A long
    // base_prompt + 300-char append could exceed Kling's text2video
    // cap and fail at submit with a cryptic API error. The guard here
    // catches it upfront with a clear remediation pointing at recipe.json.
    const provider = new KlingProvider("access", "secret");
    const longPrompt = "x".repeat(2501);
    await expect(
      provider.generateClip({ prompt: longPrompt, duration_sec: 5 }),
    ).rejects.toThrow(/2500/);
  });

  // Note: we don't test "accepts prompts at exactly 2500 chars" because
  // the happy path requires mocking the full Kling submit+poll+download
  // cycle, which is covered in test/kling-lipsync.test.ts. Asserting
  // the inverse ("not.toThrow(/2500/)") would create a flaky test
  // that depends on what error fires NEXT (network, JWT, etc.) when
  // we lack mocks. The boundary correctness is locked by the
  // implementation's `> PROMPT_CHAR_LIMIT` comparison + the 2501-char
  // rejection test above.

  // Note: we don't test the text2video happy path here because that
  // requires mocking the full Kling submit+poll+download cycle, which is
  // covered in test/kling-lipsync.test.ts via makeMockFetch. The
  // sole purpose of this file is the upfront duration + prompt guards.
});

/**
 * image2video / character consistency (issue #25). When generateClip
 * receives a `first_frame` reference image, it must call
 * /v1/videos/image2video (not text2video) and pass the image as either a
 * pass-through URL or inline base64. Same fetch-mock pattern as
 * kling-lipsync.test.ts.
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

const SUCCEED_CYCLE: MockResponse[] = [
  { status: 200, json: { code: 0, data: { task_id: "img-task-1" } } },
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
];

describe("KlingProvider.generateClip image2video (character consistency)", () => {
  const imgDir = join(tmpdir(), "ugcspy-test-img2v");
  const imgPath = join(imgDir, "reference.jpg");

  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
    mkdirSync(imgDir, { recursive: true });
    writeFileSync(imgPath, Buffer.alloc(300, 0x55)); // tiny fake jpeg
  });

  afterEach(() => {
    global.fetch = originalFetch;
    try {
      rmSync(imgDir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  test("routes to /v1/videos/image2video with inline base64 when first_frame is a local path", async () => {
    global.fetch = makeMockFetch(SUCCEED_CYCLE) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    const result = await provider.generateClip({
      prompt: "she gestures to camera",
      duration_sec: 5,
      first_frame: imgPath,
    });

    const submit = captured.find((r) => r.url.endsWith("/v1/videos/image2video"));
    expect(submit).toBeDefined();
    expect(submit!.method).toBe("POST");
    expect(submit!.headers.authorization).toMatch(/^Bearer eyJ/);
    const body = JSON.parse(submit!.body) as { image: string; prompt: string; aspect_ratio?: string };
    // Local file → base64 (300 bytes → 400 base64 chars), not a URL.
    expect(body.image).not.toMatch(/^https?:/);
    expect(body.image.length).toBe(400);
    expect(body.prompt).toBe("she gestures to camera");
    // image2video infers ratio from the reference; no aspect_ratio sent.
    expect(body.aspect_ratio).toBeUndefined();

    // Poll hits the image2video status endpoint, not text2video.
    expect(captured.some((r) => r.url.includes("/v1/videos/image2video/img-task-1"))).toBe(true);
    expect(result.external_id).toBe("img-task-1");
    expect(result.cost_usd).toBeCloseTo(0.5, 5); // 5s * $0.10
  }, 15000);

  test("passes an http(s) first_frame URL through unchanged", async () => {
    global.fetch = makeMockFetch(SUCCEED_CYCLE) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    await provider.generateClip({
      prompt: "p",
      duration_sec: 5,
      first_frame: "https://cdn.example.com/face.jpg",
    });
    const submit = captured.find((r) => r.url.endsWith("/v1/videos/image2video"));
    const body = JSON.parse(submit!.body) as { image: string };
    expect(body.image).toBe("https://cdn.example.com/face.jpg");
  }, 15000);

  test("still uses text2video when no first_frame is given", async () => {
    global.fetch = makeMockFetch(SUCCEED_CYCLE) as typeof fetch;
    const provider = new KlingProvider("access", "secret");
    await provider.generateClip({ prompt: "p", duration_sec: 5 });
    expect(captured.some((r) => r.url.endsWith("/v1/videos/text2video"))).toBe(true);
    expect(captured.some((r) => r.url.endsWith("/v1/videos/image2video"))).toBe(false);
  }, 15000);

  test("throws a clear error when the local reference image is missing", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({
        prompt: "p",
        duration_sec: 5,
        first_frame: join(imgDir, "does-not-exist.jpg"),
      }),
    ).rejects.toThrow(/failed to read reference image/);
  });

  test("rejects an oversized inline reference image (>10MB base64)", async () => {
    const bigPath = join(imgDir, "big.jpg");
    writeFileSync(bigPath, Buffer.alloc(8 * 1024 * 1024, 0)); // 8MB raw → ~10.7MB base64
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "p", duration_sec: 5, first_frame: bigPath }),
    ).rejects.toThrow(/10MB/);
  });
});
