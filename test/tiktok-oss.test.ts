import { describe, expect, test } from "bun:test";
import { ProviderError } from "../src/providers/types.ts";
import { TikTokOssProvider, authorFromUrl } from "../src/providers/tiktok-oss.ts";

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
