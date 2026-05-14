---
description: Render a competitor video into a playable reproduction MP4 from its recipe.json (uses paid video-gen + TTS APIs)
argument-hint: "<recipe-id-or-video-id-or-url> [--budget USD]"
---

The user wants to actually GENERATE a reproduction video, not just describe one. This goes one step past `/ugcspy-fork` (creator brief) and `/ugcspy-recipe` (reverse-engineering): we call paid video-gen + TTS APIs to render new MP4s clip-by-clip, then stitch them with ffmpeg into a single `reproduction.mp4`.

User arguments: `$ARGUMENTS`

## Step 0 — Cost expectations + pre-check

This pipeline costs real money on real APIs. Tell the user upfront before doing anything:

- **Per-clip video generation**: ~$0.60 (5s) or ~$1.20 (10s) via Kling 3.0
- **TTS voiceover**: ~$0.01 per typical UGC ad script
- **Total for a typical 6-30s UGC video**: $1-6
- **Default budget cap**: $5 (override with `--budget 10`)

Required API keys (the compose pipeline will fail with a clear error if missing):
- `KLING_API_KEY` — from kling.ai → API console
- `OPENAI_API_KEY` — from platform.openai.com → API keys

Tell the user which keys are missing and where to get them BEFORE running the compose. Don't burn 5 minutes of their time before telling them they need a key.

```bash
# Check what's set
[ -n "$KLING_API_KEY" ] && echo "✓ KLING_API_KEY" || echo "✗ KLING_API_KEY missing — get one at https://kling.ai"
[ -n "$OPENAI_API_KEY" ] && echo "✓ OPENAI_API_KEY" || echo "✗ OPENAI_API_KEY missing — get one at https://platform.openai.com/api-keys"
```

## Step 1 — Resolve the input to a recipe directory

The user may pass:
- A video id from `/ugcspy-search` results (numeric, looks up via SQLite)
- A TikTok URL directly
- An existing recipe directory path (e.g. `vendor/video-recipe/recipes/7630138...`)

For ids and URLs, you need to first ensure `/ugcspy-recipe` has run on this video. If `vendor/video-recipe/recipes/<id>/recipe.json` doesn't exist, run `/ugcspy-recipe` first then come back.

```bash
# If user passed an id:
sqlite3 ~/.ugcspy/db.sqlite "SELECT video_url FROM videos WHERE id = $ARGUMENTS LIMIT 1;"
# Extract the trailing video ID number from the URL
# Then check: ls vendor/video-recipe/recipes/<videoId>/recipe.json
```

## Step 2 — Dry-run first

ALWAYS run dry-run before the real compose. Shows estimated cost without spending anything:

```bash
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --dry-run --budget 10
```

Surface the cost estimate to the user and confirm before spending. Output looks like:

```
[compose] 1 cuts, estimated cost: $0.60
[compose] dry-run; no API calls made.
```

If the user says no, stop here. If yes:

## Step 3 — Compose

```bash
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 10
```

This will (in order):
1. Render each cut via Kling (1-3 min per cut due to polling)
2. Render TTS via OpenAI (~1s)
3. Concat clips with ffmpeg
4. Mix voiceover audio in
5. Output `vendor/video-recipe/recipes/<videoId>/reproduction.mp4`

Wall time: roughly `cut_count × 90 seconds` for the render polling. A 1-cut video is ~90s; a 5-cut AI montage is ~7 min.

## Step 4 — Show the result

After compose completes, tell the user where reproduction.mp4 lives and offer to:
- Open it: `open vendor/video-recipe/recipes/<videoId>/reproduction.mp4`
- Compare side-by-side with the original: open both source.mp4 and reproduction.mp4
- Iterate: edit recipe.json prompts and re-run compose for cuts where the render didn't capture the source

## Honest caveats to mention

- **Human-shot UGC won't reproduce well.** If recipe.json says `is_ai_generated: false` and `format.kind` is `talking_head_*`, the compose pipeline will refuse with a clear message — AI renders of real creators look uncanny. Suggest `/ugcspy-fork` (brief a real creator) instead.
- **AI-montage videos work best.** Kinetic typography over AI b-roll with voiceover is the format the recipe schema was designed for.
- **Quality varies wildly per prompt.** Kling and similar models still produce wonky hands, weird motion, and inconsistent characters across cuts. Expect iteration — first try is rarely the keeper.
- **Sora 2 is being discontinued Sep 2026.** Don't build workflows depending on it. Kling, Runway, Veo, and Luma are the durable options.
