import { describe, expect, test } from "bun:test";
import { platform } from "node:os";
import { ProviderError } from "../src/providers/types.ts";
import {
  TikTokOssProvider,
  authorFromUrl,
  bridgeTimeoutMs,
  parseBridgeOutput,
  resolveBridgePython,
} from "../src/providers/tiktok-oss.ts";
import { venvPython } from "../src/lib/venv.ts";

describe("authorFromUrl (free author recovery from video_url)", () => {
  // The bridge's _author can be blank (tikwm feed item with no author.unique_id),
  // but the author is ALWAYS in the URL: tiktok.com/@<handle>/video/<id>. Parsing
  // it costs zero extra fetches and kills the "(unknown)" rows.
  test("extracts handle from a standard video URL", () => {
    expect(authorFromUrl("https://www.tiktok.com/@jacob.befreed/video/7632734206828875021")).toBe(
      "jacob.befreed",
    );
  });
  test("lower-cases the handle (TikTok handles are case-insensitive)", () => {
    expect(authorFromUrl("https://www.tiktok.com/@GrowthWithMya7/video/123")).toBe("growthwithmya7");
  });
  test("returns empty for a bare URL with no @handle", () => {
    expect(authorFromUrl("https://www.tiktok.com/video/123")).toBe("");
  });
  test("returns empty for undefined or empty input", () => {
    expect(authorFromUrl(undefined)).toBe("");
    expect(authorFromUrl("")).toBe("");
  });
  test("ignores query/hash suffixes", () => {
    expect(authorFromUrl("https://www.tiktok.com/@user.name/video/9?is_copy=1")).toBe("user.name");
  });
  test("preserves dots and underscores in the handle", () => {
    expect(authorFromUrl("https://www.tiktok.com/@a.b_c123/video/1")).toBe("a.b_c123");
  });
});

describe("TikTokOssProvider", () => {
  test("rejects non-tiktok platforms with a clear ProviderError", async () => {
    const p = new TikTokOssProvider();
    await expect(p.fetchRecentVideos("@glossier", "instagram", 30)).rejects.toBeInstanceOf(
      ProviderError,
    );
    await expect(p.fetchRecentVideos("@glossier", "instagram", 30)).rejects.toMatchObject({
      message: expect.stringContaining("only supports tiktok"),
    });
  });

  test("name matches config value", () => {
    const p = new TikTokOssProvider();
    expect(p.name).toBe("tiktok-oss");
  });

  test("exposes keyword/niche search (the coverage-gap fix)", () => {
    const p = new TikTokOssProvider();
    expect(typeof p.fetchKeywordVideos).toBe("function");
  });

  test("keyword search rejects non-tiktok platforms cleanly", async () => {
    const p = new TikTokOssProvider();
    await expect(p.fetchKeywordVideos("skincare routine", "instagram", 30)).rejects.toBeInstanceOf(
      ProviderError,
    );
    await expect(p.fetchKeywordVideos("skincare routine", "instagram", 30)).rejects.toMatchObject({
      message: expect.stringContaining("only supports tiktok"),
    });
  });
});

// A realistic bridge row, _author included (the bridge's private field).
function bridgeRow(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    platform: "tiktok",
    external_id: "7632734206828875021",
    posted_at: "2026-06-01T00:00:00+00:00",
    caption: "purple colours #befreed",
    thumbnail_url: "https://t/1.jpg",
    video_url: "https://www.tiktok.com/@growthwithmya7/video/7632734206828875021",
    view_count: 2_600_000,
    like_count: 100,
    comment_count: 10,
    share_count: 5,
    _author: "growthwithmya7",
    ...over,
  };
}

describe("parseBridgeOutput (bridge stdout/error contract)", () => {
  test("non-zero exit surfaces the bridge's actionable JSON error body", () => {
    expect(() =>
      parseBridgeOutput(1, JSON.stringify({ error: "missing tag" }), "Traceback noise"),
    ).toThrow(/tiktok-oss: missing tag/);
  });

  test("non-zero exit without a JSON body falls back to stderr", () => {
    expect(() => parseBridgeOutput(1, "", "Traceback: boom")).toThrow(/Traceback: boom/);
  });

  test("non-zero exit with EMPTY stdout+stderr still yields a non-blank message naming the exit code", () => {
    // Regression: `?? "unknown error"` after stderr.trim() was unreachable —
    // an OOM-killed bridge produced `ProviderError: tiktok-oss: ` (blank).
    expect(() => parseBridgeOutput(137, "", "  ")).toThrow(/bridge exited 137/);
  });

  test("zero exit with non-JSON stdout → ProviderError", () => {
    expect(() => parseBridgeOutput(0, "WARNING: not json", "")).toThrow(ProviderError);
    expect(() => parseBridgeOutput(0, "WARNING: not json", "")).toThrow(/non-JSON/);
  });

  test("zero exit with non-array JSON → ProviderError", () => {
    expect(() => parseBridgeOutput(0, JSON.stringify({ videos: [] }), "")).toThrow(/non-array/);
  });

  test("empty array parses to []", () => {
    expect(parseBridgeOutput(0, "[]", "")).toEqual([]);
  });

  test("_author maps to author_handle and the private field is stripped", () => {
    const [v] = parseBridgeOutput(0, JSON.stringify([bridgeRow()]), "");
    expect(v!.author_handle).toBe("growthwithmya7");
    expect("_author" in v!).toBe(false);
  });

  test("blank _author falls back to the @handle in video_url ('(unknown)' rows fix)", () => {
    const [v] = parseBridgeOutput(0, JSON.stringify([bridgeRow({ _author: "" })]), "");
    expect(v!.author_handle).toBe("growthwithmya7");
  });

  test("explicit _author wins over the URL handle", () => {
    const [v] = parseBridgeOutput(
      0,
      JSON.stringify([bridgeRow({ _author: "jacob.befreed" })]),
      "",
    );
    expect(v!.author_handle).toBe("jacob.befreed");
  });

  test("neither _author nor a URL handle leaves author_handle unset", () => {
    const [v] = parseBridgeOutput(
      0,
      JSON.stringify([bridgeRow({ _author: "", video_url: "https://www.tiktok.com/video/9" })]),
      "",
    );
    expect(v!.author_handle).toBeUndefined();
  });
});

describe("resolveBridgePython (venv gating)", () => {
  test("uses the managed venv's python when the venv exists", () => {
    expect(resolveBridgePython(true, "user")).toBe(venvPython());
    expect(resolveBridgePython(true, "keyword")).toBe(venvPython());
  });

  test("keyword mode without the venv falls back to a system python", () => {
    const bin = resolveBridgePython(false, "keyword");
    // On win32 there is no `python3` on PATH (Store stub) — must be `python`.
    expect(bin).toBe(platform() === "win32" ? "python" : "python3");
  });

  test("user/hashtag without the venv throw an actionable ProviderError", () => {
    expect(() => resolveBridgePython(false, "user")).toThrow(ProviderError);
    expect(() => resolveBridgePython(false, "hashtag")).toThrow(/install-deps/);
  });
});

describe("bridgeTimeoutMs", () => {
  test("defaults to 30min", () => {
    expect(bridgeTimeoutMs({})).toBe(1_800_000);
  });

  test("env override wins", () => {
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "5000" })).toBe(5000);
  });

  test("garbage / non-positive env values fall back to the default", () => {
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "abc" })).toBe(1_800_000);
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "0" })).toBe(1_800_000);
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "-5" })).toBe(1_800_000);
  });
});
