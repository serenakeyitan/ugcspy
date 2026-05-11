# Infer the generation prompt for one cut

You are in the middle of running the `video-recipe` skill. You've just opened the three keyframes for one cut — taken at 10%, 50%, and 90% of the cut's duration. They show the start, middle, and end of that cut.

This file tells you how to classify the cut and what to write.

## Step 1: Classify the cut

Pick exactly one `inferred_kind`:

| `inferred_kind` | When to use | What to write |
|---|---|---|
| `ai_clip` | Frames show an AI-generated scene with describable content. **Default — use this whenever in doubt and the frames have content.** Includes stylized title plates that are themselves AI-generated images (the painted Ghibli landscape under "FELLOWSHIP OF THE RING" is `ai_clip`, not `title_card`). | Full structured object (see Step 2). |
| `title_card` | Frames are a pure text card with no scene to describe — black-on-white explanatory text, prompt readout, lower-third graphic, or end card. The text *itself* is the content. | Null inferred + error noting the on-screen text verbatim if short. |
| `non_ai_footage` | Frames show real-world live-action footage that was filmed, not generated. Talking-head podcast, real human on camera, real-world b-roll, screen recordings. | Null inferred + error describing what you see. |
| `lumped_cuts` | The three frames show **distinct unrelated scenes** — the cut detector merged what should be multiple cuts. Don't pick one; flag for re-detection. | Null inferred + error listing each distinct scene you saw at a/b/c. |
| `transition` | Pure black, pure white, or near-uniform color with no scene. Fade-to-black, scene break, hold frame. | Null inferred + error noting the frame state. |
| `unreadable` | Codec corruption, illegible artifacts, frames you genuinely can't make sense of. Use sparingly — most non-ai_clip cuts fit one of the categories above. | Null inferred + error describing the failure mode. |

## Step 2: When `inferred_kind` is `ai_clip`

Write a single JSON object to `recipes/<video-id>/cuts/<index>/inferred.json` via the Write tool. No prose, no markdown fences, just the JSON:

```json
{
  "subject": "...",
  "action": "...",
  "setting": "...",
  "style": "...",
  "camera": "...",
  "lighting": "...",
  "duration_sec": 4.2,
  "aspect_ratio": "16:9",
  "prompt": "<single-string prompt suitable for any video model>",
  "caption": "WAIT FOR IT" | null
}
```

Use the `duration_sec` from `cuts.json` for this cut — don't try to infer it from the frames.

The assembler will read this file, set `inferred_kind: "ai_clip"`, and embed the object as `inferred` in the final recipe.

### The `caption` field

A **caption** is editorial text that the creator added on top of the AI-generated content in post-production — kinetic typography, hook overlays, lower-thirds, burned-in subtitles. It's a different layer from the visual.

Distinguish between:

- **Caption** (write the text into `caption`): TikTok-style "WAIT FOR IT...", a lower-third "Greg Isenberg / @gregisenberg", a hook overlay "I made $1M with AI". The text appears *on top of* generated content with consistent positioning.
- **Title card text** (NOT a caption — that's a separate `inferred_kind: title_card`): black-on-white explanatory text where text IS the whole frame.
- **Scene text** (NOT a caption, leave `caption: null`): a street sign in the AI-generated city, a phone screen inside the clip, signage on a building. This is part of the generated visual.
- **Watermark / logo** (NOT a caption, leave `caption: null`): small static branding that doesn't change. The model attribution stage handles those.

When in doubt, write `caption: null`. We'd rather miss a real caption than mislabel scene text.

If a caption is animated (kinetic typography that types out word-by-word), write the text it eventually displays.

### What to look at

- **Subject**: who or what is in frame. Be concrete (a 30s woman in a red coat, not "a person").
- **Action**: what changes between the three frames. Static scene? Subject moving? Camera moving? Both?
- **Setting**: where this is. Indoor/outdoor, time of day, identifiable location features.
- **Style**: photoreal, anime, 3D-rendered, claymation, oil-painting, etc. Note any obvious AI tells (overly smooth motion, melting edges, character drift between frames).
- **Camera**: static, dolly-in, pan-left, orbit, handheld, drone-overhead. Infer from how the framing changes.
- **Lighting**: golden hour, overcast, neon, studio softbox, dramatic side-light. Specific.
- **Aspect ratio**: estimate from the frame dimensions (16:9, 9:16, 1:1, 2.39:1).

### Writing the `prompt` field

This is the most important field. Treat it as a standalone instruction for a video generation model — Sora, Veo, Runway, Kling, Pika. Someone should be able to paste it and get a clip resembling the original.

**Good prompt:**
> A 30s woman in a red wool coat walks slowly down a rain-wet Tokyo street at night, neon signs reflected in puddles. Handheld camera follows from behind at chest height. Photoreal, cinematic, shallow depth of field, 24fps motion blur. 4 seconds, 16:9.

**Bad prompt:**
> A woman walks. Cinematic.

Bad prompts produce generic outputs. Good prompts name the subject, the action, the setting, the camera move, the lighting, the style, the duration, the aspect ratio.

## Step 3: When `inferred_kind` is anything else

Write this shape:

```json
{"inferred_kind": "<one of the kinds above>", "error": "<short, structured reason>"}
```

Examples (one per kind, drawn from real dogfood runs):

```json
{"inferred_kind": "title_card", "error": "title card text only — frames read 'STUDIO GHIBLI PRESENTS / The Fellowship of the Ring' on a Ghibli-painted fjord landscape (note: the underlying landscape image is itself AI-generated; classify as ai_clip if you want to capture it as a separate generation)"}
```

```json
{"inferred_kind": "non_ai_footage", "error": "real human host (Greg Isenberg) talking to camera in a home office with bookshelves; motion-graphic 'Exact Workflow' overlay added in post"}
```

```json
{"inferred_kind": "lumped_cuts", "error": "detector merged 3 distinct AI generations: (a) red puff supernova in starfield, (b) glowing-veins human silhouette on fiery red, (c) photoreal astronauts at a meal in a spacecraft. Re-run cut detection at a lower threshold."}
```

```json
{"inferred_kind": "transition", "error": "all three frames are uniformly pure black — fade-to-black or scene-break separator"}
```

```json
{"inferred_kind": "unreadable", "error": "frames show heavy codec corruption / blocky artifacts; cannot identify subject"}
```

## What NOT to do

- Don't guess the originating model. Model attribution is a separate stage.
- Don't invent details not visible in the frames. If you can't tell what's in someone's hand, don't put something there.
- Don't write multiple alternative prompts. One prompt per cut.
- Don't include camera or model brand names ("shot on Arri Alexa", "Sora style"). Describe what you see, not what tool to use.
- Don't write prose around the JSON. Just the JSON object.
- Don't fabricate a prompt for a non-`ai_clip` kind. A categorized null is more honest and more useful than a hallucinated description.
- Don't pick `lumped_cuts` for cuts where the three frames show normal in-shot motion (subject moves, camera pans). Use `lumped_cuts` only when a, b, c look like **unrelated scenes**.
