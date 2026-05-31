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
  // Optional END-frame image (file path or URL). When set alongside
  // first_frame, the provider interpolates motion from first_frame → end_frame
  // (Kling's `image_tail`). Tighter control over the clip's final pose/scene.
  // Ignored by providers that don't support it.
  end_frame?: string;
  // Output aspect ratio. We default to 9:16 for TikTok/Reels. Note: for
  // image2video the provider derives the ratio from the input image and
  // ignores this.
  aspect_ratio?: "9:16" | "16:9" | "1:1";
  // Provider model id. Defaults to the provider's best current model when
  // unset. For Kling, the native model_name string (e.g. "kling-v3").
  model?: string;
  // Quality mode. For Kling: "std" = 720p (cheapest), "pro" = 1080p,
  // "4k" = native 4K (Kling 3.0). Provider default when unset.
  mode?: "std" | "pro" | "4k";
  // Things to keep OUT of the generation (artifacts, text, watermarks).
  // Providers that support it pass this through; others ignore it.
  negative_prompt?: string;
  // Prompt-adherence strength, 0..1 (Kling's cfg_scale). Higher = stricter
  // adherence to the prompt, less model freedom. Provider default when unset.
  // Note: Kling v2.x and v3 don't support this — the adapter drops it there.
  cfg_scale?: number;
  // Native audio generation. "on" makes audio-capable models (Kling 3.0)
  // produce sound + lip-sync inline, removing the need for a separate
  // lip-sync/TTS pass. Default off. Ignored by models that lack native audio.
  sound?: "on" | "off";
  // Multi-reference element IDs (Kling 3.0 `element_list`). Each is a
  // pre-registered Element Library id from createElement(). Up to 3. When set,
  // the generation anchors to ALL these elements (e.g. character face +
  // background scene) in one call — the v3-compatible multi-image path.
  // Mutually exclusive with voice references (we don't use those).
  element_ids?: number[];
}

/**
 * Register a multi-image reference "element" in Kling's Element Library, so it
 * can be referenced by id in a later generateClip (element_list). Kling builds
 * the element from a frontal image + up to 3 additional angle/detail images.
 * This is an async task (submit → poll), like clip generation.
 */
export interface ElementRequest {
  // Human-readable element name (Kling caps at 20 chars — adapter truncates).
  name: string;
  // Short description (Kling caps at 100 chars — adapter truncates).
  description: string;
  // The primary frontal reference image (file path or URL).
  frontal_image: string;
  // 0–3 additional reference images from other angles / close-ups.
  refer_images?: string[];
  // Element-library tag id (e.g. "o_102" Character, "o_106" Scene). Optional.
  tag_id?: string;
}

export interface ElementResult {
  // The numeric element_id to pass in a later ClipGenRequest.element_ids.
  element_id: number;
  // Provider task id that created it (traceability).
  external_id: string;
  // Cost in USD billed for creating the element (may be 0 if not surfaced).
  cost_usd: number;
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
