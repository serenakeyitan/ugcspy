# video-recipe

Reverse-engineer AI-generated videos into reproducible recipes.

Most "AI videos" published online are human-edited montages of AI-generated clips stitched with voiceover, music, and captions. `video-recipe` takes a published video URL and produces a structured `recipe.json` describing how it was made — cuts, per-clip generation prompts, hook structure, voiceover, captions, and likely models — detailed enough that an AI agent could attempt to reproduce the original.

The thesis: published AI videos are de facto open source. The artifact contains enough signal to reconstruct the recipe. At scale, the corpus of recipes becomes training data for the next generation of video agents.

## Quick start

Five commands from `git clone` to your first recipe. Step 5 needs Claude Code; the deterministic stages don't.

```bash
# 0. Clone and enter the repo
git clone https://github.com/serenakeyitan/video-recipe.git
cd video-recipe

# 1. Preflight check — confirms ffmpeg, tesseract, Python deps, network
python -m scripts.doctor

# 2. Install the Python deps (~2-5 min, downloads whisper + torch)
pip install -e ".[dev]"

# 3. Run the deterministic pipeline on a 19s test video, quick mode
python -m scripts.run_pipeline "https://www.youtube.com/watch?v=jNQXAC9IVRw" \
    --quick --recipes-root recipes

# 4. Inspect what came out
ls recipes/jNQXAC9IVRw/
cat recipes/jNQXAC9IVRw/cuts.json

# 5. From here, in Claude Code: invoke the skill on a real video.
#    Claude reads each cut's keyframes, writes inferred.json + hook.json + tts.json,
#    then re-runs assemble_recipe to produce the final v0.5 recipe (recipe.json)
#    plus a self-contained recipe.html you can open in any browser.
```

**Step 5 requires Claude Code** (or another harness with file + vision tools). For methodology-only contributors who only want to improve `prompts/*.md` or `SKILL.md`, see [METHODOLOGY.md](METHODOLOGY.md) — no setup required.

If `python -m scripts.doctor` flags anything, fix it before step 2. Common issues are documented in the doctor output.

## Status

v1.3.0 shipped (HTML recipe view). See [releases](https://github.com/serenakeyitan/video-recipe/releases), [milestones](https://github.com/serenakeyitan/video-recipe/milestones), and [`good first issue`](https://github.com/serenakeyitan/video-recipe/labels/good%20first%20issue) for partner-friendly work.

## How it works

`video-recipe` is a **Claude Code skill**, not a CLI. The orchestration logic lives in [`SKILL.md`](SKILL.md) — markdown that Claude reads and reasons through. Deterministic heavy-lifting (downloading, scene detection, frame extraction, recipe assembly) is delegated to small Python scripts in [`scripts/`](scripts/). Vision-based prompt inference is done by Claude itself, in-session, following the methodology in [`prompts/infer_prompt.md`](prompts/infer_prompt.md).

**No API key, no per-call cost.** Inference happens inside the Claude Code session that's running the skill. This means partners can iterate on methodology by editing markdown — no code review needed for prompt or workflow changes. Code review stays focused on the deterministic scripts.

## Pipeline (v1.3)

```
URL
 └─> [Python] download (yt-dlp)                          → source.mp4 + source.info.json
 └─> [Python] detect_cuts (PySceneDetect)                → cuts.json (pixel cuts)
 └─> [Python] extract_audio (ffmpeg)                     → audio.wav
 └─> [Python] detect_audio_cuts (silencedetect, merge)   → cuts.json (now layered)
 └─> [Python] extract_keyframes (ffmpeg)                 → cuts/<i>/{a,b,c}.jpg
 └─> [Python] transcribe (Whisper, paired to cuts)       → transcript.json + per-cut
 └─> [Python] ocr_title_cards (tesseract)                → per-cut ocr.json
 └─> [Claude]  classify each cut + caption, write inferred.json
 └─> [Claude]  identify hook, write hook.json (5-pattern enum)
 └─> [Claude]  identify TTS likely-synthetic, write tts.json
 └─> [Python] assemble_recipe (validates schema v0.5)    → recipe.json
 └─> [Python] attribute_model (heuristic)                → recipe.json (model_attribution filled)
 └─> [Python] render_html (self-contained, base64 thumbs) → recipe.html
 └─> [Python] eval_recipe (optional, when GT exists)     → eval.json
```

The recipe reads top-to-bottom as a make-this-video brief: title metadata → hook → shot list → voiceover → attribution. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

Optional **quick mode** runs just download → detect_cuts → keyframes → agent inference → assemble. Skips audio + OCR for fast iteration. See [SKILL.md](SKILL.md) for details.

What's still later: full re-generation eval (give recipe to a video model, compare outputs), real TTS-model attribution via audio fingerprint, image-similarity scoring, semantic embedding similarity.

## Usage

In Claude Code, from inside the cloned repo:

```
/video-recipe <url>
```

Claude runs the deterministic scripts (download → cuts → audio → audio-cut merge → keyframes → transcribe → OCR), then classifies each cut and writes the inferred prompt, hook, and TTS classification, then runs the assembler + model-attribution + HTML renderer to produce both `recipes/<video-id>/recipe.json` and a self-contained `recipe.html` you can open in any browser.

### Installing as a Claude Code skill

The skill is registered at `.claude/skills/video-recipe/SKILL.md` (a symlink to the canonical `SKILL.md` at repo root). When you run Claude Code from the repo directory, it auto-loads the skill from this path and `/video-recipe` becomes a slash command in your `/` menu.

If `/video-recipe` doesn't autocomplete in your Claude Code session:

- Confirm you started Claude Code from the repo root (not a parent directory).
- Confirm `.claude/skills/video-recipe/SKILL.md` exists and resolves: `cat .claude/skills/video-recipe/SKILL.md` should print the SKILL.md contents.
- On Windows where symlinks may not work, copy `SKILL.md` to `.claude/skills/video-recipe/SKILL.md` instead of symlinking.
- Fallback invocation: just say "run the video-recipe skill on https://..." — Claude finds and loads SKILL.md from the description field even without the slash command.

**Requirement:** This skill only runs inside Claude Code (or another Claude harness with file + vision tools). There is no headless CLI mode.

### System dependencies

- `ffmpeg` (download, audio extraction, keyframe extraction, silencedetect)
- `tesseract` with English language pack (OCR)
- Python 3.11+

On macOS: `brew install ffmpeg tesseract`. On Debian/Ubuntu: `apt-get install ffmpeg tesseract-ocr fonts-dejavu-core`.

## Repo layout

```
SKILL.md            agentic playbook (the orchestration logic, symlinked at
                    .claude/skills/video-recipe/SKILL.md for skill auto-load)
METHODOLOGY.md      markdown-only contributor track (no toolchain required)
ARCHITECTURE.md     design philosophy + dataflow diagram
scripts/            deterministic Python tools
  doctor.py             preflight check (ffmpeg, tesseract, deps, network)
  download.py           yt-dlp wrapper
  detect_cuts.py        PySceneDetect (content + adaptive)
  extract_audio.py      ffmpeg → mono 16kHz WAV
  detect_audio_cuts.py  silencedetect + merge with pixel cuts
  extract_keyframes.py  3 frames per cut at 10/50/90%
  transcribe.py         Whisper + per-cut pairing
  ocr_title_cards.py    tesseract + title-card classifier
  assemble_recipe.py    schema validation + title-card-to-clip pairing
  attribute_model.py    text mentions + watermark probe
  render_html.py        recipe.json → self-contained recipe.html
  eval_recipe.py        three-channel similarity scoring
  run_pipeline.py       orchestrator (runs all deterministic stages)
  _log.py               shared logging + SSL self-help
prompts/            Claude prompt templates (read by stage 4)
  infer_prompt.md       per-cut classification + caption + AI-clip prompt
  identify_hook.md      5-pattern hook enum + worked examples
  identify_tts.md       synthetic-voice cues
schemas/            JSON Schema versions (recipe.v0.1 .. v0.5)
recipes/            outputs, one folder per analyzed video (gitignored)
tests/              pytest suite (115 tests as of v1.3.0)
```

## Collaboration

See [CONTRIBUTING.md](CONTRIBUTING.md). All work is issue-driven: one issue per problem, comments capture the discussion, a linked PR resolves it. No direct pushes to `main`.

## License

MIT
