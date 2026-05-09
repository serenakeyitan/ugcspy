import type { Platform, RawVideo } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";

// Stub: real wire format goes here once we run the Day 0 spike with a live API key.
// Right now this throws a clear error if selected without a key, so the user falls back to mock.
export class ScrapeCreatorsProvider implements DataProvider {
  readonly name = "scrapecreators";
  constructor(private apiKey: string) {}

  async fetchRecentVideos(_handle: string, _platform: Platform, _days: number): Promise<RawVideo[]> {
    if (!this.apiKey) {
      throw new ProviderError(
        "ScrapeCreators API key missing. Run `ugcspy init` or set UGCSPY_SCRAPER_API_KEY.",
        this.name,
      );
    }
    throw new ProviderError(
      "ScrapeCreators integration is a Day 0 deliverable — not yet implemented. Use --provider mock for now.",
      this.name,
    );
  }
}
