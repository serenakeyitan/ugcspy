---
description: Take the format of one video and produce a brief for a different creator to shoot their own version. Cross-video format transfer.
argument-hint: "<target-id-or-url> <source-creator-id-or-url>"
---

The user wants to take the FORMAT of video A (the proven hit they want to copy the structure of) and produce a brief telling creator B how to shoot their own version of that format. Common case: "make a video like @growthwithmya7's purple post, but in @eilisa.befreed's style."

This is structurally different from `/ugcspy-fork` (single video → quick brief) and `/ugcspy-recipe` (single video → AI render plan). Remix needs to decode BOTH videos and produce a brief that fits B's existing style into A's structure.

User arguments: `$ARGUMENTS` — expected format `<target> <source>` where target is the format to copy and source is the new creator whose voice/style/handle to use.

## Step 1 — Parse the two arguments

Split `$ARGUMENTS` on whitespace. Each side can be:
- Numeric search id (look up via SQLite as in `/ugcspy-fork`)
- TikTok URL
- Existing recipe-dir path

```bash
# Resolve target
TARGET="<first arg>"
SOURCE="<second arg>"
# For numeric ids, derive URL via SQLite then extract video id
```

## Step 2 — Decode the TARGET (the format being copied)

```bash
cd vendor/video-recipe && python3.11 -m scripts.decode <target>
```

Read the resulting `decode.json` — this is the STRUCTURE the user wants to copy: format kind, shot list, overlay narrative, brand pitch placement.

## Step 3 — Decode the SOURCE (the creator whose style we're fitting into the format)

```bash
cd vendor/video-recipe && python3.11 -m scripts.decode <source>
```

Read this `decode.json` — this is the CREATOR's existing style we want to respect: format they typically use, how long their videos run, what their typical brand-pitch placement looks like, what their narrative voice sounds like (from `full_narrative`), what brand they typically promote.

## Step 4 — Check style fit, flag mismatches honestly

Before generating the brief, surface any genuine mismatches between target format and source creator style. Real examples:

- Target is `greenscreen_kinetic_listicle` (67s, complex Mya format) but source creator's videos are all `talking_head_floating_card` (7-10s) → tell the user: "Eilisa's existing posts are short single-thought videos; the Mya 60-second greenscreen listicle is a 6-10x format jump. This is doable but it's a creative stretch, not a minor adaptation."
- Target's brand and source's typical brand differ → flag it ("target promotes Notion, source typically promotes BeFreed — make sure you mean to swap the brand")
- Source creator has very few videos in their decoded sample → flag low-confidence on their "typical style"

Don't suppress these honestly. The user can override if they want; they just need to know what they're choosing.

## Step 5 — Write the remix brief in chat

Compose a markdown brief with these exact sections. Use BOTH decode.json files as ground truth — quote actual numbers and overlay text, don't invent details.

**Critical: spoken transcript is the primary source for what the new creator should SAY.** The overlay text is secondary — it's what they BURN INTO the video as visible cues. The new creator's mouth needs to match a script (口型 / lip-sync), and that script comes from `audio_transcript.full_text`, not `full_narrative` (overlay OCR).

If `target.audio_transcript` is missing (e.g. decoded with `--no-audio` or from an older schema), fall back to overlay-only but flag it as ⚠ "no spoken transcript available — the brief below is overlay-only, the actual script the creator needs to read aloud will need to be inferred." Tell the user they can re-decode without `--no-audio` to get the proper transcript.

```
# Remix Brief: <source.uploader> shoots <target.format.kind> for <target.brand_pitch.brand>

## What you're copying from <target.uploader>
- Source video: <target.source_url>
- Format: <target.format.kind> (confidence <target.format.confidence>)
- Duration: <target.duration>s
- Performance: <target.view_count> views
- Brand pitch: <target.brand_pitch.brand> placement is <target.brand_pitch.placement>
- Spoken read (paraphrased): <2-3 sentences extracting the actual argument from target.audio_transcript.full_text — this is what the creator SAYS>
- Overlay relationship: <one sentence: does the overlay summarize the spoken read, punctuate it, or run independently? Pull this from comparing target.audio_transcript.full_text vs cleaned target.full_narrative>

## Who you're shooting it as: <source.uploader>
- Their typical format: <source.format.kind>
- Their typical length: ~<source.duration>s
- Their voice (from existing posts): <2-3 adjectives derived from source.audio_transcript.full_text tone — fall back to source.full_narrative if audio_transcript is missing>
- Their handle to plug: <source.brand_pitch.brand or "the user-specified brand">

## Style fit notes
<if there are mismatches from Step 4, list them here as ⚠ warnings — honest, not optional>

## Shot list — adapted for <source.uploader>
| # | Time | What <source.uploader> SAYS (script) | What they DISPLAY (overlay) | Shot direction |
|---|---|---|---|---|
... one row per shot. The "SAYS" column is a script line for the new creator to deliver aloud — derived from target.audio_transcript.words filtered to this shot's time window (or paraphrased into source's voice for the remix). The "DISPLAY" column is the overlay-text cue. ...

## Script the new creator reads aloud (this is the lip-sync source)
A spoken script in <source.uploader>'s voice, roughly matching the target's word count + pacing. The creator reads this to camera; their mouth movements must match. Length should target ~<target.duration>s when read at normal cadence (typically 2.5-3 words/sec for conversational TikTok delivery).

- Preserves the argument structure of target.audio_transcript.full_text
- Uses vocabulary and sentence rhythm matching <source.uploader>'s existing reads (from source.audio_transcript)
- Lands the brand pitch at <target.brand_pitch.first_mention_pct_of_duration * 100>% through (matching the soft-sell timing of the target)
- Plugs <brand>, not target's brand (unless they're the same)
- Reads naturally aloud — if a sentence sounds awkward when spoken, rewrite it

## Overlay text to burn in (the visible-cue layer)
A separate, shorter version optimized for on-screen reading — usually punchier and more abbreviated than the spoken script. Matches the target's overlay-to-spoken ratio (e.g. Mya's overlay is ~30% of her spoken word count; Eilisa's overlay matches her spoken nearly 1:1).

## Production checklist
<reuse target.reproduction_notes.format_specific_tooling — that's the technique>

Plus any creator-specific notes:
- Setup time estimate: ~15-30 min for talking-head, ~1-2 hours for greenscreen kinetic
- Required tools: TikTok native (greenscreen filter) / Canva (collage background) / CapCut (text overlays)
- Background image suggestions: 4-image Canva collage matching the topic (per the target's pattern)
- 口型 check: have the creator read the spoken script aloud once at normal cadence, time it, confirm it fits the target duration ± 2s before shooting.

## Honest caveats
- Style transfer between drastically different formats (short→long, single-thought→listicle) requires real creative work — this brief is the scaffolding, not a finished script
- The script voice match is best when both videos have audio_transcript; without it the brief is overlay-only and you're inferring spoken cadence from text density (noisy)
- If source creator hasn't posted the target's format before, this is a new content-type experiment for them, not a low-risk repeat
```

## Step 6 — Save the brief

After showing the brief in chat, offer to save it to `vendor/video-recipe/recipes/<target-id>/remix-as-<source-uploader>.md` for the creator to reference later. Default to NOT saving unless asked — most users will just copy/paste from chat.

## Step 7 — (Optional) AI render with the source creator's FACE (image2video, #25)

The brief above is for a *human* creator to shoot. If instead the user wants to **AI-render** the target's format using the **source creator's face locked across every cut**, use the character-consistency path. This is the answer to "use this character from video B in video A's format."

When `/ugcspy-decode` ran on the SOURCE (character) video, it wrote a source-resolution reference keyframe to `recipes/<source-id>/reference.jpg`. Feed that to compose as `--character-ref`:

```bash
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<target-id> \
  --character-ref recipes/<source-id>/reference.jpg \
  --budget 10 --dry-run
```

What this does (per Issue #25):
- Every cut is generated via Kling **image2video** from the SAME reference image — so the creator's identity is consistent across all cuts, instead of plain text2video inventing a different "young woman" per cut.
- The cut PROMPTS come from the TARGET recipe (format/scene). Background is **prompt-driven** in v1 — the target's scene description rides in each cut's prompt; it is NOT locked to a second reference image (that needs Kling's multi-image2video endpoint, a documented v2 follow-up).
- Cost is the same per-second as text2video ($0.10/s std). Always `--dry-run` first and confirm the estimate before the real spend.

Before rendering, sanity-check the reference: `open recipes/<source-id>/reference.jpg`. The v1 extractor grabs a frame ~40% into the source — usually a clean mid-content frame, but if it caught a blurry/averted-face moment, tell the user to either re-decode or hand-pick a frame and pass its path to `--character-ref`. A bad reference produces a bad identity lock.

Then run for real (drop `--dry-run`). Add `--lipsync` only if the target is talking-head and you want mouth-sync to the TTS.

## When NOT to use this

- User just wants to understand one video → `/ugcspy-decode`
- User wants a brief for the original creator → `/ugcspy-fork`
- User wants to render the video via AI → `/ugcspy-reproduce`

## Honest scope

- Voice match is approximate. Whisper captures the spoken script accurately, but tone/cadence/affect comes from the creator's lived voice — Claude can paraphrase but the new creator's delivery is what determines whether the remix lands.
- Format jumps (short → long, single-thought → listicle) are flagged as warnings, not blocked. The user chooses.
- The "shoot it this way" instructions inherit from the target video's tooling hint — accurate for common UGC patterns, but if the target is unusual, the production checklist will be too generic.
- If either side has `audio_transcript: null` (older schema or `--no-audio` decode), the brief degrades to overlay-only and the lip-sync script is inferred from overlay text — flag this prominently to the user, the inference noise will show up in the new creator's read.
