import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { RenderError, type TtsProvider, type TtsRequest, type TtsResult } from "./types.ts";
import { getRenderTempDir } from "./temp-dir.ts";

/**
 * OpenAI TTS — $15 per 1M characters for the standard model, $30 for HD.
 * A typical 6-second UGC voiceover is ~150 chars = ~$0.002. Negligible.
 *
 * We default to the "alloy" voice — neutral, slightly young, matches the
 * cadence of typical UGC creator content. Users can override per-call.
 *
 * Docs: https://platform.openai.com/docs/api-reference/audio/createSpeech
 */
export class OpenAITtsProvider implements TtsProvider {
  readonly name = "openai-tts";
  // Stock voices — these are the names OpenAI exposes. "alloy" is the
  // safest default for UGC; "nova" sounds more energetic if needed.
  static readonly VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"] as const;

  constructor(private apiKey: string) {}

  async generateVoiceover(req: TtsRequest): Promise<TtsResult> {
    if (!this.apiKey) {
      throw new RenderError(
        "OpenAI API key missing. Set OPENAI_API_KEY env var or run `ugcspy init` to add it to config.",
        this.name,
      );
    }
    const voice = req.voice_id ?? "alloy";
    const text = req.text.trim();
    if (!text) {
      throw new RenderError("TTS text is empty", this.name);
    }

    const res = await fetch("https://api.openai.com/v1/audio/speech", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${this.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "tts-1",
        voice,
        input: text,
        speed: req.speed ?? 1.0,
        response_format: "mp3",
      }),
    });
    if (!res.ok) {
      throw new RenderError(
        `OpenAI TTS failed: ${res.status} ${await res.text()}`,
        this.name,
      );
    }

    const outPath = join(getRenderTempDir(), `tts-${Date.now()}.mp3`);
    const buf = Buffer.from(await res.arrayBuffer());
    writeFileSync(outPath, buf);

    // $15 per 1M chars (standard model)
    const cost = (text.length / 1_000_000) * 15;
    // Duration estimate: typical English TTS ≈ 15 chars/sec at speed 1.0
    const duration_sec = text.length / 15 / (req.speed ?? 1.0);

    return { mp3_path: outPath, duration_sec, cost_usd: cost };
  }
}
