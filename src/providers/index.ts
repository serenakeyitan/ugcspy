import type { Config, Platform } from "../types.ts";
import { effectiveScraperKey } from "../lib/config.ts";
import { InstagramOssProvider } from "./instagram-oss.ts";
import { MockProvider } from "./mock.ts";
import { ScrapeCreatorsProvider } from "./scrapecreators.ts";
import { TikTokOssProvider } from "./tiktok-oss.ts";
import type { DataProvider } from "./types.ts";

// Provider selection is PLATFORM-AWARE. The `scraper_provider` config names the
// data source for the project, but the OSS sources are platform-specific: the
// tiktok-oss bridge is 100% TikTok (tikwm/yt-dlp tiktok extractor), and the
// Instagram path is a separate gallery-dl + instaloader bridge. So when the
// config picks an OSS source, we route by `platform` to the matching OSS
// provider. Paid/cross-platform providers (scrapecreators) ignore platform here
// because they take it per-call. `mock` is platform-agnostic by design.
export function getProvider(
  config: Config,
  platform: Platform,
  // For instagram: how many roster posts to enrich with view counts (the user's
  // depth tier). Ignored for other platforms/providers.
  igEnrichCount?: number,
): DataProvider {
  switch (config.scraper_provider) {
    case "mock":
      return new MockProvider();
    case "tiktok-oss":
      // The OSS default: each platform has its own browser-free bridge.
      return platform === "instagram"
        ? new InstagramOssProvider(igEnrichCount)
        : new TikTokOssProvider();
    case "scrapecreators":
      return new ScrapeCreatorsProvider(effectiveScraperKey(config) ?? "");
    case "apify":
    case "bright_data":
      throw new Error(
        `Provider '${config.scraper_provider}' is not yet implemented. Use 'tiktok-oss', 'mock', or 'scrapecreators'.`,
      );
    default:
      throw new Error(`Unknown provider: ${config.scraper_provider}`);
  }
}

export type { DataProvider } from "./types.ts";
