import { describe, expect, test } from "bun:test";
import { ProviderError } from "../src/providers/types.ts";
import { TikTokOssProvider } from "../src/providers/tiktok-oss.ts";

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
