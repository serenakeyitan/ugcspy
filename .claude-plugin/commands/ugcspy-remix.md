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

```
# Remix Brief: <source.uploader> shoots <target.format.kind> for <target.brand_pitch.brand>

## What you're copying from <target.uploader>
- Source video: <target.source_url>
- Format: <target.format.kind> (confidence <target.format.confidence>)
- Duration: <target.duration>s
- Performance: <target.view_count> views
- Brand pitch: <target.brand_pitch.brand> placement is <target.brand_pitch.placement>
- Narrative shape (paraphrased): <2-3 sentences extracting the actual argument from target.full_narrative>

## Who you're shooting it as: <source.uploader>
- Their typical format: <source.format.kind>
- Their typical length: ~<source.duration>s
- Their voice (from existing posts): <2-3 adjectives derived from source.full_narrative tone>
- Their handle to plug: <source.brand_pitch.brand or "the user-specified brand">

## Style fit notes
<if there are mismatches from Step 4, list them here as ⚠ warnings — honest, not optional>

## Shot list — adapted for <source.uploader>
| # | Time | What <source.uploader> does | Overlay text |
|---|---|---|---|
... one row per shot derived from target.shot_list, with the shot description rewritten in source creator's voice/cadence ...

## Overlay narrative — written in <source.uploader>'s voice
A 50-100 word rewrite of target.full_narrative that:
- Preserves the listicle structure / hook pattern of the target
- Uses vocabulary and sentence rhythm that matches source's existing posts
- Lands the brand pitch at <target.brand_pitch.first_mention_pct_of_duration * 100>% through (matching the soft-sell timing of the target)
- Plugs <brand>, not target's brand (unless they're the same)

## Production checklist
<reuse target.reproduction_notes.format_specific_tooling — that's the technique>

Plus any creator-specific notes:
- Setup time estimate: ~15-30 min for talking-head, ~1-2 hours for greenscreen kinetic
- Required tools: TikTok native (greenscreen filter) / Canva (collage background) / CapCut (text overlays)
- Background image suggestions: 4-image Canva collage matching the topic (per the target's pattern)

## Honest caveats
- Style transfer between drastically different formats (short→long, single-thought→listicle) requires real creative work — this brief is the scaffolding, not a finished script
- The narrative voice match is approximate; source creator's actual reads will be tighter than what we can reconstruct from OCR
- If source creator hasn't posted the target's format before, this is a new content-type experiment for them, not a low-risk repeat
```

## Step 6 — Save the brief

After showing the brief in chat, offer to save it to `vendor/video-recipe/recipes/<target-id>/remix-as-<source-uploader>.md` for the creator to reference later. Default to NOT saving unless asked — most users will just copy/paste from chat.

## When NOT to use this

- User just wants to understand one video → `/ugcspy-decode`
- User wants a brief for the original creator → `/ugcspy-fork`
- User wants to render the video via AI → `/ugcspy-reproduce`

## Honest scope

- Voice match is approximate. OCR captures the WORDS the creator's overlay said, but tone/cadence comes from the creator's lived voice — Claude can approximate but won't sound 100% like them.
- Format jumps (short → long, single-thought → listicle) are flagged as warnings, not blocked. The user chooses.
- The "shoot it this way" instructions inherit from the target video's tooling hint — accurate for common UGC patterns, but if the target is unusual, the production checklist will be too generic.
