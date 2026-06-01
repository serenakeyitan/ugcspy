# Architecture

Why the system looks the way it does. If you want to understand the *what*, read [README.md](README.md). If you want to understand the *why* and *how to extend it*, you're in the right place.

## Design philosophy

**Markdown over TypeScript.** The orchestration logic lives in `SKILL.md` and `prompts/infer_prompt.md` — markdown that Claude reads at runtime. The deterministic heavy-lifting (download, scene detection, OCR, transcription, assembly) lives in `scripts/*.py`. This split is deliberate:

- Methodology evolves faster than implementation. Editing `prompts/infer_prompt.md` to teach the agent a new heuristic is a doc PR, reviewable by anyone, no test plan.
- Code review stays focused on the parts that need it: I/O, schema validation, parsing.
- The skill is portable. Anyone running Claude Code (or another harness with file + vision tools) can run the skill against the same scripts and get the same shape of result.

**No API for stage 4.** Prompt inference happens *inside* the Claude Code session that invoked the skill, by reading keyframes via the Read tool and writing inferred.json via the Write tool. There's no Anthropic SDK call, no API key, no per-clip cost. That's why the skill exists at all — running it is "free" in the sense that you're not buying tokens just to look at frames.

**Schema versioning.** Every breaking change to `recipe.json` ships a new `schemas/recipe.vX.Y.json` and bumps the assembler's `SCHEMA_VERSION`. Old recipes stay valid against their original schema. Today's chain: v0.1 (initial) → v0.2 (`inferred_kind`) → v0.3 (audio block + per-cut transcript) → v0.4 (OCR + paired_prompt_text) → **v0.5 (current)** (caption + hook + tts blocks for the production-blueprint shape).

**Determinism boundary.** Stages 1–7, 9, 10, 11, 12 are deterministic Python — same inputs produce identical outputs. Stage 8 (the agent's per-cut classification + caption + hook + tts identification) isn't — different sessions may produce slightly different outputs on the same video. The eval harness measures this drift and gives us a number to track.

## Pipeline dataflow

```
URL (input)
 │
 ▼
download.py  ──────────────►  recipes/<id>/source.mp4 + source.info.json
 │                                          (yt-dlp; deterministic ID from URL)
 ▼
detect_cuts.py  ───────────►  recipes/<id>/cuts.json
 │   PySceneDetect content or adaptive       [{index, start_sec, end_sec, ...}]
 ▼
extract_audio.py  ─────────►  recipes/<id>/audio.wav
 │   ffmpeg → 16kHz mono PCM (Whisper-native)
 ▼
detect_audio_cuts.py  ─────►  recipes/<id>/silence.json + cuts.json (mutated)
 │   ffmpeg silencedetect; merges into pixel cuts
 │   → splits any pixel cut whose interior contains a silence boundary
 ▼
extract_keyframes.py  ─────►  recipes/<id>/cuts/<i>/{a,b,c}.jpg
 │   ffmpeg @ 10/50/90% of cut duration
 ▼
transcribe.py  ───────────►   recipes/<id>/transcript.json + cuts/<i>/transcript.json
 │   Whisper base model with word timestamps
 │   --pair-with-cuts: per-cut transcripts via word time intervals
 ▼
ocr_title_cards.py  ───────►  recipes/<id>/cuts/<i>/ocr.json
 │   Tesseract on the middle keyframe; classifies title-card vs scene
 │
 ▼ ════════════════════════════════════════════════════════════════════
   AGENT BOUNDARY: Claude (in-session) reads keyframes + transcripts:
     – per-cut classification + caption    → cuts/<i>/inferred.json
       (per prompts/infer_prompt.md)
     – hook identification (5-pattern enum) → hook.json
       (per prompts/identify_hook.md)
     – TTS likely-synthetic + evidence     → tts.json
       (per prompts/identify_tts.md)
 ════════════════════════════════════════════════════════════════════════
 │
 ▼
assemble_recipe.py  ───────►  recipes/<id>/recipe.json (validates v0.5)
 │   Reads cuts.json + per-cut inferred/transcript/ocr/keyframes +
 │   hook.json + tts.json
 │   Pairs title_card cuts → next cut's paired_prompt_text (ground truth)
 │   Lifts per-cut caption from inferred object to cut top level
 ▼
attribute_model.py  ───────►  recipes/<id>/recipe.json (model_attribution filled)
 │   Text mentions in title/transcript/OCR + watermark probe
 ▼
render_html.py  ───────────►  recipes/<id>/recipe.html
 │   Self-contained HTML view; keyframe thumbnails embedded as base64
 │   Reads top-to-bottom as a production blueprint
 ▼
eval_recipe.py  ───────────►  recipes/<id>/eval.json (when ground truth exists)
     Three-channel similarity: token Jaccard, char n-gram Jaccard,
     token recall over ground truth. Mean ∈ [0, 1].
```

## Why each stage is its own script

A single mega-script would be smaller code, but worse for the actual usage pattern:

- **Re-runnable in pieces.** When the agent revises an inferred.json, you re-run assemble_recipe + attribute_model (two seconds), not the whole pipeline.
- **Debuggable.** When transcription is wrong, you can rerun *just* transcribe.py with a different `--model` flag and inspect the diff.
- **Testable.** Each script's tests can mock its one external dependency (yt-dlp, ffmpeg, tesseract, whisper). A unified script would force complex multi-mock test setup.
- **Skippable.** Quick mode literally skips stages 3, 4, 6, 7 by not invoking those scripts. No conditional branching inside one big script.

`run_pipeline.py` is the orchestrator that chains them; it's a thin wrapper, not a re-implementation.

## Why the agent is in the middle of the pipeline, not at the end

The agent could be at the end (after assemble), reading the recipe and producing a "polished" version. We don't do that because:

1. **Per-cut classification is a per-frame task.** Putting the agent in the middle means it reads exactly the 3 keyframes for cut `i` and produces a structured object for cut `i`. No global recipe context to confuse the question. Empirically, this gives sharper prompts.
2. **The assembler can validate.** When the agent runs first, the assembler reads its output and validates against the schema. Schema-incompatible shapes are caught immediately.
3. **Failures isolate.** A bad inference on cut 7 doesn't pollute cuts 0–6. Each `inferred.json` is independent.

## Schema migration policy

When a stage adds a new field that other stages need to read, bump the schema:

1. Add `schemas/recipe.vX.Y+1.json` — copy `vX.Y.json` and add the field. Required fields go in `required`, optional ones (default null) don't.
2. Update `assemble_recipe.SCHEMA_VERSION = "X.Y+1"`.
3. Update `assemble()` to populate the new field.
4. Update tests' expected `schema_version`.
5. Document in CHANGELOG.

Old recipes generated against vX.Y stay valid against vX.Y. We don't break backward compat by editing existing schemas — only add new ones.

## Failure modes the design protects against

- **Network/disk failures during a long run.** Each stage writes its output before the next stage runs. If transcribe.py crashes 9 minutes in, the keyframes from stage 5 are still there. Re-run from stage 6 only.
- **Agent hallucinations.** The structured `inferred_kind` enum (#19) means the agent must explicitly classify the cut; there's no "if you don't know, write something plausible" path. `lumped_cuts`, `non_ai_footage`, `transition`, `unreadable` are all real, recordable outcomes.
- **Pixel-detector limits.** When pixel cuts merge unrelated AI generations (#22's known limit), the audio-cut layer (stage 4) catches the boundary. When that fails too, the agent's `lumped_cuts` classification surfaces the issue rather than fabricating a single prompt for what is really N clips.
- **Schema drift over months.** Upper-bound version pins (#31) prevent silent breakage when a dependency does a major release. Major-version drifts surface as deliberate upgrade decisions.

## What's intentionally not in v1.x

- **Headless / no-Claude-Code mode.** The skill IS the agent loop. If you need a service, that's an Agent SDK app, not a v1 feature.
- **Distributed processing.** A single video goes through the pipeline serially. Multi-video parallelism is an orchestration layer above this codebase.
- **Database / catalog.** Recipes are JSON files on disk. Indexing them is downstream tooling.
- **Image-similarity eval.** `eval_recipe.py` scores prompt text vs prompt text. Whether the regenerated video looks like the original is later phase.
- **Real TTS-model attribution.** We capture the transcript and the agent classifies `likely_synthetic` with evidence, but `tts.model` and `tts.voice_id` are reserved-null. Audio-fingerprint attribution is later phase.
- **Semantic embedding eval.** The deterministic three-channel score is a useful signal without a 200MB sentence-transformers download. Swappable when we want it.

## File map

```
SKILL.md                          # Agent's runtime instruction sheet
                                  # (auto-loaded via .claude/skills/video-recipe/SKILL.md
                                  # symlink for the `/video-recipe` slash command)
ARCHITECTURE.md                   # This file (why)
README.md                         # Quick start (what)
CONTRIBUTING.md                   # PR workflow (how)
METHODOLOGY.md                    # No-setup contributor track (markdown-only)
CHANGELOG.md                      # Version history
RELEASE.md                        # Release-cutting playbook

docs/
  kling-api-notes.md              # Kling API gotchas (lip-sync video_id vs
                                  # task id, voice_id required, trial units)

scripts/
  doctor.py                       # Preflight: ffmpeg, tesseract, deps, network
  download.py                     # Stage 1: yt-dlp
  detect_cuts.py                  # Stage 2: PySceneDetect (content + adaptive)
  extract_audio.py                # Stage 3: ffmpeg → WAV
  detect_audio_cuts.py            # Stage 4: silencedetect + merge
  extract_keyframes.py            # Stage 5: ffmpeg → JPGs
  transcribe.py                   # Stage 6: Whisper + pair-to-cuts
  ocr_title_cards.py              # Stage 7: Tesseract + classifier
  # Stage 8 has no script — the agent does cut classification + caption,
  # hook identification, and TTS likely-synthetic in-session
  assemble_recipe.py              # Stage 9: schema validation + title-card pairing
  attribute_model.py              # Stage 10: heuristic model ID
  render_html.py                  # Stage 11: recipe.json → recipe.html
  eval_recipe.py                  # Stage 12: similarity scoring
  run_pipeline.py                 # Orchestrator (1, 2, 3, 4, 5, 6, 7)
  _log.py                         # Shared logging + SSL self-help helpers

prompts/
  infer_prompt.md                 # Stage 8a: per-cut classification + caption
  identify_hook.md                # Stage 8b: 5-pattern hook enum
  identify_tts.md                 # Stage 8c: synthetic-voice cues

schemas/
  recipe.v0.1.json                # Initial recipe shape
  recipe.v0.2.json                # + inferred_kind
  recipe.v0.3.json                # + audio block + per-cut transcript
  recipe.v0.4.json                # + ocr_text + paired_prompt_text
  recipe.v0.5.json                # + caption + hook block + tts block   (current)

tests/
  test_*.py                       # One file per script (115 tests as of v1.3.0)

.claude/skills/video-recipe/
  SKILL.md                        # Symlink → ../../../SKILL.md (auto-load path)

recipes/                          # Run outputs (gitignored)
  <video-id>/
    source.mp4
    source.info.json
    audio.wav
    cuts.json
    silence.json
    transcript.json
    hook.json                     # written by the agent (stage 8b)
    tts.json                      # written by the agent (stage 8c)
    pipeline_log.json
    cuts/<i>/
      a.jpg b.jpg c.jpg
      inferred.json               # written by the agent (stage 8a)
      transcript.json
      ocr.json
    recipe.json                   # validates against schemas/recipe.v0.5.json
    recipe.html                   # self-contained HTML view (v1.3+)
    eval.json
```
