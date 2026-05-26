import { loadConfig } from "../lib/config.ts";
import { KlingProvider } from "../render/kling.ts";
import { OpenAITtsProvider } from "../render/openai-tts.ts";
import { RenderError } from "../render/types.ts";

/**
 * Subcommand contract for the Python composer.
 *
 * Stdin (JSON): one of
 *   { "kind": "clip",    "prompt": str, "duration_sec": int, "aspect_ratio"?: str }
 *   { "kind": "tts",     "text": str,   "voice"?: str, "speed"?: float }
 *   { "kind": "lipsync", "video_id": str, "audio_path": str }
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
  // Kling uses TWO keys: access_key + secret_key, both required for HMAC
  // signing each request. KLING_API_KEY (the old single-key env var) is
  // accepted as a fallback alias for KLING_ACCESS_KEY only — secret is
  // still required separately.
  const klingAccess = process.env.KLING_ACCESS_KEY ?? process.env.KLING_API_KEY ?? "";
  const klingSecret = process.env.KLING_SECRET_KEY ?? "";

  try {
    if (req.kind === "clip") {
      const provider = new KlingProvider(klingAccess, klingSecret);
      const result = await provider.generateClip({
        prompt: String(req.prompt ?? ""),
        duration_sec: Number(req.duration_sec ?? 5),
        aspect_ratio: (req.aspect_ratio as "9:16" | "16:9" | "1:1") ?? "9:16",
        first_frame: req.first_frame as string | undefined,
      });
      console.log(
        JSON.stringify({
          ok: true,
          mp4_path: result.mp4_path,
          external_id: result.external_id,
          cost_usd: result.cost_usd,
        }),
      );
      return;
    }
    if (req.kind === "tts") {
      const provider = new OpenAITtsProvider(openaiKey);
      const result = await provider.generateVoiceover({
        text: String(req.text ?? ""),
        voice_id: req.voice as string | undefined,
        speed: req.speed as number | undefined,
      });
      console.log(
        JSON.stringify({
          ok: true,
          mp3_path: result.mp3_path,
          duration_sec: result.duration_sec,
          cost_usd: result.cost_usd,
        }),
      );
      return;
    }
    if (req.kind === "lipsync") {
      const provider = new KlingProvider(klingAccess, klingSecret);
      const result = await provider.lipSyncClip({
        video_id: String(req.video_id ?? ""),
        audio_path: String(req.audio_path ?? ""),
      });
      console.log(
        JSON.stringify({
          ok: true,
          mp4_path: result.mp4_path,
          external_id: result.external_id,
          cost_usd: result.cost_usd,
        }),
      );
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

async function readStdin(): Promise<string> {
  // Bun's stdin is an async iterable; collect chunks.
  const chunks: Uint8Array[] = [];
  for await (const chunk of Bun.stdin.stream()) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}
