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
    const body = JSON.parse(submit!.body) as {
      image: string;
      prompt: string;
      aspect_ratio?: string;
      model_name: string;
      mode: string;
    };
    // Local file → base64 (300 bytes → 400 base64 chars), not a URL.
    expect(body.image).not.toMatch(/^https?:/);
    expect(body.image.length).toBe(400);
    expect(body.prompt).toBe("she gestures to camera");
    // image2video infers ratio from the reference; no aspect_ratio sent.
    expect(body.aspect_ratio).toBeUndefined();
    // Default model + mode: flagship kling-v3 in pro.
    expect(body.model_name).toBe("kling-v3");
    expect(body.mode).toBe("pro");

    // Poll hits the image2video status endpoint, not text2video.
    expect(captured.some((r) => r.url.includes("/v1/videos/image2video/img-task-1"))).toBe(true);
    expect(result.external_id).toBe("img-task-1");
    // 5s * v3 pro ($0.21/s) = $1.05.
    expect(result.cost_usd).toBeCloseTo(1.05, 5);
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

/**
 * Quality params (v2-6 upgrade): model selection, std/pro mode, negative_prompt,
 * cfg_scale, and the image_tail end-frame.
 */
describe("KlingProvider.generateClip quality params", () => {
  // Reuse the module-level `captured` array that makeMockFetch writes to —
  // declaring a local one here would shadow it and stay empty.
  let originalFetch: typeof fetch;
  const imgDir = join(tmpdir(), "ugcspy-test-quality");

  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
    mkdirSync(imgDir, { recursive: true });
  });
  afterEach(() => {
    global.fetch = originalFetch;
    try {
      rmSync(imgDir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  function mockOnce() {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "q-1" } } },
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
  }

  function submitBody() {
    const submit = captured.find((r) => r.method === "POST" && r.url.includes("/v1/videos/"));
    return JSON.parse(submit!.body) as Record<string, unknown>;
  }

  test("text2video defaults to kling-v3 pro", async () => {
    mockOnce();
    const result = await new KlingProvider("a", "s").generateClip({ prompt: "p", duration_sec: 5 });
    const body = submitBody();
    expect(body.model_name).toBe("kling-v3");
    expect(body.mode).toBe("pro");
    // 5s * v3 pro ($0.21/s) = $1.05.
    expect(result.cost_usd).toBeCloseTo(1.05, 5);
  }, 15000);

  test("honors an explicit model + std mode and prices it accordingly", async () => {
    mockOnce();
    const result = await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      model: "kling-v1-6",
      mode: "std",
    });
    const body = submitBody();
    expect(body.model_name).toBe("kling-v1-6");
    expect(body.mode).toBe("std");
    // 5s * v1-6 std ($0.05/s) = $0.25.
    expect(result.cost_usd).toBeCloseTo(0.25, 5);
  }, 15000);

  test("supports 4k mode and prices it at the 4k tier", async () => {
    mockOnce();
    const result = await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      mode: "4k",
    });
    expect(submitBody().mode).toBe("4k");
    // 5s * v3 4k ($0.42/s) = $2.10.
    expect(result.cost_usd).toBeCloseTo(2.1, 5);
  }, 15000);

  test("sends sound:on for native audio when requested, omits it otherwise", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({ prompt: "p", duration_sec: 5, sound: "on" });
    expect(submitBody().sound).toBe("on");

    captured = []; // reset so submitBody() finds the SECOND call's POST
    mockOnce();
    await new KlingProvider("a", "s").generateClip({ prompt: "p", duration_sec: 5, sound: "off" });
    expect(submitBody().sound).toBeUndefined();
  }, 15000);

  test("coerces a pro-only model (v2-1-master) to pro even when std is asked", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      model: "kling-v2-1-master",
      mode: "std",
    });
    expect(submitBody().mode).toBe("pro");
  }, 15000);

  test("passes negative_prompt and clamped cfg_scale on a cfg-capable model", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      model: "kling-v1-6", // v1.x supports cfg_scale
      negative_prompt: "blurry, warped hands, watermark",
      cfg_scale: 1.7, // out of range → clamped to 1
    });
    const body = submitBody();
    expect(body.negative_prompt).toBe("blurry, warped hands, watermark");
    expect(body.cfg_scale).toBe(1);
  }, 15000);

  test("drops cfg_scale on models that don't support it (v3 / v2.x)", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      model: "kling-v3",
      cfg_scale: 0.7,
    });
    expect(submitBody().cfg_scale).toBeUndefined();
  }, 15000);

  test("defaults to the official Singapore domain; honors a base-URL override", async () => {
    // Check the SIGNED API calls (submit + poll) — the download hits the CDN
    // URL, not the API host, so we filter to /v1/videos/ requests.
    const apiCalls = (host: string) =>
      captured.filter((r) => r.url.includes("/v1/videos/")).every((r) => r.url.startsWith(host));

    mockOnce();
    await new KlingProvider("a", "s").generateClip({ prompt: "p", duration_sec: 5 });
    expect(apiCalls("https://api-singapore.klingai.com/")).toBe(true);

    captured = [];
    mockOnce();
    await new KlingProvider("a", "s", "https://api.klingai.com").generateClip({
      prompt: "p",
      duration_sec: 5,
    });
    expect(apiCalls("https://api.klingai.com/")).toBe(true);
  }, 15000);

  test("omits negative_prompt and cfg_scale when not provided", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({ prompt: "p", duration_sec: 5 });
    const body = submitBody();
    expect(body.negative_prompt).toBeUndefined();
    expect(body.cfg_scale).toBeUndefined();
  }, 15000);

  test("sends image_tail (end frame) alongside image for image2video", async () => {
    mockOnce();
    const start = join(imgDir, "start.jpg");
    const end = join(imgDir, "end.jpg");
    writeFileSync(start, Buffer.alloc(120, 0x11));
    writeFileSync(end, Buffer.alloc(120, 0x22));
    await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      first_frame: start,
      end_frame: end,
    });
    const body = submitBody();
    expect(body.image).toBeDefined();
    expect(body.image_tail).toBeDefined();
    // Distinct images → distinct base64.
    expect(body.image).not.toBe(body.image_tail);
  }, 15000);

  test("sends element_list and routes to image2video when element_ids given", async () => {
    mockOnce();
    await new KlingProvider("a", "s").generateClip({
      prompt: "p",
      duration_sec: 5,
      element_ids: [101, 202],
    });
    // Routes to image2video even without a first_frame image.
    const submit = captured.find((r) => r.url.endsWith("/v1/videos/image2video"));
    expect(submit).toBeDefined();
    const body = JSON.parse(submit!.body) as { element_list: { element_id: number }[]; image?: string };
    expect(body.element_list).toEqual([{ element_id: 101 }, { element_id: 202 }]);
    expect(body.image).toBeUndefined(); // no first_frame → no image field
  }, 15000);

  test("rejects more than 3 element_ids", async () => {
    await expect(
      new KlingProvider("a", "s").generateClip({
        prompt: "p",
        duration_sec: 5,
        element_ids: [1, 2, 3, 4],
      }),
    ).rejects.toThrow(/at most 3 elements/);
  });
});

/**
 * createElement — register a multi-image reference element and read back the
 * element_id from the async submit→poll lifecycle.
 */
describe("KlingProvider.createElement", () => {
  let originalFetch: typeof fetch;
  const imgDir = join(tmpdir(), "ugcspy-test-element");
  const frontal = join(imgDir, "frontal.jpg");

  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
    mkdirSync(imgDir, { recursive: true });
    writeFileSync(frontal, Buffer.alloc(150, 0x33));
  });
  afterEach(() => {
    global.fetch = originalFetch;
    try {
      rmSync(imgDir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  test("submits to advanced-custom-elements and returns the element_id", async () => {
    global.fetch = makeMockFetch([
      // submit
      { status: 200, json: { code: 0, data: { task_id: "el-task-1" } } },
      // poll → succeed with element_id
      {
        status: 200,
        json: {
          data: {
            task_status: "succeed",
            task_result: { elements: [{ element_id: 777 }] },
          },
        },
      },
    ]) as typeof fetch;

    const refer = join(imgDir, "side.jpg");
    writeFileSync(refer, Buffer.alloc(150, 0x44));
    const result = await new KlingProvider("a", "s").createElement({
      name: "creator-face-which-is-a-very-long-name-over-twenty-chars",
      description: "the creator",
      frontal_image: frontal,
      refer_images: [refer],
      tag_id: "o_102",
    });

    const submit = captured.find((r) => r.url.endsWith("/v1/general/advanced-custom-elements"));
    expect(submit).toBeDefined();
    const body = JSON.parse(submit!.body) as {
      element_name: string;
      reference_type: string;
      element_image_list: { frontal_image: string; refer_images: { image_url: string }[] };
      tag_list: { tag_id: string }[];
    };
    expect(body.reference_type).toBe("image_refer");
    expect(body.element_name.length).toBeLessThanOrEqual(20); // truncated
    expect(body.element_image_list.frontal_image).toBeDefined();
    expect(body.element_image_list.refer_images).toHaveLength(1);
    expect(body.tag_list).toEqual([{ tag_id: "o_102" }]);
    // Polls the element status endpoint.
    expect(
      captured.some((r) => r.url.includes("/v1/general/advanced-custom-elements/el-task-1")),
    ).toBe(true);
    expect(result.element_id).toBe(777);
    expect(result.external_id).toBe("el-task-1");
  }, 15000);

  test("throws when the succeed payload has no element_id", async () => {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "el-2" } } },
      { status: 200, json: { data: { task_status: "succeed", task_result: { elements: [] } } } },
    ]) as typeof fetch;
    await expect(
      new KlingProvider("a", "s").createElement({
        name: "x",
        description: "y",
        frontal_image: frontal,
      }),
    ).rejects.toThrow(/no element_id/);
  }, 15000);

  test("requires a frontal_image", async () => {
    await expect(
      new KlingProvider("a", "s").createElement({ name: "x", description: "y", frontal_image: "" }),
    ).rejects.toThrow(/frontal_image/);
  });

  test("surfaces a failed element-creation task", async () => {
    global.fetch = makeMockFetch([
      { status: 200, json: { code: 0, data: { task_id: "el-3" } } },
      { status: 200, json: { data: { task_status: "failed", task_status_msg: "bad image" } } },
    ]) as typeof fetch;
    await expect(
      new KlingProvider("a", "s").createElement({
        name: "x",
        description: "y",
        frontal_image: frontal,
      }),
    ).rejects.toThrow(/bad image/);
  }, 15000);
});
