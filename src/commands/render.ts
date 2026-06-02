import { loadConfig } from "../lib/config.ts";
import { ElevenLabsTtsProvider } from "../render/elevenlabs-tts.ts";
import { KlingProvider } from "../render/kling.ts";
import { OpenAITtsProvider } from "../render/openai-tts.ts";
import { RenderError } from "../render/types.ts";

/**
 * Subcommand contract for the Python composer.
 *
 * Stdin (JSON): one of
 *   { "kind": "clip",               "prompt": str, "duration_sec": int, "aspect_ratio"?: str,
 *                                   "first_frame"?: str, "end_frame"?: str, "model"?: str,
 *                                   "mode"?: "std"|"pro"|"4k", "negative_prompt"?: str,
 *                                   "cfg_scale"?: float, "sound"?: "on"|"off",
 *                                   "element_ids"?: number[] }
 *   { "kind": "create_element",     "frontal_image": str, "name"?: str, "description"?: str,
 *                                   "refer_images"?: str[], "tag_id"?: str }
 *                                   → { ok, element_id, external_id, cost_usd }
 *   { "kind": "tts",                "text": str,   "voice"?: str, "speed"?: float }
 *   { "kind": "tts_elevenlabs",     "text": str,   "voice_id": str }
 *   { "kind": "lipsync",            "video_id": str, "audio_path": str }
 *   { "kind": "lipsync_text2video", "video_id": str, "text": str (≤120 chars),
 *                                   "voice_id"?: str, "voice_language"?: "en"|"zh",
 *                                   "voice_speed"?: float (0.8-2.0) }
 *
 * Stdout (JSON):
 *   on success: { "ok": true, "mp4_path"?: str, "mp3_path"?: str,
 *                 "external_id"?: str, "duration_sec"?: float,
 *                 "cost_usd": float }
 *   on failure: { "ok": false, "error": str, "provider"?: str }
 *
 * Exit codes: 0 on success, 1 on bad input, 2 on provider error.
 *
 * Why a stdin/stdout shape: the composer is Python (ffmpeg lives there)
 * but config + secrets management is cleaner in TS. This gives Python a
 * one-call boundary into the render layer without re-implementing
 * everything in Python.
 */
export async function runRender(): Promise<void> {
  const stdinText = await readStdin();
  let req: { kind?: string; [k: string]: unknown };
  try {
    req = JSON.parse(stdinText);
  } catch {
    emitError(`invalid stdin json: ${stdinText.slice(0, 200)}`);
    process.exit(1);
  }

  const config = loadConfig();
  // Read keys from env first, then config. Env wins so CI/agents can
  // override without touching files on disk.
  const openaiKey = process.env.OPENAI_API_KEY ?? "";
  // ElevenLabs TTS (optional — only needed when --tts elevenlabs)
  const elevenlabsKey = process.env.ELEVENLABS_API_KEY ?? "";
  // Kling uses TWO keys: access_key + secret_key, both required for HMAC
  // signing each request. KLING_API_KEY (the old single-key env var) is
  // accepted as a fallback alias for KLING_ACCESS_KEY only — secret is
  // still required separately.
  const klingAccess = process.env.KLING_ACCESS_KEY ?? process.env.KLING_API_KEY ?? "";
  const klingSecret = process.env.KLING_SECRET_KEY ?? "";
  // Official API domain moved to api-singapore.klingai.com (non-China). Allow
  // overriding via KLING_BASE_URL for region-specific hosts or the old domain.
  // Empty string → the provider's built-in default.
  const klingBase = process.env.KLING_BASE_URL ?? "";

  // Kling provider is shared across the clip/element/lipsync kinds — construct
  // once. (OpenAI TTS uses its own provider below.)
  const kling = new KlingProvider(klingAccess, klingSecret, klingBase);

  try {
    if (req.kind === "clip") {
      const mode =
        req.mode === "std" || req.mode === "pro" || req.mode === "4k" ? req.mode : undefined;
      const sound = req.sound === "on" ? "on" : req.sound === "off" ? "off" : undefined;
      const result = await kling.generateClip({
        prompt: String(req.prompt ?? ""),
        duration_sec: Number(req.duration_sec ?? 5),
        aspect_ratio: (req.aspect_ratio as "9:16" | "16:9" | "1:1") ?? "9:16",
        first_frame: req.first_frame as string | undefined,
        end_frame: req.end_frame as string | undefined,
        model: req.model ? String(req.model) : undefined,
        mode,
        negative_prompt: req.negative_prompt ? String(req.negative_prompt) : undefined,
        cfg_scale: typeof req.cfg_scale === "number" ? req.cfg_scale : undefined,
        sound,
        element_ids: Array.isArray(req.element_ids)
          ? (req.element_ids as unknown[]).map((n) => Number(n)).filter((n) => Number.isFinite(n))
          : undefined,
      });
      emitOk({
        mp4_path: result.mp4_path,
        external_id: result.external_id,
        // video_id is the lip-sync-ready id (distinct from the task's
        // external_id). compose persists + passes this to lipsync_text2video.
        video_id: result.video_id,
        cost_usd: result.cost_usd,
      });
      return;
    }
    if (req.kind === "create_element") {
      // Register a multi-image reference element; returns element_id for a
      // later clip's element_ids (Kling v3 multi-reference).
      const referImages = Array.isArray(req.refer_images)
        ? (req.refer_images as unknown[]).map((s) => String(s))
        : undefined;
      const result = await kling.createElement({
        name: String(req.name ?? "ref"),
        description: String(req.description ?? ""),
        frontal_image: String(req.frontal_image ?? ""),
        refer_images: referImages,
        tag_id: req.tag_id ? String(req.tag_id) : undefined,
      });
      emitOk({
        element_id: result.element_id,
        external_id: result.external_id,
        cost_usd: result.cost_usd,
      });
      return;
    }
    if (req.kind === "tts") {
      const provider = new OpenAITtsProvider(openaiKey);
      const result = await provider.generateVoiceover({
        text: String(req.text ?? ""),
        voice_id: req.voice as string | undefined,
        speed: req.speed as number | undefined,
      });
      emitOk({
        mp3_path: result.mp3_path,
        duration_sec: result.duration_sec,
        cost_usd: result.cost_usd,
      });
      return;
    }
    if (req.kind === "tts_elevenlabs") {
      // ElevenLabs TTS — same MP3-output shape as OpenAI TTS so the
      // downstream Kling audio2video lipsync path doesn't care which
      // provider produced the audio.
      const provider = new ElevenLabsTtsProvider(elevenlabsKey);
      const result = await provider.generateVoiceover({
        text: String(req.text ?? ""),
        voice_id: req.voice_id ? String(req.voice_id) : undefined,
      });
      emitOk({
        mp3_path: result.mp3_path,
        duration_sec: result.duration_sec,
        cost_usd: result.cost_usd,
      });
      return;
    }
    if (req.kind === "lipsync") {
      const result = await kling.lipSyncClip({
        video_id: String(req.video_id ?? ""),
        audio_path: String(req.audio_path ?? ""),
      });
      emitOk({
        mp4_path: result.mp4_path,
        external_id: result.external_id,
        cost_usd: result.cost_usd,
      });
      return;
    }
    if (req.kind === "lipsync_text2video") {
      // Bundled TTS + lipsync. Kling generates the TTS internally; the
      // returned mp4 has the synced audio embedded. No separate audio
      // file needed on the caller side.
      const lang = req.voice_language as "en" | "zh" | undefined;
      const result = await kling.lipSyncWithText({
        video_id: String(req.video_id ?? ""),
        text: String(req.text ?? ""),
        voice_id: req.voice_id ? String(req.voice_id) : undefined,
        voice_language: lang === "en" || lang === "zh" ? lang : undefined,
        voice_speed: typeof req.voice_speed === "number" ? req.voice_speed : undefined,
      });
      emitOk({
        mp4_path: result.mp4_path,
        external_id: result.external_id,
        cost_usd: result.cost_usd,
      });
      return;
    }
    emitError(`unknown kind: ${req.kind}`);
    process.exit(1);
  } catch (err) {
    if (err instanceof RenderError) {
      console.log(JSON.stringify({ ok: false, error: err.message, provider: err.provider }));
      process.exit(2);
    }
    emitError((err as Error).message);
    process.exit(2);
  }
}

function emitError(msg: string): void {
  console.log(JSON.stringify({ ok: false, error: msg }));
}

// Emit a success result on stdout. Every render kind returns `ok: true` plus
// a kind-specific payload (mp4_path / mp3_path / element_id / ...); this just
// stamps the ok flag so the per-kind blocks only spell out their own fields.
function emitOk(payload: Record<string, unknown>): void {
  console.log(JSON.stringify({ ok: true, ...payload }));
}

async function readStdin(): Promise<string> {
  // Bun's stdin is an async iterable; collect chunks.
  const chunks: Uint8Array[] = [];
  for await (const chunk of Bun.stdin.stream()) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}
