import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { RenderError, type TtsProvider, type TtsRequest, type TtsResult } from "./types.ts";
import { getRenderTempDir } from "./temp-dir.ts";

/**
 * ElevenLabs Text-to-Speech adapter.
 *
 * Endpoint: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
 * Auth:     xi-api-key header (NOT Bearer; ElevenLabs uses its own header name)
 * Body:     { text, model_id?, voice_settings? }
 * Response: Raw MP3 bytes
 *
 * Schema verified against ElevenLabs Create Speech docs
 * (https://elevenlabs.io/docs/api-reference/text-to-speech/convert) on
 * 2026-06. If the docs change, the mock tests will fail first because
 * they pin the exact URL + headers + body shape.
 *
 * Pricing (June 2026):
 *   - Standard model (eleven_multilingual_v2): $0.30 / 1000 chars
 *   - Free tier: 10,000 chars/month
 *   - Pro/Creator tiers have higher quota at the same per-char cost
 *
 * A typical 6-second UGC voiceover is ~150 chars = $0.045. Comparable to
 * OpenAI TTS for cost; advantage is voice catalog + voice clone support
 * (cloned voices used with the SAME endpoint — just pass the cloned
 * voice_id in the path).
 *
 * Voice clone integration is out of scope here — the user is expected to
 * have created a clone via the ElevenLabs Voice Lab UI and pass the
 * resulting voice_id. We just consume it.
 */
export class ElevenLabsTtsProvider implements TtsProvider {
  readonly name = "elevenlabs-tts";

  // Default model. eleven_multilingual_v2 is the standard high-quality
  // model (best balance of latency + naturalness for short reads).
  // eleven_turbo_v2 is faster but lower-quality; not exposed yet.
  static readonly DEFAULT_MODEL = "eleven_multilingual_v2";

  private static readonly BASE_URL = "https://api.elevenlabs.io";

  // ElevenLabs Standard pricing ($0.30 / 1000 chars), per the public
  // pricing page. Voice clone cost is the SAME per char — only the
  // monthly quota tier differs. If your account is on a different tier
  // your real bill will differ; total_cost will diverge from the actual
  // bill in that case. The real ElevenLabs invoice is authoritative.
  static readonly COST_PER_1K_CHARS_USD = 0.30;

  constructor(private apiKey: string) {}

  async generateVoiceover(req: TtsRequest): Promise<TtsResult> {
    if (!this.apiKey) {
      throw new RenderError(
        "ElevenLabs API key missing. Set ELEVENLABS_API_KEY env var (https://elevenlabs.io → Profile → API Keys).",
        this.name,
      );
    }
    if (!req.voice_id) {
      throw new RenderError(
        "ElevenLabs requires voice_id (no implicit default — voice catalog is per-account). " +
          "Pick a voice from your account at https://elevenlabs.io/app/voice-lab or pass a cloned voice_id.",
        this.name,
      );
    }
    const text = req.text.trim();
    if (!text) {
      throw new RenderError("TTS text is empty", this.name);
    }

    const url = `${ElevenLabsTtsProvider.BASE_URL}/v1/text-to-speech/${encodeURIComponent(req.voice_id)}`;
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "xi-api-key": this.apiKey,
        "Content-Type": "application/json",
        // Accept mp3 explicitly. The API can return wav or others if asked;
        // mp3 is the default but pin it so we're not at the mercy of
        // server-side default changes.
        "Accept": "audio/mpeg",
      },
      body: JSON.stringify({
        text,
        model_id: ElevenLabsTtsProvider.DEFAULT_MODEL,
      }),
    });

    if (!res.ok) {
      // ElevenLabs returns JSON errors with { detail: { status: ..., message: ... } }
      // or sometimes just a plain string. Best-effort surface either shape.
      const body = await res.text();
      throw new RenderError(
        `ElevenLabs TTS failed: ${res.status} ${body.slice(0, 500)}`,
        this.name,
      );
    }

    const outPath = join(getRenderTempDir(), `tts-elevenlabs-${Date.now()}.mp3`);
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length === 0) {
      throw new RenderError(
        "ElevenLabs returned 200 OK but the audio body was empty.",
        this.name,
      );
    }
    writeFileSync(outPath, buf);

    // Cost = $0.30 per 1000 chars. Cheaper voices don't exist in this
    // pricing tier; the cost is uniform per char regardless of voice_id.
    const cost = (text.length / 1000) * ElevenLabsTtsProvider.COST_PER_1K_CHARS_USD;

    // Duration estimate: ElevenLabs averages ~14 chars/sec at default
    // speed (slightly slower than OpenAI's ~15 chars/sec because the
    // voices have more natural pauses). Speed isn't exposed in the
    // request body — ElevenLabs controls cadence via voice_settings
    // instead of a speed param. We don't pass voice_settings in v1.
    const duration_sec = text.length / 14;

    return { mp3_path: outPath, duration_sec, cost_usd: cost };
  }
}
