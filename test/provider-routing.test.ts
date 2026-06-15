import { describe, expect, test } from "bun:test";
import { getProvider } from "../src/providers/index.ts";
import type { Config } from "../src/types.ts";

// Phase 1: getProvider is PLATFORM-AWARE. The OSS default must route each
// platform to its own browser-free bridge — tiktok→tiktok-oss, instagram→
// instagram-oss — so a single `--platform all` run drives both correctly.
function cfg(provider: Config["scraper_provider"]): Config {
  return { scraper_provider: provider };
}

describe("getProvider platform routing", () => {
  test("tiktok-oss config routes tiktok → tiktok-oss", () => {
    expect(getProvider(cfg("tiktok-oss"), "tiktok").name).toBe("tiktok-oss");
  });

  test("tiktok-oss config routes instagram → instagram-oss (the new sibling)", () => {
    expect(getProvider(cfg("tiktok-oss"), "instagram").name).toBe("instagram-oss");
  });

  test("mock provider is platform-agnostic (same provider for both)", () => {
    expect(getProvider(cfg("mock"), "tiktok").name).toBe("mock");
    expect(getProvider(cfg("mock"), "instagram").name).toBe("mock");
  });

  test("the IG provider rejects a non-instagram platform loudly", async () => {
    const ig = getProvider(cfg("tiktok-oss"), "instagram");
    // Wrong platform → clear ProviderError, never a silent empty result.
    await expect(ig.fetchRecentVideos("@x", "tiktok", 30)).rejects.toThrow(/only supports instagram/);
  });

  test("the TikTok provider rejects instagram loudly (no accidental cross-wiring)", async () => {
    const tt = getProvider(cfg("tiktok-oss"), "tiktok");
    await expect(tt.fetchRecentVideos("@x", "instagram", 30)).rejects.toThrow(/only supports tiktok/);
  });
});
