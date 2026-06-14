---
description: Render a competitor video into a playable reproduction MP4 from its recipe.json (uses paid video-gen + TTS APIs)
argument-hint: "<recipe-id-or-video-id-or-url> [--budget USD]"
---

The user wants to actually GENERATE a reproduction video, not just describe one. This goes one step past `/ugcspy-fork` (creator brief) and `/ugcspy-recipe` (reverse-engineering): we call paid video-gen + TTS APIs to render new MP4s clip-by-clip, then stitch them with ffmpeg into a single `reproduction.mp4`.

User arguments: `$ARGUMENTS`

## Step 0 — Cost expectations + pre-check

This pipeline costs real money on real APIs. Tell the user upfront before doing anything:

- **Per-clip video generation**: depends on the Kling model + mode (`--kling-model` / `--kling-mode`). Default is **`kling-v3` pro** (1080p) — the flagship model (native 4K, native audio, multi-shot) at ~**$0.21/sec** (≈ $1.05 for a 5s clip, $2.10 for 10s). `--kling-mode 4k` for native 4K at ~$0.42/sec (≈$2.10 / $4.20). Drop to `--kling-model kling-v1-6 --kling-mode std` for ~$0.05/sec (≈$0.25 / $0.50) when cost matters more than quality. Runs on the official API (`api-singapore.klingai.com`); override the host with `--kling-base-url`.
- **Native audio (no separate TTS/lipsync)**: `--kling-sound on` makes kling-v3 generate sound + lip-sync inline per cut — for talking-head reproductions this can replace the separate TTS + `--lipsync` passes. Default off.
- **TTS voiceover**: ~$0.01 per typical UGC ad script. Now rendered per-cut and aligned to each cut's spoken-text window (so audio events land at the right clips even if Kling clip lengths shift).
- **Lip-sync warp (opt-in, talking-head only)**: ~$0.084/sec per cut warped, on top of text2video. Add `--lipsync` to the compose call for talking-head reproductions where the mouth needs to match the TTS (口型). Roughly doubles per-clip cost. Skip for greenscreen-kinetic and AI-montage formats — there's no face to sync.
- **Quality knobs (free)**: a default `--kling-negative-prompt` (no blurry/warped-hands/text/watermark artifacts) is applied to every cut; override or disable with `''`. `--kling-cfg-scale 0..1` tightens prompt adherence. Neither costs extra.
- **Total for a typical 6-30s UGC video**: ~$3-15 at the v2-6-pro default; ~$1-6 at v1-6-std. (+~$0.084/sec if `--lipsync`.) Always `--dry-run` first to see the exact estimate.
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

If `$ARGUMENTS` is a numeric video id from a previous `/ugcspy-search`, resolve it first — same rule as `/ugcspy-fork`: the search table's `#` column is a display position, NOT the database id, so re-run the same search with `--json` (cached, instant) and take the Nth element's `id`/`video_url`. Then:

```bash
# With the RESOLVED db id (not the raw table position):
sqlite3 ~/.ugcspy/db.sqlite "SELECT video_url FROM videos WHERE id = <resolved-db-id> LIMIT 1;"
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

# Greenscreen-kinetic / collage — build a multi-image collage backdrop behind each cut:
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 10 --backgrounds pinterest

# Tune the collage grid: 4 (default, 2x2 Canva-collage look) / 1 (single blurred backdrop) / 6 / 9 …
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 10 --backgrounds pinterest --backgrounds-tiles 4

# Cheaper run — older/cheaper model + std mode (lower fidelity, ~1/4 the cost):
cd vendor/video-recipe && python3.11 -m scripts.compose recipes/<videoId> --budget 5 --kling-model kling-v1-6 --kling-mode std
```

**Model & quality:** clips render with `--kling-model` (default `kling-v3`, the flagship — native 4K + native audio + multi-shot) in `--kling-mode` (`std`=720p / `pro`=1080p default / `4k`=native 4K). `--kling-sound on` adds native audio (replaces TTS+lipsync for talking-head). A default `--kling-negative-prompt` steers away from artifacts (blurry, warped hands, text, watermark). `--kling-cfg-scale 0..1` tightens prompt adherence — but note kling-v2.x and kling-v3 don't support it (the render layer drops it; it only applies to kling-v1.x). Higher model+mode costs more per second — see Step 0 and always `--dry-run` to confirm.

Pipeline (in order):
1. For each cut, append the per-cut spoken text to the Kling prompt (L1 — gives diffusion a target for mouth movements, free improvement)
2. Render each cut via Kling text2video (1-3 min per cut)
3. If `--backgrounds pinterest|web`: search for several topic-matching images per cut (Pinterest first, generic web fallback) and composite them as a **multi-image collage grid** behind the cut — `--backgrounds-tiles 4` (default) reconstructs the 2x2 Canva-collage look of greenscreen-kinetic UGC; `1` is a single blurred backdrop. The grid is blurred + darkened so the foreground reads. Pure ffmpeg, no video-gen API cost. Best-effort — a search miss keeps the plain clip; fewer images found than requested → the grid uses whatever was fetched. Auto-gated to backdrop-friendly formats (greenscreen-kinetic / collage / ai-montage) when decode.json is present; pointless for talking-head.
4. For cuts with `transcript`, render per-cut TTS via OpenAI (~1s each)
5. If `--lipsync` AND the cut has audio: POST the cut to Kling `/v1/videos/lip-sync` with the per-cut TTS as base64 audio → returns a warped MP4 with mouth synced to TTS. Falls back silently to the un-warped clip if Kling rejects (e.g. no face detected).
6. Mix per-cut TTS into each clip (when not using lipsync — lipsync already bakes audio in)
7. Concat clips with ffmpeg
8. Output `vendor/video-recipe/recipes/<videoId>/reproduction.mp4`

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
- **`--backgrounds` is best-effort, and Pinterest is flaky.** Pinterest has no public search API and blocks scrapers aggressively, so `--backgrounds pinterest` frequently returns nothing and silently falls back to generic web image search (which is more reliable). A cut whose search finds nothing keeps its plain clip — no error. Treat searched backgrounds as a bonus, not a guarantee, and eyeball the result. Backgrounds only apply to backdrop-friendly formats (greenscreen-kinetic / collage / ai-montage); they're skipped for talking-head even if you pass the flag.
- **The collage is a grid of search results, not a clone of the source's exact images.** `--backgrounds-tiles 4` tiles four *independently-searched* topical photos into a 2x2 grid (matching the source's collage *composition*), then blurs + darkens them as a backdrop. It does not extract and reuse the source video's specific background images — it finds new ones for the same topic. If fewer than the requested number of distinct images are found, the grid fills with whatever was fetched.
- **CamelCase brand pronunciations**: Whisper sometimes transcribes a camelCase brand name as dash-split capitals — a brand spelled "BeBrand" can arrive as "B-BRAND" (verified on a real creator video). The per-cut TTS reads that literally as "B dash brand" — sounds awkward. Edit the cut's `transcript` field in recipe.json to the brand's correct spelling (or your preferred pronunciation) before composing.
- **Sora 2 is being discontinued Sep 2026.** Don't build workflows depending on it. Kling, Runway, Veo, and Luma are the durable options.
- **No AI-disclosure watermark.** compose never burns a watermark and there is no flag to add one — the reproduction is always unlabeled. If you publish AI output where AIGC labeling is required (FTC / EU AI Act / platform ToS), apply the label yourself downstream.
- **Resume protects against mid-pipeline failure.** A Kling 502 on cut 4 of 5 doesn't burn the $4 already spent on cuts 0-3 — compose persists per-cut state to `reproduction/compose_state.json` and skips cached stages on re-run. Refuses to resume when `recipe.json` changed (silent corruption guard); pass `--no-resume` to discard previous progress explicitly.
