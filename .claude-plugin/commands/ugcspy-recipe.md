---
description: Reverse-engineer a UGC video into a reproducible recipe (cuts, prompts, hook structure, voiceover) — uses the bundled video-recipe agent
argument-hint: "<video-url-or-tiktok-id>"
---

The user wants to turn a competitor UGC video into a "recipe" — a structured description of how the video was made (cuts, per-clip generation prompts, hook pattern, voiceover, captions, likely models) that another creator or AI agent could use to reproduce it. This is the natural follow-up to `/ugcspy-search`: find a high-performing creator video, then learn how it was constructed.

User arguments: `$ARGUMENTS`

## Routing

If `$ARGUMENTS` is a numeric video id from a previous `/ugcspy-search`, resolve it to a URL first:

```bash
sqlite3 ~/.ugcspy/db.sqlite "SELECT video_url FROM videos WHERE id = $ARGUMENTS;"
```

If `$ARGUMENTS` is already a URL (TikTok, YouTube, Instagram Reels), use it as-is.

If you can't resolve a URL, tell the user "I need a video URL or a numeric id from your most recent /ugcspy-search. Run that first if you haven't."

## Handoff to video-recipe

video-recipe is bundled in this repo at `vendor/video-recipe/`. Its full skill is loaded as a separate plugin skill — but you (this slash command) handle the cd-and-invoke wrapper so the user doesn't have to think about it.

```bash
cd vendor/video-recipe
```

Then follow the video-recipe skill (`.claude-plugin/skills/video-recipe/SKILL.md`) end-to-end on the resolved URL.

### First-run setup for video-recipe

If the user has never run video-recipe before, the python deps probably aren't installed. Run the doctor first:

```bash
cd vendor/video-recipe && python -m scripts.doctor 2>&1
```

If doctor reports missing deps (whisper, torch, ffmpeg, tesseract), walk the user through the install:

- **Python deps**: `cd vendor/video-recipe && pip install -e ".[dev]"` (~2-5 min, downloads whisper + torch).
- **ffmpeg**: macOS → `brew install ffmpeg`; Linux → `sudo apt install ffmpeg`.
- **tesseract**: macOS → `brew install tesseract`; Linux → `sudo apt install tesseract-ocr`.

Once the doctor passes, run the deterministic pipeline:

```bash
cd vendor/video-recipe && python -m scripts.run_pipeline "<resolved-url>" --quick --recipes-root recipes
```

Then follow video-recipe's SKILL.md stages 4 onward — read keyframes, write `inferred.json`, `hook.json`, `tts.json`, re-run `assemble_recipe`. The full SKILL.md is at `.claude-plugin/skills/video-recipe/SKILL.md`.

## What the user gets

A `recipes/<video-id>/recipe.json` (schema v0.5) plus a `recipe.html` they can open in any browser. Includes:

- Cuts (start/end timestamps per shot)
- Per-clip inferred generation prompts (what to type into Veo / Sora / Runway to recreate each shot)
- Opening hook pattern (the first 2-3 seconds, identified as a known pattern type)
- Voiceover transcript + TTS-likelihood classification
- OCR'd title cards
- Heuristic model attribution

This is enough detail for another human creator to copy the structure, or for an AI agent to attempt full reproduction.

## After the recipe

Suggest natural next moves:

- "Want me to fork this into a creator brief instead?" → `/ugcspy-fork` for a simpler hook + beat-sheet brief.
- "Want to recipe another competitor video?" → `/ugcspy-recipe <other-id-or-url>`.
- "Want to see more UGC from the same creator?" → `ugcspy search @<handle>` (user mode).
