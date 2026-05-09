import type { Platform, RawVideo } from "../types.ts";

export interface DataProvider {
  readonly name: string;
  fetchRecentVideos(handle: string, platform: Platform, days: number): Promise<RawVideo[]>;
}

export class ProviderError extends Error {
  constructor(message: string, public provider: string, public override cause?: unknown) {
    super(message);
    this.name = "ProviderError";
  }
}
