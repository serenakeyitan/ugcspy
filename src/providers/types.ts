import type { Platform, RawVideo, TranscriptDoc } from "../types.ts";

export interface DataProvider {
  readonly name: string;
  // Fetch a specific handle's own videos (e.g. @glossier's posts).
  fetchRecentVideos(handle: string, platform: Platform, days: number): Promise<RawVideo[]>;
  // Fetch videos tagged with a hashtag (e.g. #liquiddeath posted by ANY creator).
  // This is how we find third-party UGC promoting a brand. Optional — providers
  // may throw a clear error if they don't support hashtag search yet.
  fetchHashtagVideos?(tag: string, platform: Platform, days: number): Promise<RawVideo[]>;
  // Fetch videos by free-text keyword/niche (e.g. "skincare routine") posted by
  // ANY creator, WITHOUT requiring a brand hashtag. This is niche/competitor
  // discovery — the broad corpus a script writer browses for format inspiration.
  // Optional — only providers with a real video-search source implement it.
  fetchKeywordVideos?(keyword: string, platform: Platform, days: number): Promise<RawVideo[]>;
  // Network-wide trending videos for a region (no brand filter) — the raw
  // material for trend-riding template discovery. Optional — needs a
  // trending-capable source.
  fetchTrendingVideos?(region: string, days: number): Promise<RawVideo[]>;
  // Download ONE video's audio and transcribe it (hook + spoken narrative +
  // talking/non-talking signal). Expensive (~10-40s/video) — callers cache the
  // result in the videos table. Optional — needs an audio pipeline (whisper).
  fetchTranscript?(videoUrl: string): Promise<TranscriptDoc>;
  // Batch form: one model load for the whole wave; results align with the
  // input order, per-video failures come back as {error} elements.
  fetchTranscriptBatch?(videoUrls: string[]): Promise<Array<TranscriptDoc | { error: string }>>;
}

export class ProviderError extends Error {
  constructor(message: string, public provider: string, public override cause?: unknown) {
    super(message);
    this.name = "ProviderError";
  }
}
