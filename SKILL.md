---
name: video-recipe
description: Reverse-engineer an AI-generated video into a reproducible recipe. Given a video URL, produce a structured recipe.json (schema v0.5) plus a self-contained recipe.html describing cuts, per-clip generation prompts, kinetic-typography captions, the opening hook pattern, voiceover transcripts and TTS likely-synthetic classification, OCR'd title cards (with title-card-to-clip pairing), and a heuristic model attribution. Use when the user asks to "analyze this AI video", "extract the recipe", "reverse-engineer this video", or provides a video URL with phrases like "how was this made".
---

# video-recipe

You are reverse-engineering an AI-generated video into a recipe another agent could use to reproduce it.

The input is one URL. The output is `recipes/<video-id>/recipe.json` (schema v0.5) plus a self-contained `recipes/<video-id>/recipe.html` view, plus the intermediate artifacts that justify them (downloaded video, audio track, transcript, cuts list, keyframes, OCR results, per-cut inferred prompts, hook.json, tts.json).

## What an AI-generated video looks like

Treat the input as a **human-edited montage of AI-generated clips**. Almost no AI video published today is one continuous generation — it is 5 to 50 short clips, each generated from a separate prompt, stitched together with voiceover, music, captions, and transitions. Your job is to recover the per-clip prompts and the assembly structure.

Do not assume the input is a single generation. Even when it looks continuous, suspect cuts at:
- changes in camera movement style
- subject or scene changes
- lighting or color grade shifts
- aspect-ratio or resolution shifts mid-video

## The pipeline

Run these stages in order. Stages 1–3 and 5–10 are deterministic Python. Stage 4 is **you** reading frames and writing JSON.

The **full pipeline** runs everything; **quick mode** skips audio + OCR for fast iteration. See "Quick mode" below.

### 1. Download → `source.mp4` + `source.info.json`

```
python -m scripts.download <url> --recipes-root recipes
```

The script picks a deterministic `<video-id>` from the URL (yt-dlp's video id when available, otherwise sha1 of the URL). Record the resolved id for the rest of the run.

If the download fails (geo-block, ToS, login wall), stop and report. Do not try to scrape around it.

### 2. Detect pixel-difference cuts → `cuts.json`

```
python -m scripts.detect_cuts recipes/<id>/source.mp4 recipes/<id>/cuts.json
```

PySceneDetect with content-aware detection. **Pick the threshold for the video category:**

| Category | Recommended | Why |
|---|---|---|
| Slow cinematic / long takes | `content` @ 30+ | Avoid tripping on motion blur within a single shot |
| Default / unknown | `content` @ 27 | PySceneDetect's published default |
| Announcement reel (clip + prompt-card pairs, e.g. Sora demos) | `content` @ 15–18 | Text-card → text-card transitions are subtle |
| Fast TikTok / mixed AI montage | `--detector adaptive --threshold 3.0` | Sub-second AI clips with shared color grade fool the fixed detector |

Sanity-check the output: a typical AI montage has 3–80 cuts. Single-cut output usually means the threshold was wrong — re-run lower. Cuts shorter than ~0.4s are flagged (`flagged_short: true`); do not silently drop.

### 3. Extract audio → `audio.wav` (full mode only)

```
python -m scripts.extract_audio recipes/<id>/source.mp4 recipes/<id>/audio.wav
```

ffmpeg → mono 16 kHz PCM WAV. Skip in quick mode.

### 4. Layer audio cuts on pixel cuts → updated `cuts.json` (full mode only)

```
python -m scripts.detect_audio_cuts recipes/<id>/audio.wav recipes/<id>/silence.json \
  --merge-with recipes/<id>/cuts.json --out recipes/<id>/cuts.json
```

ffmpeg `silencedetect` finds silence boundaries; `merge_silence_into_cuts` splits any pixel cut whose interior contains a silence. **This catches the lumped cuts pixel detection missed** — see #22 / dogfood runs. Skip in quick mode.

### 5. Extract keyframes per cut → `cuts/<i>/{a,b,c}.jpg`

```
python -m scripts.extract_keyframes recipes/<id>/source.mp4 \
  recipes/<id>/cuts.json recipes/<id>/cuts
```

Three frames per cut at 10/50/90% of the cut's duration. Captures start pose, mid-action, end pose — enough for vision to infer camera motion and subject behavior.

### 6. Transcribe voiceover → `transcript.json` + per-cut transcripts (full mode only)

```
python -m scripts.transcribe recipes/<id>/audio.wav recipes/<id>/transcript.json \
  --pair-with-cuts recipes/<id>/cuts.json --cuts-dir recipes/<id>/cuts
```

OpenAI Whisper (`base` model) with word timestamps. The `--pair-with-cuts` flag writes per-cut `transcript.json` files. Skip in quick mode.

### 7. OCR title cards → per-cut `ocr.json` (full mode only)

```
python -m scripts.ocr_title_cards recipes/<id>/cuts recipes/<id>/cuts.json
```

Tesseract on each cut's middle keyframe. Detects pure-text title cards via a confidence + background-uniformity heuristic. Skip in quick mode.

### 7.5. Identify the TTS / voiceover (full mode only — markdown step, not a script)

After transcription you have a script. Now ask: was it spoken by a human or by a TTS model? Follow [`prompts/identify_tts.md`](prompts/identify_tts.md):

1. Read `recipes/<id>/transcript.json` (and listen to `audio.wav` if your harness allows).
2. Look for human tells (filler words, false starts, breath, pacing variation) vs TTS tells (their absence).
3. Write `recipes/<id>/tts.json` with `script`, `language`, `duration_sec`, `likely_synthetic` (bool), `evidence` (1–3 short bullets), and `model: null` / `voice_id: null` (reserved for phase 3 audio-fingerprint attribution).

Skip when there's no voiceover (silent video, music-only).

### 8. Stage 4 — you classify each cut and write `inferred.json`

This stage is **not a script**. You — the agent reading this skill — do the inference yourself, one cut at a time, using your vision capability.

For each cut `i` in `cuts.json`:

1. **Read** the three keyframes via the Read tool: `recipes/<id>/cuts/<i>/{a,b,c}.jpg`. They appear to you visually.
2. **Follow** the methodology in [`prompts/infer_prompt.md`](prompts/infer_prompt.md). It tells you how to classify the cut as one of:
   - `ai_clip` — write a full structured generation prompt
   - `title_card` — pure text card, no scene to describe
   - `non_ai_footage` — real-world filmed footage (talking head, b-roll)
   - `lumped_cuts` — three frames show unrelated scenes; cut detector erred
   - `transition` — pure black/white/uniform color
   - `unreadable` — codec garbage, can't make sense of
3. **Write** to `recipes/<id>/cuts/<i>/inferred.json` via the Write tool.

For `ai_clip`, the file is the full structured object (subject/action/setting/style/camera/lighting/duration_sec/aspect_ratio/prompt). For any other kind, the file is `{"inferred_kind": "<kind>", "error": "<short reason>"}`.

Use the `duration_sec` from `cuts.json` for that cut, not from the frames.

The `prompt` field on `ai_clip` cuts must be a **standalone, model-agnostic generation prompt** — written so it could be pasted into Sora, Veo, Runway, Kling, or Pika and produce a clip resembling the original.

**Process cuts one at a time**, in order. Don't batch. For long videos (50+ cuts), subset to the most informative stretch and tell the user. **If you find yourself writing many `lumped_cuts` errors, stop and re-run stage 2 with a lower threshold or `--detector adaptive`.**

### 8.5. Identify the hook — also you, also markdown-driven

After stage 4 (per-cut classification) but before stage 9 (assembly), spend one focused look at the opening 1–3 seconds of the video and identify the hook.

Follow [`prompts/identify_hook.md`](prompts/identify_hook.md). It tells you to:

1. Read keyframes + transcripts for cuts whose `start_sec < 3.0`.
2. Pick one of 5 patterns: `question`, `claim`, `shock_cut`, `transformation_tease`, `pattern_break`. Or `null` when no clear hook exists.
3. Write `recipes/<id>/hook.json` with the structured object (or `{"hook": null}`).

The hook is a property of the video, not a single cut. Most videos have one; some don't.

### 9. Assemble `recipe.json`

```
python -m scripts.assemble_recipe <source_url> recipes/<id>/
```

Reads cuts.json + per-cut `inferred.json`, `transcript.json`, `ocr.json`, plus `hook.json` and `tts.json` from the recipe root. Pairs each title-card cut with the immediately following cut: that next cut's `paired_prompt_text` carries the OCR'd ground-truth prompt. Validates against `schemas/recipe.v0.5.json`.

### 10. Heuristic model attribution → `recipe.json` updated in place

```
python -m scripts.attribute_model recipes/<id>/
```

Scans title/description/transcript/OCR/inferred prompts for known model names (Sora, Veo, Kling, Runway, Pika, Luma) plus a per-cut watermark probe. Populates `model_attribution` block.

### 11. Render HTML view → `recipe.html`

```
python -m scripts.render_html recipes/<id>/recipe.json
```

Reads `recipe.json` and writes a self-contained `recipe.html` next to it. Keyframe thumbnails embedded as base64 data URLs — the file is portable, no external assets, openable in any browser. Reads top-to-bottom as a production blueprint: header → hook → shot list → voiceover → attribution.

### 12. Eval (optional, only when ground-truth exists)

```
python -m scripts.eval_recipe recipes/<id>/recipe.json
```

For each cut where both `paired_prompt_text` (ground truth from #15 OCR) and `inferred.prompt` exist, scores three-channel similarity. Writes `eval.json`. Use this to track recipe quality over time — runs with no `paired_prompt_text` produce an empty summary, which is fine.

## Quick mode

If the user wants a fast pass — or the video has no useful audio/text — run only stages **1, 2, 5, 8, 9**:

```
download → detect_cuts → extract_keyframes → AGENT inference → assemble_recipe
```

You'll get a recipe with `audio: null`, no `transcript`, no `ocr_text`, no `paired_prompt_text`, but full per-cut prompts. That's a complete v0 recipe.

Quick mode is right for: short demo reels, videos without narration, fast iteration. **Default to full mode** when the user provides a real video URL — the audio + OCR signals are what unlock cut splitting and ground-truth pairing.

## What "done" looks like

A run is successful when:
1. `recipe.json` validates against the v0.5 schema and `recipe.html` is written alongside it
2. Every cut has 3 keyframes and an `inferred_kind`; `ai_clip` cuts have a non-empty `prompt` and (when applicable) a `caption`
3. Top-level `hook` block is populated (or explicitly `null` for videos without a clear hook)
4. Top-level `tts` block has `likely_synthetic` plus at least one piece of evidence (or is null for silent videos)
5. The cut count and total duration match the source video (within rounding)
6. `model_attribution.primary_model` is set when there's any signal
7. The user can open `recipe.html` and understand, cut by cut, how to reproduce the video

## What you must NOT do

- Do not edit the deterministic scripts mid-run to "fix" their output. If a script is wrong, stop and open an issue.
- Do not fabricate prompts for non-`ai_clip` cuts. Use the structured null kinds.
- Do not skip stage 4's classification step (don't auto-write `ai_clip` for everything). Each cut deserves an honest look.
- Do not run stage 11 (eval) and treat low scores as failures — low scores when there's no ground truth, or when ground truth is a 14-word seed prompt and the inferred is a 200-word descriptor, are correct outcomes.

## Determinism note

Stages 1–3, 5–7, 9, 10, and 11 are deterministic Python. Stage 4 (your classification + prompt writing) is not — different sessions may produce slightly different inferred prompts. This is expected and tracked by the eval harness.

## Failure modes to watch for

- **Single-cut detection on an obvious montage**: threshold too high, re-run with `--threshold 20` or lower.
- **100+ cuts on a stable scene**: detector tripping on motion blur, threshold too low.
- **Many `lumped_cuts` classifications**: pixel detector missed boundaries — try `--detector adaptive --threshold 3.0` and re-run cuts + audio merge + keyframes.
- **A cut's frames are blank or unreadable**: write `{"inferred_kind": "transition" or "unreadable", "error": "..."}` and move on. Do not fabricate.
- **Source download is a slideshow of stills**: not an AI video. Stop and report.
- **Whisper certificate errors on first run**: set `SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")` once.
- **Tesseract path errors on macOS**: handled internally (we cwd to the image's parent and pass a relative filename).
