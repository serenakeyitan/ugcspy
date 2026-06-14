import { describe, expect, test } from "bun:test";
import { platform } from "node:os";
import { ProviderError } from "../src/providers/types.ts";
import {
  TikTokOssProvider,
  authorFromUrl,
  bridgeTimeoutMs,
  parseBridgeOutput,
  parseSimilarOutput,
  resolveBridgePython,
} from "../src/providers/tiktok-oss.ts";
import { venvPython } from "../src/lib/venv.ts";
import { seedToHandle } from "../src/commands/similar.ts";

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

  test("rows missing/malformed any required field are dropped (fail-soft on untrusted JSON), valid rows kept", () => {
    const out = parseBridgeOutput(
      0,
      JSON.stringify([
        bridgeRow(), // valid
        bridgeRow({ video_url: 12345 }), // non-string video_url → dropped
        bridgeRow({ external_id: null }), // missing external_id → dropped
        bridgeRow({ caption: undefined }), // missing caption (would crash captionHook.trim) → dropped
        bridgeRow({ thumbnail_url: 7 }), // non-string thumbnail_url → dropped
        bridgeRow({ view_count: "lots" }), // non-finite metric → dropped
        bridgeRow({ platform: "instagram" }), // wrong platform → dropped
        { platform: "tiktok", caption: "no required fields" }, // dropped
        bridgeRow({ external_id: "OK2", video_url: "https://www.tiktok.com/@x/video/2" }), // valid
      ]),
      "",
    );
    expect(out.length).toBe(2);
    expect(out.map((v) => v.external_id).sort()).toEqual(["7632734206828875021", "OK2"]);
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

describe("resolveBridgePython stdlib-only fallbacks", () => {
  test("keyword, trending AND snowball run on system python without the venv", () => {
    expect(() => resolveBridgePython(false, "keyword")).not.toThrow();
    expect(() => resolveBridgePython(false, "trending")).not.toThrow();
    // snowball is pure HTTP (the follow-graph walk via tikwm) — must NOT need the venv.
    expect(() => resolveBridgePython(false, "snowball")).not.toThrow();
  });
  test("hashtag/user/transcript still require the venv", () => {
    for (const mode of ["hashtag", "user", "transcript"]) {
      expect(() => resolveBridgePython(false, mode)).toThrow(/install-deps/);
    }
  });
});

describe("parseSimilarOutput (snowball follow-graph envelope)", () => {
  test("empty creators is a VALID result, not an error", () => {
    // Most following lists are private/blocked — empty is the common case.
    expect(parseSimilarOutput(0, JSON.stringify({ creators: [], seedResults: [] }), "")).toEqual({
      creators: [],
      seedResults: [],
    });
  });

  test("parses creators and seedResults from the envelope", () => {
    const out = parseSimilarOutput(
      0,
      JSON.stringify({
        creators: [
          { handle: "alice", seedsFollowing: 3 },
          { handle: "bob", seedsFollowing: 1 },
        ],
        seedResults: [
          { handle: "seed1", status: 40 },
          { handle: "seed2", status: -1 },
        ],
      }),
      "",
    );
    expect(out.creators).toEqual([
      { handle: "alice", seedsFollowing: 3 },
      { handle: "bob", seedsFollowing: 1 },
    ]);
    expect(out.seedResults).toEqual([
      { handle: "seed1", status: 40 },
      { handle: "seed2", status: -1 },
    ]);
  });

  test("drops creator rows missing a handle or with a non-numeric score (untrusted JSON)", () => {
    const out = parseSimilarOutput(
      0,
      JSON.stringify({
        creators: [
          { handle: "good", seedsFollowing: 2 },
          { handle: "", seedsFollowing: 5 }, // blank handle → dropped
          { seedsFollowing: 4 }, // no handle → dropped
          { handle: "nan", seedsFollowing: "lots" }, // non-numeric → dropped
          { handle: "alsogood", seedsFollowing: 1 },
        ],
        seedResults: [],
      }),
      "",
    );
    expect(out.creators).toEqual([
      { handle: "good", seedsFollowing: 2 },
      { handle: "alsogood", seedsFollowing: 1 },
    ]);
  });

  test("missing seedResults degrades to an empty array, not a crash", () => {
    const out = parseSimilarOutput(0, JSON.stringify({ creators: [] }), "");
    expect(out).toEqual({ creators: [], seedResults: [] });
  });

  test("a nonzero exit surfaces the bridge error envelope", () => {
    expect(() =>
      parseSimilarOutput(1, JSON.stringify({ error: "no valid seed handles" }), ""),
    ).toThrow(/no valid seed handles/);
  });

  test("a bare array (old shape) is rejected as a non-envelope", () => {
    expect(() => parseSimilarOutput(0, "[]", "")).toThrow(/non-envelope/);
  });
});

describe("seedToHandle (URL → handle normalization)", () => {
  test("strips a leading @ and lowercases a plain handle", () => {
    expect(seedToHandle("@Kathryn.Tatess")).toBe("kathryn.tatess");
    expect(seedToHandle("bresvibes")).toBe("bresvibes");
  });

  test("extracts the creator from a profile or video URL", () => {
    expect(seedToHandle("https://www.tiktok.com/@logandowntalks")).toBe("logandowntalks");
    expect(seedToHandle("https://tiktok.com/@cadyebs/video/7644237775092370702")).toBe("cadyebs");
  });

  test("trims surrounding whitespace", () => {
    expect(seedToHandle("  @abefromanx  ")).toBe("abefromanx");
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

  test("partially-numeric values fall back instead of parsing the prefix ('1h' must not become 1ms)", () => {
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "30000ms" })).toBe(1_800_000);
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "1h" })).toBe(1_800_000);
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: "1e6" })).toBe(1_800_000);
    expect(bridgeTimeoutMs({ UGCSPY_BRIDGE_TIMEOUT_MS: " 5000 " })).toBe(5000); // trimmed whole-integer ok
  });
});
