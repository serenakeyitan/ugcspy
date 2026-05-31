---
description: Deeply decode the production technique of one UGC video — what shooting style, what overlay tooling, where the brand pitch lands. Writes decode.json + decode.html.
argument-hint: "<video-id-from-search | tiktok-url | recipe-dir-path>"
---

You're decoding the production technique of a single UGC video so the user can understand HOW it was made (and reproduce the pattern with a different creator). This is different from `/ugcspy-recipe` (which is aimed at AI reproduction of AI-generated montages) — `/ugcspy-decode` works for human-shot videos too and explicitly captures: which editing tool/template, what the overlay narrative is, where the brand pitch lands (soft 软广 vs hard sell), and a shot-list a human creator could shoot from.

User arguments: `$ARGUMENTS`

## Step 1 — Resolve the input

The user can pass three things:
- **Numeric search id** (e.g. `42`) — look up the video_url in SQLite, then derive the TikTok video id from the URL
- **TikTok URL** (e.g. `https://www.tiktok.com/@growthwithmya7/video/7637483885516918030`)
- **Existing recipe-dir path** (e.g. `vendor/video-recipe/recipes/7637483885516918030`)

For a numeric id:

```bash
sqlite3 ~/.ugcspy/db.sqlite "SELECT video_url FROM videos WHERE id = $ARGUMENTS LIMIT 1;"
```

Extract the trailing `/video/<number>` from the URL.

## Step 2 — Run the decoder

```bash
cd vendor/video-recipe && python3.11 -m scripts.decode <url-or-recipe-dir>
```

Tell the user upfront this takes ~30 seconds (downloads if needed, extracts frames, OCRs every second, detects cuts, classifies format). It writes:
- `recipes/<video_id>/decode.json` — structured artifact (includes `reference_image` — the keyframe filename for character-consistent remixing)
- `recipes/<video_id>/decode.html` — browser-skimmable view
- `recipes/<video_id>/reference.jpg` — a source-resolution keyframe for Kling image2video character consistency (#25). When OpenCV is available, decode samples several frames across the video's middle 20–80% band and **auto-picks the sharpest one with the clearest, most-centered face** (variance-of-Laplacian + Haar face detection); without OpenCV it falls back to the 40%-mark frame. `/ugcspy-remix` feeds this as `--character-ref` so the creator's face stays consistent across AI-generated cuts. Skip with `--no-reference`.

## Step 3 — Render the summary in chat

Read decode.json and present it to the user as a focused breakdown. Don't dump the raw JSON. Format like this (adapt the fields based on what's actually there):

```
## Decoded: <source_meta.title (first 80 chars)>

@<uploader> · <duration>s · <view_count> views · <aspect_ratio>

### Format
**<format.kind>** (confidence <format.confidence>)
- <format.signals[0]>
- <format.signals[1]>
- ...

### Brand pitch
- Brand: **<brand_pitch.brand>** (detected via <brand_pitch.brand_source>)
- First mention at: <first_mention_at_sec>s (<first_mention_pct_of_duration * 100>% through the video)
- Placement: <brand_pitch.placement>

### Spoken narrative (Whisper transcript)
If `audio_transcript` is present in decode.json, show the spoken transcript FIRST — this is what the creator actually says to camera, the primary content for most UGC formats. Format as a clean italic blockquote of `audio_transcript.full_text`:

> <audio_transcript.full_text — wrap at 80 chars, italic blockquote>

Note the language (`audio_transcript.language`) and word count (`len(audio_transcript.words)`) underneath in dim text.

If `audio_transcript` is missing (older decode.json, or `--no-audio` was passed), skip this block entirely and tell the user "audio transcript not captured — re-run decode without `--no-audio` to get it" instead.

### On-screen overlay text (reconstructed from OCR)
This is what the creator BURNS INTO the video as visible text — usually a summary or supporting cue, not the full read. Quote the cleaned `full_narrative` (the HTML renderer already scrubs OCR noise; in chat you can quote the cleaned version directly).

> <full_narrative — wrap at 80 chars, italic blockquote>

When both spoken AND overlay narratives exist, briefly note whether they agree or diverge (e.g. "overlay is a 5-bullet summary of the 60-second spoken read"). This relationship matters a LOT for /ugcspy-remix.

### Shot list for a new creator
| # | Time | Shot | What they SAY (spoken) | What they DISPLAY (overlay) |
|---|---|---|---|---|
| ... one row per shot_list entry; for the "say" column, pull the spoken-transcript words whose start_sec falls inside the shot's time window. If no audio_transcript, use "(no audio)" ... |

### How to shoot this
<reproduction_notes.format_specific_tooling>

### Honest caveats
- <reproduction_notes.honest_caveats[0]>
- <reproduction_notes.honest_caveats[1]>
```

Always link the user to the HTML at the end:

```
Full decode → vendor/video-recipe/recipes/<video_id>/decode.html
```

## Step 4 — Offer the natural follow-ups

After the summary, suggest one of these depending on what the user seems to want next:

- **If they want to make a similar video with a different creator:** suggest `/ugcspy-remix <this-id> <other-creator-id>` — that command takes both videos and writes a hand-able brief for the new creator.
- **If the video is AI-generated and they want to actually render their own version:** suggest `/ugcspy-recipe` then `/ugcspy-reproduce` for the AI-render path.
- **If they want a quick creator brief for this format:** suggest `/ugcspy-fork <id>` for the lighter-weight brief.

## When NOT to use this

- If the user just wants a quick brief without the full production breakdown → use `/ugcspy-fork` (faster, no decode step)
- If the user wants to actually render the video with AI → use `/ugcspy-recipe` + `/ugcspy-reproduce`

## Honest scope

- The OCR-driven overlay reconstruction is approximate. Heavy kinetic typography loses 20-40% of characters per frame; the chunking algorithm partially compensates but the result is more like "what the overlay roughly says" than verbatim.
- The Whisper-driven spoken transcript is generally accurate for clear English speech but degrades on heavy background music, multiple speakers, or strong accents. Word-timestamps are accurate to ~200ms. Defaults to whisper-base; bump with `--whisper-model small` (or higher) on tricky audio.
- Format classification is heuristic with ~75% accuracy on common UGC patterns. The `signals[]` array is more trustworthy than the `kind` label for ambiguous videos.
- Brand-pitch detection prefers caption-anchored signals (@mentions, campaign-coded hashtags like #brand_NNNN) over generic words to avoid false positives like picking "purple" over "befreed". (Brand detection currently runs against overlay text only — extending it to spoken transcript is a future improvement.)
