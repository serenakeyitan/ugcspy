import Anthropic from "@anthropic-ai/sdk";
import { FORMAT_TAGS, type FormatTag, type RawVideo } from "../types.ts";

const TAG_LIST = FORMAT_TAGS.join(", ");

const CLASSIFIER_PROMPT = `You classify short-form video into exactly ONE format tag from this closed list:
${TAG_LIST}

Definitions:
- GRWM: get ready with me (camera follows a personal grooming/styling routine)
- POV: point-of-view framing, often with on-screen "POV: ..." text
- talking_head: creator speaks directly to camera, no other format
- product_demo: hands-on use of a product
- unboxing: opening packaging
- tutorial: step-by-step how-to
- before_after: explicit before/after reveal
- voiceover_broll: voiceover over b-roll, no on-camera person
- duet_stitch: TikTok duet or stitch
- other: doesn't cleanly fit any of the above

Respond with ONLY the tag, no other text.`;

export async function classifyFormat(
  video: RawVideo,
  anthropicKey: string | undefined,
): Promise<FormatTag | null> {
  // No key → no classification. Don't fake it.
  if (!anthropicKey) return null;

  const client = new Anthropic({ apiKey: anthropicKey });
  const userPayload = `Caption: ${video.caption}\nPlatform: ${video.platform}\nLikes: ${video.like_count} / Views: ${video.view_count}\n\nClassify this video.`;

  const response = await client.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 20,
    system: CLASSIFIER_PROMPT,
    messages: [{ role: "user", content: userPayload }],
  });

  const block = response.content[0];
  if (!block || block.type !== "text") return null;
  const tag = block.text.trim() as FormatTag;
  return (FORMAT_TAGS as readonly string[]).includes(tag) ? tag : "other";
}
