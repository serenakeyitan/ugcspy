export type Platform = "tiktok" | "instagram";

// Format tags are no longer auto-classified by the standalone CLI (that needed
// an Anthropic key). The Claude Code plugin classifies on demand using the
// user's existing subscription. The DB column stays for schema compat and
// future import paths; values come from the plugin or are null.
export type FormatTag = string;

export type HookSource = "caption" | "overlay" | "whisper" | "none";

export interface RawVideo {
  platform: Platform;
  external_id: string;
  posted_at: string;
  caption: string;
  thumbnail_url: string;
  video_url: string;
  view_count: number;
  like_count: number;
  comment_count: number;
  share_count: number;
  // Author handle of the actual poster. For handle searches, this matches the
  // queried handle. For hashtag searches, this is the third-party creator
  // promoting the brand — different per row. Optional/nullable so legacy data
  // and SQLite NULLs both load cleanly.
  author_handle?: string | null;
}

export interface VideoRecord extends RawVideo {
  id: number;
  competitor_id: number;
  fetched_at: string;
  hook_source: HookSource;
  hook_text: string;
  hook_confidence: number;
  format_tag: FormatTag | null;
  raw_metrics_json: string;
  // For hashtag results, this is the third-party creator. For handle results,
  // this matches the queried handle (or null if pre-migration data).
  author_handle?: string | null;
  // Whisper transcript cache (ugcspy transcript). NULL until transcribed —
  // transcription is expensive (~10-40s/video), the content of a posted video
  // never changes, so it's fetched once and cached forever.
  transcript?: string | null;
  transcript_kind?: AudioKind | null;
  transcript_lang?: string | null;
  transcript_words?: number | null;
  transcript_duration_sec?: number | null;
  transcribed_at?: string | null;
}

// Whole-track audio classification from the Whisper bridge: "speech" = all
// segments are real speech, "music" = nothing but a bed (no spoken narrative),
// "mixed" = both. Whisper's hallucinated lyrics over music beds are blanked
// upstream (no_speech_prob gate), so "music" is trustworthy.
export type AudioKind = "speech" | "music" | "mixed";

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string; // blanked ("") for kind === "non_speech"
  kind: "speech" | "non_lexical" | "non_speech";
  no_speech_prob?: number;
}

// One video's transcript doc as emitted by the bridge's transcript mode.
export interface TranscriptDoc {
  language: string | null;
  duration_sec: number;
  segments: TranscriptSegment[];
  audio_kind: AudioKind;
  // Word count over speech segments only — the talking-classifier input.
  lexical_word_count: number;
  video_url?: string;
  whisper_model?: string;
}

export interface Competitor {
  id: number;
  handle: string;
  platform: Platform;
  added_at: string;
}

export interface Watch {
  id: number;
  competitor_id: number;
  slack_webhook_url: string;
  threshold_multiplier: number;
  state: "warming_up" | "active";
  created_at: string;
}

export interface AlertFired {
  id: number;
  video_id: number;
  watch_id: number;
  fired_at: string;
}

export interface Config {
  scraper_provider: "tiktok-oss" | "scrapecreators" | "apify" | "bright_data" | "mock";
  scraper_api_key?: string;
  default_slack_webhook?: string;
}
