import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { statSync } from "node:fs";
import { basename, dirname } from "node:path";
import { ElevenLabsTtsProvider } from "../src/render/elevenlabs-tts.ts";

/**
 * Contract tests for ElevenLabsTtsProvider. Mock fetch — no real
 * ElevenLabs API call during CI. Pin the wire format so a future
 * docs change (or my misreading) surfaces here instead of mid-render.
 */

interface MockResponse {
  status?: number;
  bodyBuffer?: ArrayBuffer;
  text?: string;
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
      text: async () => r?.text ?? "",
      arrayBuffer: async () => r?.bodyBuffer ?? new ArrayBuffer(0),
    } as unknown as Response;
  };
}

describe("ElevenLabsTtsProvider", () => {
  beforeEach(() => {
    captured = [];
    originalFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  test("posts to /v1/text-to-speech/:voice_id with xi-api-key header", async () => {
    // 100 bytes of dummy mp3
    const fakeMp3 = new ArrayBuffer(100);
    global.fetch = makeMockFetch([{ status: 200, bodyBuffer: fakeMp3 }]) as typeof fetch;

    const provider = new ElevenLabsTtsProvider("test-api-key");
    const result = await provider.generateVoiceover({
      text: "Hello from ElevenLabs.",
      voice_id: "S9NKLs1GeSTKzXd9D0Lf",
    });

    expect(captured).toHaveLength(1);
    const req = captured[0]!;
    // URL includes voice_id in PATH (not body)
    expect(req.url).toBe(
      "https://api.elevenlabs.io/v1/text-to-speech/S9NKLs1GeSTKzXd9D0Lf",
    );
    expect(req.method).toBe("POST");
    // ElevenLabs uses xi-api-key, NOT Bearer
    expect(req.headers["xi-api-key"]).toBe("test-api-key");
    expect(req.headers.authorization).toBeUndefined();
    // Body has text + model_id, no voice_id (it's in the URL)
    const body = JSON.parse(req.body) as { text: string; model_id: string; voice_id?: string };
    expect(body.text).toBe("Hello from ElevenLabs.");
    expect(body.model_id).toBe("eleven_multilingual_v2");
    expect(body).not.toHaveProperty("voice_id");

    // Result shape — mp3_path is a real temp file, cost is per-char
    expect(result.mp3_path).toMatch(/tts-elevenlabs-\d+\.mp3$/);
    // 22 chars × $0.30/1000 = $0.0066
    expect(result.cost_usd).toBeCloseTo(0.0066, 4);
    expect(result.duration_sec).toBeCloseTo(22 / 14, 2);
  });

  test("URL-encodes voice_id (in case of unusual chars)", async () => {
    global.fetch = makeMockFetch([{ status: 200, bodyBuffer: new ArrayBuffer(50) }]) as typeof fetch;
    const provider = new ElevenLabsTtsProvider("key");
    await provider.generateVoiceover({ text: "x", voice_id: "weird/id with space" });
    // encodeURIComponent renders "weird%2Fid%20with%20space"
    expect(captured[0]!.url).toBe(
      "https://api.elevenlabs.io/v1/text-to-speech/weird%2Fid%20with%20space",
    );
  });

  test("throws clear error when API key is missing", async () => {
    const provider = new ElevenLabsTtsProvider("");
    await expect(
      provider.generateVoiceover({ text: "hi", voice_id: "abc" }),
    ).rejects.toThrow(/ELEVENLABS_API_KEY/);
  });

  test("throws clear error when voice_id is missing", async () => {
    const provider = new ElevenLabsTtsProvider("key");
    await expect(
      provider.generateVoiceover({ text: "hi" }),
    ).rejects.toThrow(/voice_id/);
  });

  test("throws clear error when text is empty or whitespace", async () => {
    const provider = new ElevenLabsTtsProvider("key");
    await expect(
      provider.generateVoiceover({ text: "   ", voice_id: "abc" }),
    ).rejects.toThrow(/empty/);
  });

  test("surfaces ElevenLabs error response with status + body", async () => {
    global.fetch = makeMockFetch([
      {
        status: 401,
        text: '{"detail":{"status":"invalid_api_key","message":"Invalid API key"}}',
      },
    ]) as typeof fetch;
    const provider = new ElevenLabsTtsProvider("bad-key");
    await expect(
      provider.generateVoiceover({ text: "hi", voice_id: "abc" }),
    ).rejects.toThrow(/401.*invalid_api_key/);
  });

  test("throws when ElevenLabs returns 200 with empty body", async () => {
    // Defensive: a successful response with zero-byte audio is a Kling-like
    // failure mode worth catching upfront rather than passing empty audio
    // downstream to Kling lipsync.
    global.fetch = makeMockFetch([{ status: 200, bodyBuffer: new ArrayBuffer(0) }]) as typeof fetch;
    const provider = new ElevenLabsTtsProvider("key");
    await expect(
      provider.generateVoiceover({ text: "hi", voice_id: "abc" }),
    ).rejects.toThrow(/empty/);
  });

  test("writes the mp3 into a private per-process render dir (mkdtemp, 0700)", async () => {
    // Hardening: outputs used to land in a FIXED, world-guessable
    // /tmp/ugcspy-renders with default perms — pre-creatable (and then
    // owned/readable) by any local user on a shared host. The shared
    // helper (src/render/temp-dir.ts) now mkdtemps a per-process dir:
    // unpredictable suffix + 0o700.
    global.fetch = makeMockFetch([{ status: 200, bodyBuffer: new ArrayBuffer(10) }]) as typeof fetch;
    const provider = new ElevenLabsTtsProvider("key");
    const result = await provider.generateVoiceover({ text: "hi", voice_id: "v" });
    const dir = dirname(result.mp3_path);
    // mkdtemp prefix + random suffix — NOT the old fixed "ugcspy-renders".
    expect(basename(dir)).toMatch(/^ugcspy-renders-.+/);
    if (process.platform !== "win32") {
      expect(statSync(dir).mode & 0o777).toBe(0o700);
    }
  });

  test("cost scales linearly with text length", async () => {
    global.fetch = makeMockFetch([
      { status: 200, bodyBuffer: new ArrayBuffer(100) },
      { status: 200, bodyBuffer: new ArrayBuffer(100) },
    ]) as typeof fetch;
    const provider = new ElevenLabsTtsProvider("key");
    const r1 = await provider.generateVoiceover({ text: "a".repeat(1000), voice_id: "v" });
    const r2 = await provider.generateVoiceover({ text: "a".repeat(2000), voice_id: "v" });
    // 1000 chars = $0.30
    expect(r1.cost_usd).toBeCloseTo(0.30, 5);
    // 2000 chars = $0.60 (linear)
    expect(r2.cost_usd).toBeCloseTo(0.60, 5);
  });
});
