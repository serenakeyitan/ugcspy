import type { Platform, RawVideo, TranscriptDoc } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";

// Browser-free Instagram bridge — the IG sibling of tiktok-oss. Free/OSS, built
// on a HYBRID of two tools driven off a logged-in IG browser session:
//   1. gallery-dl  — walks a creator's roster (shortcode, likes, caption,
//      downloadable video_url) fast, in bulk.
//   2. instaloader — enriches each shortcode with view_count / play_count
//      (gallery-dl's listing endpoint omits these; instaloader's single-post
//      GraphQL call returns them).
// The combination yields a complete IG VideoRecord WITH view counts, so IG
// breakout/threshold alerts run at parity with TikTok. See DESIGN.md for the
// data-source bakeoff that established this.
//
// What IG does NOT support (no honest free source — TikTok-only): trending,
// snowball/similar (follow-graph is private), keyword search (no free IG search
// relay; tikwm is TikTok-only).
//
// AUTH: needs a live logged-in IG session (cookies exported from a browser).
// Sessions expire — a missing/expired sessionid surfaces as a clear
// "re-login required" ProviderError rather than a silent empty result.
//
// NOTE: the gallery-dl + instaloader bridge implementation lands in Phase 2.
// This stub establishes the platform-routing seam (getProvider routes
// platform='instagram' here) and fails loudly until then, exactly like the
// scrapecreators stub did before its Day-0 spike.
export class InstagramOssProvider implements DataProvider {
  readonly name = "instagram-oss";

  async fetchRecentVideos(_handle: string, platform: Platform, _days: number): Promise<RawVideo[]> {
    if (platform !== "instagram") {
      throw new ProviderError(
        `Provider 'instagram-oss' only supports instagram (got '${platform}'). Use 'tiktok-oss' for TikTok.`,
        this.name,
      );
    }
    throw new ProviderError(
      "Instagram fetch bridge (gallery-dl roster + instaloader view enrichment) is not yet wired — Phase 2. Use --provider mock to exercise the IG code path meanwhile.",
      this.name,
    );
  }

  // Transcript reuses the existing audio-download + Whisper path; the IG
  // video_url comes from the Phase-2 bridge. Implemented in Phase 3.
  async fetchTranscript(_videoUrl: string): Promise<TranscriptDoc> {
    throw new ProviderError(
      "Instagram transcript is not yet wired — Phase 3.",
      this.name,
    );
  }
}
