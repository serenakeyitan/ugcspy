/**
 * Render-provider interface. Each adapter wraps one video-gen or TTS API
 * behind a uniform shape so the composer can swap providers without code
 * changes (matching the pattern in src/providers/types.ts for scrapers).
 */

export interface ClipGenRequest {
  prompt: string;
  duration_sec: number;
  // Optional first-frame image (file path or URL). When set, the provider
  // generates an image-to-video; otherwise text-to-video. Not all providers
  // support both — adapters that don't will throw a clear error.
  first_frame?: string;
  // Output aspect ratio. We default to 9:16 for TikTok/Reels.
  aspect_ratio?: "9:16" | "16:9" | "1:1";
}

export interface ClipGenResult {
  // Local path to the downloaded MP4. The adapter is responsible for
  // pulling the video from the provider's CDN to disk.
  mp4_path: string;
  // Provider's own ID for traceability (useful when debugging cost/quality).
  external_id: string;
  // Cost in USD as billed at the time of the call. Used to surface a
  // running total to the user before they spend more.
  cost_usd: number;
}

export interface VideoGenProvider {
  readonly name: string;
  readonly cost_per_second_usd: number;
  generateClip(req: ClipGenRequest): Promise<ClipGenResult>;
}

/**
 * Post-generation lip-sync warp. Takes a clip the provider previously
 * generated (referenced by external_id from a ClipGenResult) plus an
 * audio file, returns a new clip whose mouth movements match the audio.
 *
 * Only sensible on clips that contain a clear human face — Kling's API
 * will reject (with code != 0) if no face is detectable. The caller is
 * responsible for gating to talking-head formats; the provider just
 * surfaces the API error if the gate is wrong.
 */
export interface LipSyncRequest {
  /** task_id / external_id from a prior generateClip call. Must be from
   *  the same provider account and ≤30 days old (Kling constraint). */
  video_id: string;
  /** Path on local disk to the audio file (mp3/wav/m4a/aac, ≤5MB). The
   *  adapter base64-encodes it and posts inline; no external upload step. */
  audio_path: string;
}

export interface LipSyncResult {
  mp4_path: string;
  external_id: string;
  cost_usd: number;
}

export interface LipSyncProvider {
  readonly name: string;
  /** USD per second of warped video. Kling Std is ~$0.084/sec. */
  readonly lipsync_cost_per_second_usd: number;
  lipSyncClip(req: LipSyncRequest): Promise<LipSyncResult>;
  /** Optional: bundled TTS+lipsync in one call. Kling supports this via
   * mode: text2video on the same endpoint. Hard 120-char limit per call.
   * Adapters that don't support it can throw or omit the method. */
  lipSyncWithText?(req: LipSyncWithTextRequest): Promise<LipSyncResult>;
}

/**
 * Bundled TTS + lip-sync (Kling's `mode: "text2video"` on the lipsync
 * endpoint). The provider generates the TTS internally and produces a
 * face-synced video in a single call. No separate audio file to manage.
 *
 * Use this when:
 *   - the TTS text fits the provider's char limit (Kling: 120)
 *   - the source video is talking-head (otherwise lipsync fails anyway)
 *
 * Voice catalog is provider-specific. Pass voice_id explicitly when you
 * want a particular voice; omit it to use the provider's default for the
 * language.
 */
export interface LipSyncWithTextRequest {
  /** task_id / external_id from a prior generateClip call. */
  video_id: string;
  /** Text to be spoken. Provider may enforce a hard char limit. */
  text: string;
  /** Provider-specific voice catalog ID. Optional — provider picks a
   *  default for the language when unset. */
  voice_id?: string;
  /** Language code. Kling supports "en" and "zh". */
  voice_language?: "en" | "zh";
  /** Speech rate, 0.8–2.0 (Kling range). Defaults to 1.0. */
  voice_speed?: number;
}

export interface TtsRequest {
  text: string;
  // Stock voice id. Each provider has its own voice catalog; we default
  // to a "neutral young adult female" voice that matches typical UGC
  // creator cadence.
  voice_id?: string;
  speed?: number; // 0.5–2.0, default 1.0
}

export interface TtsResult {
  mp3_path: string;
  duration_sec: number;
  cost_usd: number;
}

export interface TtsProvider {
  readonly name: string;
  generateVoiceover(req: TtsRequest): Promise<TtsResult>;
}

export class RenderError extends Error {
  constructor(message: string, public provider: string, public override cause?: unknown) {
    super(message);
    this.name = "RenderError";
  }
}
