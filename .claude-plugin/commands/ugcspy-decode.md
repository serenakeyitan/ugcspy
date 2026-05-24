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
- `recipes/<video_id>/decode.json` — structured artifact
- `recipes/<video_id>/decode.html` — browser-skimmable view

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

### Narrative (reconstructed from OCR)
> <full_narrative — wrap at 80 chars, italic blockquote>

### Shot list for a new creator
| # | Time | Shot | Overlay |
|---|---|---|---|
| ... one row per shot_list entry ...

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

- The OCR-driven narrative reconstruction is approximate. Heavy kinetic typography loses 20-40% of characters per frame; the chunking algorithm partially compensates but the result is more like "what the narrative roughly says" than verbatim.
- Format classification is heuristic with ~75% accuracy on common UGC patterns. The `signals[]` array is more trustworthy than the `kind` label for ambiguous videos.
- Brand-pitch detection prefers caption-anchored signals (@mentions, campaign-coded hashtags like #brand_NNNN) over generic words to avoid false positives like picking "purple" over "befreed".
