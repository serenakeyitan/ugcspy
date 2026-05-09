import Anthropic from "@anthropic-ai/sdk";
import type { HookSource, RawVideo } from "../types.ts";

export interface HookResult {
  source: HookSource;
  text: string;
  confidence: number;
}

// Multi-source hook extraction per design doc:
// 1. Caption (free, always available, often is the hook)
// 2. Overlay text via vision model (Sonnet 4.6)
// 3. Whisper fallback for first 5 seconds of audio
//
// Confidence is heuristic; Day 4 detection-quality bar will calibrate against the hand-labeled set.
export async function extractHook(
  video: RawVideo,
  anthropicKey: string | undefined,
): Promise<HookResult> {
  const captionHook = pickCaptionHook(video.caption);
  if (captionHook && captionHook.confidence >= 0.7) {
    return captionHook;
  }

  // Overlay vision needs a thumbnail and an Anthropic key. Skip if either missing.
  if (anthropicKey && video.thumbnail_url) {
    try {
      const overlay = await extractOverlay(video.thumbnail_url, anthropicKey);
      if (overlay) return overlay;
    } catch {
      // Vision call failed — fall through. Don't crash the pipeline.
    }
  }

  // Whisper fallback is intentionally not wired up here — it requires downloading the video,
  // which is out of scope for a mock-data MVP. Real implementation lives in Day 1 when we
  // have a live provider returning playable URLs.
  if (captionHook) return captionHook;

  return { source: "none", text: "", confidence: 0 };
}

function pickCaptionHook(caption: string): HookResult | null {
  const trimmed = caption.trim();
  if (!trimmed) return null;
  // First sentence-ish chunk, capped at 120 chars. Most TikTok captions front-load the hook.
  const match = trimmed.match(/^[^.!?\n]{1,120}/);
  const text = match ? match[0]!.trim() : trimmed.slice(0, 120);
  // Confidence heuristic: short, punchy caption = high; long marketing copy = lower.
  const confidence = text.length <= 80 ? 0.85 : 0.55;
  return { source: "caption", text, confidence };
}

async function extractOverlay(thumbnailUrl: string, apiKey: string): Promise<HookResult | null> {
  const client = new Anthropic({ apiKey });
  const response = await client.messages.create({
    model: "claude-sonnet-4-6",
    max_tokens: 200,
    messages: [
      {
        role: "user",
        content: [
          {
            type: "image",
            source: { type: "url", url: thumbnailUrl },
          },
          {
            type: "text",
            text: "Read any on-screen overlay text in this short-form video thumbnail. If there is overlay text, return ONLY that text verbatim. If there is no overlay text or it is unreadable, return the exact string: NO_OVERLAY",
          },
        ],
      },
    ],
  });

  const block = response.content[0];
  if (!block || block.type !== "text") return null;
  const text = block.text.trim();
  if (text === "NO_OVERLAY" || text.length === 0) return null;
  return { source: "overlay", text: text.slice(0, 200), confidence: 0.8 };
}
