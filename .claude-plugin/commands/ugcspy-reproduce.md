---
description: Render a competitor video into a playable reproduction MP4 from its recipe.json (uses paid video-gen + TTS APIs)
argument-hint: "<recipe-id-or-video-id-or-url> [--budget USD]"
---

The user wants to actually GENERATE a reproduction video, not just describe one. This goes one step past `/ugcspy-fork` (creator brief) and `/ugcspy-recipe` (reverse-engineering): we call paid video-gen + TTS APIs to render new MP4s clip-by-clip, then stitch them with ffmpeg into a single `reproduction.mp4`.

User arguments: `$ARGUMENTS`

## Step 0 — Cost expectations + pre-check

This pipeline costs real money on real APIs. Tell the user upfront before doing anything:

- **Per-clip video generation**: ~$0.60 (5s) or ~$1.20 (10s) via Kling 3.0 (`$0.10/sec` std)
- **TTS voiceover**: ~$0.01 per typical UGC ad script. Now rendered per-cut and aligned to each cut's spoken-text window (so audio events land at the right clips even if Kling clip lengths shift).
- **Lip-sync warp (opt-in, talking-head only)**: ~$0.084/sec per cut warped, on top of text2video. Add `--lipsync` to the compose call for talking-head reproductions where the mouth needs to match the TTS (口型). Roughly doubles per-clip cost. Skip for greenscreen-kinetic and AI-montage formats — there's no face to sync.
- **Total for a typical 6-30s UGC video**: $1-6 (without lipsync) / $2-12 (with lipsync)
- **Default budget cap**: $5 (override with `--budget 10`)
- **Resume by default**: compose persists per-cut state. If a run fails on cut 4 of 5, the next run skips cuts 0-3 (no re-billing). Pass `--no-resume` to start fresh. Recipe-hash protects against silent corruption when recipe.json changes between runs.
- **No AI-disclosure watermark**: the reproduction.mp4 is always unlabeled — compose never burns a watermark and there is no flag to add one. Label the output yourself at publish time if you need to (FTC + EU AI Act + platform ToS may require AIGC labeling depending on where you post).

Required API keys (the compose pipeline will fail with a clear error if missing):
- `KLING_ACCESS_KEY` + `KLING_SECRET_KEY` — Kling's API uses HMAC-signed JWTs and needs both halves. Get the pair at https://klingai.com/dev (the developer portal — NOT the same as the web-app account at klingai.com).
- `OPENAI_API_KEY` — from https://platform.openai.com/api-keys

Tell the user which keys are missing and where to get them BEFORE running the compose. Don't burn 5 minutes of their time before telling them they need a key.

```bash
# Check what's set
[ -n "$KLING_ACCESS_KEY" ] && echo "✓ KLING_ACCESS_KEY" || echo "✗ KLING_ACCESS_KEY missing — get the access+secret pair at https://klingai.com/dev"
[ -n "$KLING_SECRET_KEY" ] && echo "✓ KLING_SECRET_KEY" || echo "✗ KLING_SECRET_KEY missing — same place as above"
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

ALWAYS run dry-run before the real compose. Shows estimated cost broken down by stage without spending anything:

```bash
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --dry-run --budget 10
```

If the source is talking-head and you want lip-sync (口型 — mouth matches TTS), add `--lipsync` to BOTH the dry-run and the real call. Without `--lipsync` the dry-run will under-estimate by ~$0.084/sec per cut.

Surface the cost estimate + breakdown to the user and confirm before spending. Output looks like:

```
[compose] 2 cuts, estimated cost: $1.84
  - text2video 2 cuts: $1.00
  - TTS 99 chars: $0.0015
  - lipsync 2 cuts: $0.84
[compose] dry-run; no API calls made.
```

If the user says no, stop here. If yes:

## Step 3 — Compose

```bash
# Default — no lipsync (greenscreen-kinetic, AI-montage, anything where face isn't the focal point):
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 10

# Talking-head with lip-sync — adds Kling lipsync warp per cut so mouth matches TTS:
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 10 --lipsync
```

Pipeline (in order):
1. For each cut, append the per-cut spoken text to the Kling prompt (L1 — gives diffusion a target for mouth movements, free improvement)
2. Render each cut via Kling text2video (1-3 min per cut)
3. For cuts with `transcript`, render per-cut TTS via OpenAI (~1s each)
4. If `--lipsync` AND the cut has audio: POST the cut to Kling `/v1/videos/lip-sync` with the per-cut TTS as base64 audio → returns a warped MP4 with mouth synced to TTS. Falls back silently to the un-warped clip if Kling rejects (e.g. no face detected).
5. Mix per-cut TTS into each clip (when not using lipsync — lipsync already bakes audio in)
6. Concat clips with ffmpeg
7. Output `vendor/video-recipe/recipes/<videoId>/reproduction.mp4`

Wall time: roughly `cut_count × 90 seconds` for text2video, plus another ~90 seconds per cut for lipsync if `--lipsync`. A 5-cut talking-head with lipsync is ~15 min.

## Step 4 — Show the result

After compose completes, tell the user where reproduction.mp4 lives and offer to:
- Open it: `open vendor/video-recipe/recipes/<videoId>/reproduction.mp4`
- Compare side-by-side with the original: open both source.mp4 and reproduction.mp4
- Iterate: edit recipe.json prompts and re-run compose for cuts where the render didn't capture the source

## Honest caveats to mention

- **Human-shot UGC won't reproduce well.** If recipe.json says `is_ai_generated: false` and `format.kind` is `talking_head_*`, the compose pipeline will refuse with a clear message — AI renders of real creators look uncanny. Suggest `/ugcspy-fork` (brief a real creator) instead.
- **AI-montage videos work best.** Kinetic typography over AI b-roll with voiceover is the format the recipe schema was designed for.
- **Quality varies wildly per prompt.** Kling and similar models still produce wonky hands, weird motion, and inconsistent characters across cuts. Expect iteration — first try is rarely the keeper.
- **Lip-sync (`--lipsync`) only works on clear-face cuts.** Kling rejects cuts with no detectable face (e.g. background-only montage, hands-only product shots) with code `1006` "no face detected". Our compose pipeline catches that and silently keeps the un-warped clip rather than aborting — but you'll spend ~$0.084/sec on cuts that fall back. If your source is purely b-roll, omit `--lipsync` entirely.
- **Lipsync source-video freshness window**: Kling's lipsync only accepts source clips ≤30 days old. We just generated them ourselves so this is fine, but if you re-run lipsync standalone weeks later on cached cuts, it'll fail.
- **`B-FREED` and other camelCase brand pronunciations**: Whisper sometimes transcribes BeFreed as "B-FREED" (verified on real Mya video). The per-cut TTS reads that literally as "B dash freed" — sounds awkward. Edit the cut's `transcript` field in recipe.json to "BeFreed" (or your preferred pronunciation) before composing.
- **Sora 2 is being discontinued Sep 2026.** Don't build workflows depending on it. Kling, Runway, Veo, and Luma are the durable options.
- **No AI-disclosure watermark.** compose never burns a watermark and there is no flag to add one — the reproduction is always unlabeled. If you publish AI output where AIGC labeling is required (FTC / EU AI Act / platform ToS), apply the label yourself downstream.
- **Resume protects against mid-pipeline failure.** A Kling 502 on cut 4 of 5 doesn't burn the $4 already spent on cuts 0-3 — compose persists per-cut state to `reproduction/compose_state.json` and skips cached stages on re-run. Refuses to resume when `recipe.json` changed (silent corruption guard); pass `--no-resume` to discard previous progress explicitly.
