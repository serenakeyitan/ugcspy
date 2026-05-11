# Changelog

All notable changes to `video-recipe`. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.0] - 2026-05-11

HTML recipe view. Every full-pipeline run now produces a self-contained `recipe.html` alongside `recipe.json`.

### Added

- **`scripts/render_html.py`** (#62) — reads `recipes/<id>/recipe.json` and writes `recipes/<id>/recipe.html`, a single self-contained HTML page rendering the recipe as a top-to-bottom production blueprint:
  - **Header** — title, schema version, duration/resolution/fps, source link
  - **Hook** — pattern badge (color-coded by enum), caption, voiceover quote, first-visual
  - **Shot list** — one row per cut: keyframe thumbnail (base64-embedded), kind badge, duration, prompt or null-reason, caption (yellow box), voiceover slice (green box), paired-prompt (purple box)
  - **Voiceover (TTS)** — synthetic/real badge, full script, evidence bullets
  - **Model attribution** — primary model badge, candidate chips, evidence
- Keyframes embedded as base64 data URLs — the `.html` file is fully portable, no external assets, emailable.
- HTML escaping on all user-controlled fields (prompts, captions, voiceover) to prevent XSS from a malicious recipe.
- **`run_pipeline.py --with-assemble`** now invokes render_html as the final stage, so the orchestrator's dev/test path produces recipe.html automatically.

### E2E checkpoint

Rendered the cached BeFreed TikTok recipe (425KB HTML). Hook, 7-cut shot list with thumbnails, TTS "Real human speech" badge with 4 evidence bullets, attribution all render correctly. Captions, voiceovers, paired prompts, per-cut metadata all visible.

### Tests

115 tests passing locally and in CI (was 100 in v1.2.0). New render_html test file covers full recipe rendering, base64 keyframe embedding, null/full hook variants, TTS real + synthetic badges, attribution with candidates, null-kind cuts with errors, missing keyframes (no crash), HTML escaping XSS guard, --out custom path, parametrized field round-trips.

---

## [1.2.0] - 2026-05-10

Onboarding pass. A new partner now goes from `git clone` to first recipe in <10 minutes with concrete diagnostics when something's wrong.

### Added

- **`scripts/doctor.py`** (#45) — preflight check, runs in <5s, reports 11 environment checks (Python version, ffmpeg, tesseract + English language pack, all Python packages importable, certifi resolvable, disk space, network reachable). One-line remediation hint per failure. Exits 0 when green, 1 when any required check fails. 14 unit tests.
- **`scripts/_log.is_ssl_certificate_error()` + `print_ssl_self_help()`** (#46) — `download.py` and `transcribe.py` now catch SSL cert verification errors (common on managed-cert systems) and print concrete remediation (`export SSL_CERT_FILE=...`) instead of a raw stack trace. Walks the cause/context chain, loop-safe.
- **README Quick Start** (#47) — top-of-README section walking from `git clone` to first recipe in 5 commands. Pipeline diagram updated for v1.1's caption / hook / tts agent stages.
- **METHODOLOGY.md** (#48) — markdown-only contributor track documentation. Lists what you can edit without any toolchain (`SKILL.md`, `prompts/*.md`, all docs), the 3-step PR flow from the GitHub web UI, what the reviewer looks for, a worked example. Explicit list of what you do NOT need (Python, ffmpeg, tesseract, pytest, API keys).
- **`.claude/skills/video-recipe/SKILL.md`** (#49) — symlink to canonical `SKILL.md` at the canonical Claude Code skill path so `/video-recipe <url>` actually appears as a slash command. README documents the install path, three diagnostics, and Windows fallback.

### Triaged

Five good-first-issues opened for partners (#56-#60). Two markdown-only (suitable for METHODOLOGY.md track), three Python (small + well-scoped):

- #56 — `flagged_short` warning in per-cut classification prompt (markdown-only)
- #57 — make `tts.evidence` required when `tts != null` (schema v0.6 bump)
- #58 — `run_pipeline --with-assemble` warning at startup
- #59 — per-recipe `README.md` written alongside `recipe.json`
- #60 — TikTok cooking-video example for `transformation_tease` hook (markdown-only)

### E2E checkpoint

Fresh `git clone` → `python -m scripts.doctor` (all 11 green) → `python -m scripts.run_pipeline ... --quick` (3 stages successful) → `recipes/jNQXAC9IVRw/cuts.json` materialized. Confirmed `.claude/skills/video-recipe/SKILL.md` symlink survives clone and resolves to the canonical SKILL.md.

### Tests

91 tests passing locally and in CI (was 77 in v1.1.0; added 14 doctor tests + SSL self-help tests).

---

## [1.1.0] - 2026-05-10

Production-blueprint pass. Schema **v0.5**. The recipe now reads top-to-bottom as a make-this-video brief: title metadata → hook → shot list → voiceover → attribution.

### Added

Four new fields, three new agent steps, no new deterministic scripts:

- **Per-cut `caption`** (#37) — editorial overlay text the creator added in post (kinetic typography, hook overlays, lower-thirds, burned-in subtitles). Distinct from OCR'd title cards (which are creator-written prompts, captured as `paired_prompt_text` on the next cut) and from scene text inside the AI-generated visual. The agent fills it during the existing stage 4 loop; the assembler lifts it from `inferred.json` to the cut's top level.
- **Top-level `hook` block** (#38) — the opening 1-3 seconds that earns watch time. New agent step (stage 8.5) reads cuts/transcripts in the first ~3s and picks one of 5 patterns (`question` / `claim` / `shock_cut` / `transformation_tease` / `pattern_break`) or `null`. Schema enforces the enum. Driven by `prompts/identify_hook.md`.
- **Top-level `tts` block** (#39) — voiceover script + `likely_synthetic` boolean call (with 1-3 evidence bullets). New agent step (stage 7.5) reads the transcript (and optionally listens to audio) for human tells (filler words, false starts, breath sounds, pacing variation) vs TTS tells (their absence). `model` and `voice_id` are reserved-null for phase 3 audio-fingerprint attribution. Driven by `prompts/identify_tts.md`. The legacy `audio` block stays alongside for back-compat (deprecated in v1.2).

### Schema v0.5 changes

- Added per-cut: `caption: string | null`
- Added top-level: `hook: object | null` with `pattern` enum, `duration_sec`, `spans_cuts`, `text`, `voiceover`, `first_visual`
- Added top-level: `tts: object | null` with required `script`, `language`, `duration_sec`, `likely_synthetic`; reserved `model`, `voice_id`, `evidence`
- Recipe field order is now production-blueprint shape: schema_version → metadata → hook → cuts → tts → audio (legacy) → model_attribution

### Closed without action

- **#40** (rename `cuts` → `shot_list` + reorder fields) — cosmetic rename rejected as wontfix. The reorder was already accomplished as a side-effect of #38/#39; the rename would break every existing recipe.json validating against v0.4 for no reader-facing benefit. A future v1.2 can revisit if real reader feedback says `shot_list` is meaningfully clearer.

### Tests

77 tests passing locally and in CI. New per-PR coverage:

- caption lifted/null/non-ai cases (3)
- hook embedded/null-marker/invalid-pattern cases (3)
- tts embedded/null-marker/missing-required-field cases (3)

### E2E checkpoints validated

- #37 caption: cached PJ Ace recipe — agent's "POV: opening the door" caption correctly lifts to cut 4 top level
- #38 hook: cached PJ Ace recipe — agent's `question` pattern with the actual opening line ("What if I can show you the exact workflow")
- #39 tts: cached PJ Ace recipe — Greg's voice classified `likely_synthetic: false` with concrete evidence ("speeds up on 'hundreds of millions of views'", "false start: 'AI ad man man himself'", "no comma-precise pauses")
- v1.1.0 final: full pipeline + agent stage 4 + hook + tts on the 19s "Me at the zoo" video, schema-valid v0.5 recipe with all four pillars (hook=null honest, shot_list with non_ai_footage classification, tts non-synthetic with evidence, model_attribution null)

---

## [1.0.0] - 2026-05-09

First production-ready release. Schema **v0.4**.

### Pipeline complete

11 stages now wired end-to-end:

1. **download** (yt-dlp): URL → `source.mp4` + `source.info.json`
2. **detect_cuts** (PySceneDetect): pixel-difference cuts with `--detector content` or `--detector adaptive`
3. **extract_audio** (ffmpeg): mono 16 kHz PCM WAV
4. **detect_audio_cuts** (ffmpeg silencedetect): silence boundaries layered onto pixel cuts; splits cuts the pixel detector merged
5. **extract_keyframes** (ffmpeg): 3 frames per cut at 10/50/90% of cut duration
6. **transcribe** (Whisper base): word-level timestamps, paired to cuts
7. **ocr_title_cards** (tesseract): per-cut text extraction + title-card classifier
8. **agent** (Claude in-session): classify each cut as one of `ai_clip` / `title_card` / `non_ai_footage` / `lumped_cuts` / `transition` / `unreadable`; for `ai_clip`, write a paste-into-Sora-ready prompt
9. **assemble_recipe**: schema-validated `recipe.json` + title-card-to-clip pairing
10. **attribute_model**: heuristic Sora/Veo/Kling/Runway/Pika/Luma identification (text mentions + watermark probe)
11. **eval_recipe** (optional): three-channel similarity score against ground-truth prompts when present

### Added

- `scripts/run_pipeline.py` orchestrator with `--quick` mode, `--threshold`, `--detector`, `--with-assemble` flags
- `scripts/_log.py` shared logging helpers; every stage emits `[stage] start`/`done in Xs` to stderr
- `recipes/<id>/pipeline_log.json` records per-stage timing + ok/error
- `paired_prompt_text` field on cuts: when a `title_card` cut precedes a clip, the OCR'd text becomes the next cut's ground-truth prompt
- `eval.json` summary: mean/min/max similarity across evaluable cuts, plus per-cut breakdown
- `model_attribution` block: `primary_model`, `confidence`, `evidence`, `per_cut`, `candidates`
- `ARCHITECTURE.md` covering design philosophy, dataflow, schema migration policy

### Changed

- Schema bumped: v0.1 → v0.2 → v0.3 → v0.4
- All third-party deps now have upper-bound version pins (yt-dlp <2027, scenedetect <0.8, opencv <5, ffmpeg-python <1, jsonschema <5, Pillow <12, pytest <9, ruff <1)
- CI installs ffmpeg + tesseract-ocr + fonts-dejavu-core via apt
- `pyproject.toml` version → 1.0.0

### v0.4 schema additions
- per-cut: `ocr_text`, `ocr_confidence`, `paired_prompt_text`

### v0.3 schema additions
- top-level: `audio` block (`language`, `transcript_path`, `duration_sec`)
- per-cut: `transcript`

### v0.2 schema additions
- per-cut: required `inferred_kind` enum

---

## [0.0.1] - 2026-05-08

Initial scaffold and v0 milestone.

### Added

- Issue-driven repo structure with PR + issue templates
- `SKILL.md` agentic playbook
- `prompts/infer_prompt.md` template
- `scripts/{download,detect_cuts,extract_keyframes,assemble_recipe}.py`
- `schemas/recipe.v0.1.json`
- CI workflow (ruff + pytest)
- 3 reference videos run end-to-end (Sora announcement, PJ Ace × Greg Isenberg interview, Ghibli LOTR) — all produced schema-valid recipes
