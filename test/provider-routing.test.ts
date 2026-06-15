import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { getProvider } from "../src/providers/index.ts";
import type { Config } from "../src/types.ts";

// Phase 1: getProvider is PLATFORM-AWARE. The OSS default must route each
// platform to its own browser-free bridge — tiktok→tiktok-oss, instagram→
// instagram-oss — so a single `--platform all` run drives both correctly.
// These assert the FREE (keyless) route, so we isolate HOME to an empty temp dir
// and clear the env key — otherwise a real ~/.ugcspy/scrapecreators.key on the
// dev machine would (correctly) auto-upgrade IG to scrapecreators and break the
// assertions. (effectiveScraperKey reads env > config > $HOME/.ugcspy/...key.)
function cfg(provider: Config["scraper_provider"]): Config {
  return { scraper_provider: provider };
}

let savedHome: string | undefined;
let savedKey: string | undefined;
beforeAll(() => {
  savedHome = process.env.UGCSPY_HOME;
  savedKey = process.env.UGCSPY_SCRAPER_API_KEY;
  process.env.UGCSPY_HOME = mkdtempSync(join(tmpdir(), "ugcspy-routing-")); // no key file here
  delete process.env.UGCSPY_SCRAPER_API_KEY;
});
afterAll(() => {
  if (savedHome === undefined) delete process.env.UGCSPY_HOME;
  else process.env.UGCSPY_HOME = savedHome;
  if (savedKey === undefined) delete process.env.UGCSPY_SCRAPER_API_KEY;
  else process.env.UGCSPY_SCRAPER_API_KEY = savedKey;
});

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
