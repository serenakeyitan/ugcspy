import type { Platform, RawVideo } from "../types.ts";

export interface DataProvider {
  readonly name: string;
  // Fetch a specific handle's own videos (e.g. @befreed's posts).
  fetchRecentVideos(handle: string, platform: Platform, days: number): Promise<RawVideo[]>;
  // Fetch videos tagged with a hashtag (e.g. #befreed posted by ANY creator).
  // This is how we find third-party UGC promoting a brand. Optional — providers
  // may throw a clear error if they don't support hashtag search yet.
  fetchHashtagVideos?(tag: string, platform: Platform, days: number): Promise<RawVideo[]>;
  // Fetch videos by free-text keyword/niche (e.g. "skincare routine") posted by
  // ANY creator, WITHOUT requiring a brand hashtag. This is niche/competitor
  // discovery — the broad corpus a script writer browses for format inspiration.
  // Optional — only providers with a real video-search source implement it.
  fetchKeywordVideos?(keyword: string, platform: Platform, days: number): Promise<RawVideo[]>;
}

export class ProviderError extends Error {
  constructor(message: string, public provider: string, public override cause?: unknown) {
    super(message);
    this.name = "ProviderError";
  }
}
