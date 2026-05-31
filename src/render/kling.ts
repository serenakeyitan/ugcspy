import { readFileSync, writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createHmac } from "node:crypto";
import {
  RenderError,
  type ClipGenRequest,
  type ClipGenResult,
  type LipSyncProvider,
  type LipSyncRequest,
  type LipSyncResult,
  type LipSyncWithTextRequest,
  type VideoGenProvider,
} from "./types.ts";

/**
 * Kling (kling.ai / Kuaishou) text-to-video. $0.10/sec for std mode,
 * cheapest commercial-use video-gen API as of May 2026.
 *
 * Auth (verified empirically against api.klingai.com on a real account):
 *   - Two credentials per account: access_key + secret_key
 *   - Each request signs a fresh HS256 JWT:
 *       header  = {alg: HS256, typ: JWT}
 *       payload = {iss: <access_key>, exp: now+30min, nbf: now-5s}
 *       sig     = HMAC-SHA256(secret_key, base64url(header)+"."+base64url(payload))
 *     Send as `Authorization: Bearer <jwt>`
 *
 * Wire format: confirmed against https://api.klingai.com/v1/videos/text2video
 * on 2026-05-24 — GET to list jobs returns {code:0, message:"SUCCEED"} when
 * auth is good, distinct from a 401 on bad auth.
 *
 * Polling: submit → returns task_id immediately → poll
 * /v1/videos/text2video/<task_id> every 5s until task_status="succeed"
 * (typical 1-3 min for std mode).
 */
// Native api.klingai.com model_name strings, VERIFIED against the native
// schema (302.ai / klingapi.com mirrors, 2026). Use HYPHENS, not dots —
// "kling-v2-6" is native; "kling-v2.6" is reseller-only and will fail here.
// NOTE: "kling-v3" is NOT a native model — Kling 3.0 is reseller-only
// (fal.ai/Vercel), reachable only via a different provider/auth. Don't add it
// to this map; it would fail every native call.
export const KLING_DEFAULT_MODEL = "kling-v2-6";

// Per-second USD by model + mode. Third-party-derived (Kling doesn't publish
// per-second native USD); treat as estimates for the cost preflight — the
// real Kling bill is authoritative. std/pro ≈ 1.8x; master is the ceiling.
// Numbers chosen to NOT under-bill (the failure mode that burns users).
const KLING_COST_PER_SEC: Record<string, { std: number; pro: number }> = {
  "kling-v1": { std: 0.05, pro: 0.09 },
  "kling-v1-5": { std: 0.05, pro: 0.09 },
  "kling-v1-6": { std: 0.05, pro: 0.09 },
  "kling-v2-master": { std: 0.19, pro: 0.19 }, // master is single-tier
  "kling-v2-1": { std: 0.09, pro: 0.16 },
  "kling-v2-1-master": { std: 0.19, pro: 0.19 }, // pro-only; price the ceiling
  "kling-v2-5-turbo": { std: 0.10, pro: 0.18 },
  "kling-v2-6": { std: 0.10, pro: 0.18 }, // pro adds native audio
};
// Models that only run in pro mode — we coerce mode→pro and warn.
const KLING_PRO_ONLY = new Set(["kling-v2-1-master"]);
// Fallback pricing for an unknown model_name — priced at the v2-6 tier so an
// unrecognized (e.g. brand-new) model never under-bills the cost preflight.
const KLING_COST_FALLBACK = { std: 0.10, pro: 0.18 };
// Cheapest baseline (v1-6 std), exposed as the interface-required static cost.
const KLING_BASELINE_COST_PER_SEC = 0.05;

function klingCostPerSec(model: string, mode: "std" | "pro"): number {
  const row = KLING_COST_PER_SEC[model] ?? KLING_COST_FALLBACK;
  return mode === "pro" ? row.pro : row.std;
}

export class KlingProvider implements VideoGenProvider, LipSyncProvider {
  readonly name = "kling";
  // Interface-required baseline (cheapest path: v1-6 std). Actual per-call
  // cost is computed from the chosen model+mode in generateClip — this static
  // value is only a fallback for callers that read it generically.
  readonly cost_per_second_usd = KLING_BASELINE_COST_PER_SEC;
  // Kling lip-sync Std pricing per fal.ai mirror (verified May 2026).
  // Roughly doubles per-clip cost when used.
  readonly lipsync_cost_per_second_usd = 0.084;

  // Default model + mode for clip generation when the request doesn't specify.
  // kling-v2-6 is the best native-callable model (2026): top fidelity + native
  // audio. pro is its default mode (pro is where v2-6's audio lives).
  readonly default_model = KLING_DEFAULT_MODEL;
  readonly default_mode: "std" | "pro" = "pro";

  // Base URL is api.klingai.com (NOT api.kling.ai — that domain doesn't host
  // the dev API). Verified empirically.
  private readonly base = "https://api.klingai.com";

  constructor(private accessKey: string, private secretKey: string) {}

  async generateClip(req: ClipGenRequest): Promise<ClipGenResult> {
    this.assertConfigured();
    // Kling std supports 5s or 10s segments. Compose layer should already
    // have rounded via kling_billed_duration() (see
    // vendor/video-recipe/scripts/compose.py:kling_billed_duration), but
    // we re-apply the same rule here as defense-in-depth: a caller that
    // forgets to round shouldn't get a 47s render request or have Kling
    // silently truncate to 10s with no log line.
    if (req.duration_sec > 10) {
      throw new RenderError(
        `Kling std supports 5s or 10s segments only; received duration_sec=${req.duration_sec}. ` +
          `Round the recipe cut to ≤10s in the composer before calling render, or split the cut.`,
        this.name,
      );
    }
    // Kling text2video prompt cap is ~2500 chars (per fal.ai mirror docs).
    // Compose's L1 injection truncates the *appended* transcript to 300
    // chars but doesn't cap base_prompt itself, so a long inferred prompt
    // + 300-char append could exceed Kling's limit and fail at submit
    // with a cryptic error. Catch upfront with a clear remediation.
    // Issue #30 (Codex flagged).
    const PROMPT_CHAR_LIMIT = 2500;
    if (req.prompt.length > PROMPT_CHAR_LIMIT) {
      throw new RenderError(
        `prompt is ${req.prompt.length} chars; Kling text2video caps at ${PROMPT_CHAR_LIMIT}. ` +
          `Shorten cut.inferred.prompt in recipe.json — the most descriptive ~2000 chars are ` +
          `usually plenty for diffusion-based generation. Compose's L1 transcript injection ` +
          `(~300 chars) is included in this budget.`,
        this.name,
      );
    }
    const duration = req.duration_sec <= 5 ? 5 : 10;
    const aspect = req.aspect_ratio ?? "9:16";

    // Resolve model + mode (request overrides → provider defaults). Coerce
    // pro-only models to pro and warn rather than failing on a std request.
    const model = req.model && req.model.length > 0 ? req.model : this.default_model;
    let mode: "std" | "pro" = req.mode ?? this.default_mode;
    if (KLING_PRO_ONLY.has(model) && mode !== "pro") {
      console.warn(`[kling] model ${model} is pro-only; coercing mode std → pro.`);
      mode = "pro";
    }
    // cfg_scale: Kling range is 0..1 (default 0.5). Clamp defensively.
    const cfgScale =
      typeof req.cfg_scale === "number"
        ? Math.max(0, Math.min(1, req.cfg_scale))
        : undefined;

    // Branch: image-to-video when a first_frame reference image is given,
    // else text-to-video. image2video locks character identity across cuts
    // (issue #25) — every cut generated from the SAME reference image keeps
    // the same face, instead of text2video inventing a new "young woman"
    // per cut. The two endpoints share the submit/poll/download lifecycle;
    // only the submit path + body differ.
    const useImage = typeof req.first_frame === "string" && req.first_frame.length > 0;
    const endpoint = useImage ? "/v1/videos/image2video" : "/v1/videos/text2video";

    // 1. Submit job
    const body: Record<string, unknown> = {
      model_name: model,
      prompt: req.prompt,
      duration: String(duration),
      mode,
    };
    // Shared quality knobs (both endpoints accept these).
    if (req.negative_prompt && req.negative_prompt.length > 0) {
      body.negative_prompt = req.negative_prompt;
    }
    if (cfgScale !== undefined) {
      body.cfg_scale = cfgScale;
    }
    if (useImage) {
      // Kling's `image` field accepts either a public URL or a raw
      // base64-encoded image (no data: prefix per their docs). A local file
      // path is read + base64-encoded inline. image2video does NOT take
      // aspect_ratio — the output ratio is inferred from the reference image.
      body.image = this.resolveImageField(req.first_frame as string);
      // Optional end frame (image_tail): motion interpolates first→tail.
      if (req.end_frame && req.end_frame.length > 0) {
        body.image_tail = this.resolveImageField(req.end_frame);
      }
    } else {
      // text2video uses aspect_ratio (image2video derives it from the image).
      body.aspect_ratio = aspect;
    }
    const submitRes = await this.fetchSigned(endpoint, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!submitRes.ok) {
      throw new RenderError(
        `Kling submit failed (${useImage ? "image2video" : "text2video"}): ${submitRes.status} ${await submitRes.text()}`,
        this.name,
      );
    }
    const submitJson = (await submitRes.json()) as {
      code?: number;
      message?: string;
      data?: { task_id?: string };
    };
    if (submitJson.code !== 0) {
      throw new RenderError(
        `Kling submit returned code=${submitJson.code} message="${submitJson.message}"`,
        this.name,
      );
    }
    const taskId = submitJson.data?.task_id;
    if (!taskId) throw new RenderError("Kling response missing data.task_id", this.name);

    // 2. Poll until done — same status endpoint shape for both kinds.
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 5000;
    const TIMEOUT_MS = 8 * 60 * 1000;
    let videoUrl: string | null = null;
    while (Date.now() - startedAt < TIMEOUT_MS) {
      await sleep(POLL_INTERVAL_MS);
      const statusRes = await this.fetchSigned(`${endpoint}/${taskId}`);
      if (!statusRes.ok) continue; // transient — retry on next tick
      const statusJson = (await statusRes.json()) as {
        data?: {
          task_status?: string;
          task_status_msg?: string;
          task_result?: { videos?: { url?: string }[] };
        };
      };
      const status = statusJson.data?.task_status;
      if (status === "succeed") {
        videoUrl = statusJson.data?.task_result?.videos?.[0]?.url ?? null;
        break;
      }
      if (status === "failed") {
        throw new RenderError(
          `Kling job ${taskId} failed: ${statusJson.data?.task_status_msg ?? "no message"}`,
          this.name,
        );
      }
      // "submitted" or "processing" — keep polling
    }
    if (!videoUrl) {
      throw new RenderError(`Kling job ${taskId} timed out after 8min`, this.name);
    }

    // 3. Download to local temp
    const outDir = join(tmpdir(), "ugcspy-renders");
    await mkdir(outDir, { recursive: true });
    const outPath = join(outDir, `kling-${taskId}.mp4`);
    const dl = await fetch(videoUrl);
    if (!dl.ok) {
      throw new RenderError(`Kling download failed: ${dl.status}`, this.name);
    }
    const buf = Buffer.from(await dl.arrayBuffer());
    writeFileSync(outPath, buf);

    return {
      mp4_path: outPath,
      external_id: taskId,
      // Model+mode-aware cost (not the static baseline) so the running total
      // reflects what v2-6/pro actually bills, not v1-6/std.
      cost_usd: duration * klingCostPerSec(model, mode),
    };
  }

  /**
   * Apply lip-sync warp to a previously-generated Kling clip. The source
   * clip is referenced by its task_id (from a prior generateClip) — Kling
   * has direct access to its own task outputs so no upload of the video
   * is needed.
   *
   * Audio is sent inline as base64 in the JSON body (audio_type: "file").
   * That keeps us off the hook for hosting the MP3 anywhere; OpenAI TTS
   * outputs are ~4KB/sec so a 10s clip stays well under Kling's 5MB cap.
   *
   * Constraints (per Kling docs): source video must be 5s or 10s, ≤30 days
   * old, ≥720p, and contain a clear steady face. We don't pre-check those
   * — the API rejects with code != 0 and the caller decides whether to
   * keep the un-warped clip (compose.py does this) or fail loudly.
   */
  async lipSyncClip(req: LipSyncRequest): Promise<LipSyncResult> {
    this.assertConfigured();

    // 1. Read audio file, base64-encode
    let audioBuf: Buffer;
    try {
      audioBuf = readFileSync(req.audio_path);
    } catch (e) {
      throw new RenderError(
        `lipsync: failed to read audio at ${req.audio_path}: ${(e as Error).message}`,
        this.name,
      );
    }
    // Kling's docs say the inline audio_file is capped at 5MB. The cap
    // applies to the BASE64-ENCODED payload (which is what they receive),
    // not the raw file. base64 inflates by ~33% (every 3 raw bytes → 4
    // base64 chars), so the raw cap is 5MB * 3/4 ≈ 3.75MB. A 4MB raw
    // MP3 would pass a naive raw-bytes check, then get rejected by Kling
    // mid-pipeline — exactly the failure mode the Codex audit caught.
    //
    // We compute the base64 length without actually encoding first:
    // ceil(n/3)*4 is exact. Reject before encoding to save the CPU/memory.
    const MAX_BASE64_BYTES = 5 * 1024 * 1024;
    const expectedBase64Length = Math.ceil(audioBuf.length / 3) * 4;
    if (expectedBase64Length > MAX_BASE64_BYTES) {
      const rawMB = (audioBuf.length / 1024 / 1024).toFixed(2);
      const b64MB = (expectedBase64Length / 1024 / 1024).toFixed(2);
      throw new RenderError(
        `lipsync: audio file ${req.audio_path} is ${audioBuf.length} bytes (${rawMB}MB raw → ${b64MB}MB base64); ` +
          `Kling caps inline audio at 5MB after base64 encoding (raw must be ≤ ~3.75MB). ` +
          `Shorten the clip or use audio_url mode (not yet implemented).`,
        this.name,
      );
    }
    const audioB64 = audioBuf.toString("base64");

    // 2. Submit lipsync job
    const submitRes = await this.fetchSigned("/v1/videos/lip-sync", {
      method: "POST",
      body: JSON.stringify({
        input: {
          video_id: req.video_id,
          mode: "audio2video",
          audio_type: "file",
          audio_file: audioB64,
        },
      }),
    });
    if (!submitRes.ok) {
      throw new RenderError(
        `Kling lipsync submit failed: ${submitRes.status} ${await submitRes.text()}`,
        this.name,
      );
    }
    const submitJson = (await submitRes.json()) as {
      code?: number;
      message?: string;
      data?: { task_id?: string };
    };
    if (submitJson.code !== 0) {
      throw new RenderError(
        `Kling lipsync submit returned code=${submitJson.code} message="${submitJson.message}"`,
        this.name,
      );
    }
    const taskId = submitJson.data?.task_id;
    if (!taskId) {
      throw new RenderError("Kling lipsync response missing data.task_id", this.name);
    }

    // 3. Poll for completion — same lifecycle as text2video
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 5000;
    const TIMEOUT_MS = 8 * 60 * 1000;
    let videoUrl: string | null = null;
    let durationSec = 0;
    while (Date.now() - startedAt < TIMEOUT_MS) {
      await sleep(POLL_INTERVAL_MS);
      const statusRes = await this.fetchSigned(`/v1/videos/lip-sync/${taskId}`);
      if (!statusRes.ok) continue;
      const statusJson = (await statusRes.json()) as {
        data?: {
          task_status?: string;
          task_status_msg?: string;
          task_result?: { videos?: { url?: string; duration?: string }[] };
        };
      };
      const status = statusJson.data?.task_status;
      if (status === "succeed") {
        const v = statusJson.data?.task_result?.videos?.[0];
        videoUrl = v?.url ?? null;
        durationSec = Number(v?.duration ?? 0);
        // Same defensive parsing as lipSyncWithText. Issue #30.
        if (!videoUrl) {
          throw new RenderError(
            `Kling lipsync job ${taskId} reported succeed but returned no video URL. ` +
              `Unexpected response shape. Raw response: ` +
              `${JSON.stringify(statusJson).slice(0, 500)}`,
            this.name,
          );
        }
        break;
      }
      if (status === "failed") {
        throw new RenderError(
          `Kling lipsync job ${taskId} failed: ${statusJson.data?.task_status_msg ?? "no message"}`,
          this.name,
        );
      }
    }
    if (!videoUrl) {
      throw new RenderError(`Kling lipsync job ${taskId} timed out after 8min`, this.name);
    }

    // 4. Download warped MP4 to local temp
    const outDir = join(tmpdir(), "ugcspy-renders");
    await mkdir(outDir, { recursive: true });
    const outPath = join(outDir, `kling-lipsync-${taskId}.mp4`);
    const dl = await fetch(videoUrl);
    if (!dl.ok) {
      throw new RenderError(`Kling lipsync download failed: ${dl.status}`, this.name);
    }
    writeFileSync(outPath, Buffer.from(await dl.arrayBuffer()));

    // Defensive billing — issue #30. See lipSyncWithText for full rationale.
    // Over-attribute (10s, Kling's max) when duration is missing so we
    // don't silently under-count cost.
    const billableSeconds = durationSec > 0 ? durationSec : 10;
    if (durationSec <= 0) {
      console.warn(
        `[kling] lipsync succeed response missing duration for task ${taskId}; ` +
          `billing the safe upper bound (10s = $${(10 * this.lipsync_cost_per_second_usd).toFixed(2)}). ` +
          `Real Kling bill is authoritative.`,
      );
    }
    return {
      mp4_path: outPath,
      external_id: taskId,
      cost_usd: billableSeconds * this.lipsync_cost_per_second_usd,
    };
  }

  /**
   * Bundled TTS + lip-sync via Kling's `mode: "text2video"` on the same
   * /v1/videos/lip-sync endpoint. Kling generates the TTS internally
   * (using its voice catalog) and produces a face-synced clip in one
   * call. No separate audio file to manage.
   *
   * Constraints (per Kling docs, verified May 2026):
   *   - `text` max 120 chars; longer text MUST be split by the caller
   *     into separate cuts (this method enforces the limit and refuses
   *     loudly rather than silently truncating).
   *   - `voice_language` ∈ {"en", "zh"}; other languages auto-translate
   *     to English on Kling's side (likely surprising).
   *   - `voice_speed` ∈ [0.8, 2.0].
   *
   * Same source-video constraints as lipSyncClip (5/10s, ≤30 days old,
   * clear face). Same pricing per second.
   */
  async lipSyncWithText(req: LipSyncWithTextRequest): Promise<LipSyncResult> {
    this.assertConfigured();

    if (req.text.length > 120) {
      throw new RenderError(
        `Kling lipsync text2video text is ${req.text.length} chars; Kling caps at 120. ` +
          `Either split the cut into shorter segments, or use lipSyncClip with separately-rendered TTS audio.`,
        this.name,
      );
    }
    if (!req.text.trim()) {
      throw new RenderError(
        "Kling lipsync text2video requires non-empty text.",
        this.name,
      );
    }
    const lang = req.voice_language ?? "en";
    if (lang !== "en" && lang !== "zh") {
      throw new RenderError(
        `Kling lipsync text2video voice_language must be "en" or "zh"; got ${JSON.stringify(lang)}.`,
        this.name,
      );
    }
    const speed = req.voice_speed ?? 1.0;
    if (speed < 0.8 || speed > 2.0) {
      throw new RenderError(
        `Kling lipsync text2video voice_speed must be in [0.8, 2.0]; got ${speed}.`,
        this.name,
      );
    }

    // 1. Submit lipsync text2video job
    // Schema verified against github/199-mcp/mcp-kling/kling-api-docs.md
    // section 3-13 (lip-sync endpoint, mode: text2video sub-shape).
    const submitBody: Record<string, unknown> = {
      input: {
        video_id: req.video_id,
        mode: "text2video",
        text: req.text,
        voice_language: lang,
        voice_speed: speed,
      },
    };
    if (req.voice_id) {
      (submitBody.input as Record<string, unknown>).voice_id = req.voice_id;
    }
    const submitRes = await this.fetchSigned("/v1/videos/lip-sync", {
      method: "POST",
      body: JSON.stringify(submitBody),
    });
    if (!submitRes.ok) {
      throw new RenderError(
        `Kling lipsync text2video submit failed: ${submitRes.status} ${await submitRes.text()}`,
        this.name,
      );
    }
    const submitJson = (await submitRes.json()) as {
      code?: number;
      message?: string;
      data?: { task_id?: string };
    };
    if (submitJson.code !== 0) {
      throw new RenderError(
        `Kling lipsync text2video submit returned code=${submitJson.code} message="${submitJson.message}"`,
        this.name,
      );
    }
    const taskId = submitJson.data?.task_id;
    if (!taskId) {
      throw new RenderError("Kling lipsync text2video response missing data.task_id", this.name);
    }

    // 2. Poll for completion — same lifecycle as lipSyncClip
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 5000;
    const TIMEOUT_MS = 8 * 60 * 1000;
    let videoUrl: string | null = null;
    let durationSec = 0;
    while (Date.now() - startedAt < TIMEOUT_MS) {
      await sleep(POLL_INTERVAL_MS);
      const statusRes = await this.fetchSigned(`/v1/videos/lip-sync/${taskId}`);
      if (!statusRes.ok) continue;
      const statusJson = (await statusRes.json()) as {
        data?: {
          task_status?: string;
          task_status_msg?: string;
          task_result?: { videos?: { url?: string; duration?: string }[] };
        };
      };
      const status = statusJson.data?.task_status;
      if (status === "succeed") {
        const v = statusJson.data?.task_result?.videos?.[0];
        videoUrl = v?.url ?? null;
        durationSec = Number(v?.duration ?? 0);
        // Defensive: if task_status is "succeed" but URL is missing,
        // we want to throw a TRUTHFUL error (the response shape is
        // unexpected), NOT a fake "timed out" message. Issue #30
        // (Codex caught this). Capture the raw response so the user
        // can file a bug with concrete evidence.
        if (!videoUrl) {
          throw new RenderError(
            `Kling lipsync text2video job ${taskId} reported succeed but returned no video URL. ` +
              `This is an unexpected response shape — the API may have changed. Raw response: ` +
              `${JSON.stringify(statusJson).slice(0, 500)}`,
            this.name,
          );
        }
        break;
      }
      if (status === "failed") {
        throw new RenderError(
          `Kling lipsync text2video job ${taskId} failed: ${statusJson.data?.task_status_msg ?? "no message"}`,
          this.name,
        );
      }
    }
    if (!videoUrl) {
      throw new RenderError(`Kling lipsync text2video job ${taskId} timed out after 8min`, this.name);
    }

    // 3. Download warped MP4 to local temp
    const outDir = join(tmpdir(), "ugcspy-renders");
    await mkdir(outDir, { recursive: true });
    const outPath = join(outDir, `kling-lipsync-t2v-${taskId}.mp4`);
    const dl = await fetch(videoUrl);
    if (!dl.ok) {
      throw new RenderError(`Kling lipsync text2video download failed: ${dl.status}`, this.name);
    }
    writeFileSync(outPath, Buffer.from(await dl.arrayBuffer()));

    // Defensive billing: if Kling didn't return a duration field in the
    // succeed response (issue #30), we can't know the actual clip
    // length. Two options: under-attribute (5s hardcode, the old
    // behavior) or over-attribute (10s, Kling's max). Over-attribute
    // is safer for the user — they see inflated internal cost, but
    // their actual Kling bill (which they'll see independently) is the
    // truth. Under-attribute would silently let total_cost drift below
    // the real bill, which is the failure mode that bites users.
    const billableSeconds = durationSec > 0 ? durationSec : 10;
    if (durationSec <= 0) {
      console.warn(
        `[kling] lipsync text2video succeed response missing duration for task ${taskId}; ` +
          `billing the safe upper bound (10s = $${(10 * this.lipsync_cost_per_second_usd).toFixed(2)}). ` +
          `Real Kling bill is authoritative.`,
      );
    }
    return {
      mp4_path: outPath,
      external_id: taskId,
      cost_usd: billableSeconds * this.lipsync_cost_per_second_usd,
    };
  }

  /**
   * Resolve a `first_frame` reference into Kling's `image` field value.
   *
   *  - http(s) URL → passed through unchanged (Kling fetches it server-side).
   *  - local file path → read + base64-encoded inline (no data: prefix,
   *    per Kling's image2video docs).
   *
   * Kling caps the inline image at 10MB base64. We check the post-encode
   * size and reject upfront with a clear remediation rather than burning a
   * round-trip on a too-large reference. The decode-side keyframe extractor
   * writes a JPEG at source resolution, which is comfortably under the cap
   * (a 1080x1920 JPEG is ~200-500KB), but a user could hand-pass anything.
   */
  private resolveImageField(firstFrame: string): string {
    if (/^https?:\/\//i.test(firstFrame)) {
      return firstFrame;
    }
    let imgBuf: Buffer;
    try {
      imgBuf = readFileSync(firstFrame);
    } catch (e) {
      throw new RenderError(
        `image2video: failed to read reference image at ${firstFrame}: ${(e as Error).message}. ` +
          `Pass an http(s) URL or a readable local file path.`,
        this.name,
      );
    }
    const b64 = imgBuf.toString("base64");
    const B64_CAP_BYTES = 10 * 1024 * 1024;
    if (b64.length > B64_CAP_BYTES) {
      const rawMB = (imgBuf.length / 1024 / 1024).toFixed(1);
      const b64MB = (b64.length / 1024 / 1024).toFixed(1);
      throw new RenderError(
        `image2video: reference image ${firstFrame} is ${imgBuf.length} bytes ` +
          `(${rawMB}MB raw → ${b64MB}MB base64), over Kling's 10MB inline cap. ` +
          `Downscale the reference (e.g. re-extract at ≤1080px) or host it and pass a URL instead.`,
        this.name,
      );
    }
    return b64;
  }

  /** Fail fast with an actionable message if keys are unconfigured. */
  private assertConfigured(): void {
    if (!this.accessKey || !this.secretKey) {
      throw new RenderError(
        "Kling credentials missing. Set both KLING_ACCESS_KEY and KLING_SECRET_KEY env vars (get them from https://klingai.com/dev — needs the dev account, not just the web app).",
        this.name,
      );
    }
  }

  /** Sign a fresh JWT for each request — Kling's per-request expiry is 30min. */
  private mintToken(): string {
    const now = Math.floor(Date.now() / 1000);
    const header = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = b64url(JSON.stringify({ iss: this.accessKey, exp: now + 1800, nbf: now - 5 }));
    const signingInput = `${header}.${payload}`;
    const sig = createHmac("sha256", this.secretKey).update(signingInput).digest();
    return `${signingInput}.${b64urlBuf(sig)}`;
  }

  private async fetchSigned(path: string, init: RequestInit = {}): Promise<Response> {
    const token = this.mintToken();
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${this.base}${path}`, { ...init, headers });
  }
}

function b64url(s: string): string {
  return b64urlBuf(Buffer.from(s, "utf8"));
}

function b64urlBuf(b: Buffer): string {
  return b.toString("base64").replace(/=+$/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
