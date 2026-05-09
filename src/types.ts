export type Platform = "tiktok" | "instagram";

export const FORMAT_TAGS = [
  "GRWM",
  "POV",
  "talking_head",
  "product_demo",
  "unboxing",
  "tutorial",
  "before_after",
  "voiceover_broll",
  "duet_stitch",
  "other",
] as const;

export type FormatTag = (typeof FORMAT_TAGS)[number];

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
  anthropic_api_key?: string;
  scraper_provider: "tiktok-oss" | "scrapecreators" | "apify" | "bright_data" | "mock";
  scraper_api_key?: string;
  openai_api_key?: string;
  default_slack_webhook?: string;
}
