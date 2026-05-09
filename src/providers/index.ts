import type { Config } from "../types.ts";
import { effectiveScraperKey } from "../lib/config.ts";
import { MockProvider } from "./mock.ts";
import { ScrapeCreatorsProvider } from "./scrapecreators.ts";
import type { DataProvider } from "./types.ts";

export function getProvider(config: Config): DataProvider {
  switch (config.scraper_provider) {
    case "mock":
      return new MockProvider();
    case "scrapecreators":
      return new ScrapeCreatorsProvider(effectiveScraperKey(config) ?? "");
    case "apify":
    case "bright_data":
      // Stubs follow same pattern as scrapecreators — implement in Day 0 spike.
      throw new Error(
        `Provider '${config.scraper_provider}' is not yet implemented. Use 'mock' or 'scrapecreators'.`,
      );
    default:
      throw new Error(`Unknown provider: ${config.scraper_provider}`);
  }
}

export type { DataProvider } from "./types.ts";
