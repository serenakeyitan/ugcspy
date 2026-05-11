# Identify the TTS / voiceover layer

You are running the `video-recipe` skill. After stages 6 (transcribe) and 4 (per-cut classification), you have:

- `recipes/<id>/audio.wav` — extracted audio track
- `recipes/<id>/transcript.json` — full Whisper transcript with word timestamps
- `recipes/<id>/cuts/<i>/transcript.json` — per-cut spoken text

This file tells you how to classify the **voiceover** as likely-synthetic-TTS vs likely-real-human-speech.

We're not doing model attribution yet (that's phase 3 — needs an audio fingerprint corpus). We just want one structured boolean: did a TTS service produce this voiceover, or did a human read it?

## Step 1: When to skip

Skip this step entirely (write `{"tts": null}` or just don't write tts.json) when:

- The video has no voiceover (silent or music-only)
- The transcript is empty or near-empty (≤3 words across the whole video)

Move on to assembly.

## Step 2: Listen + read

Open `audio.wav` if your harness gives you audio playback (you can also infer from the transcript alone — the textual cues often suffice).

Look at the **full transcript** (read `recipes/<id>/transcript.json`). Notice:

- **Phrasing**: Is it grammatically perfect with no false starts? Real humans say "uh", "um", trail off, restart sentences. TTS doesn't.
- **Pacing rhythm**: Does every sentence have the same cadence? Real humans vary tempo — fast through the unimportant parts, slow on the punchlines. TTS keeps an even pace unless explicitly directed.
- **Filler words**: "you know", "like", "I mean", "basically" are human tells. Their *absence* in casual content is a TTS tell.
- **Punctuation prosody**: TTS pauses precisely at commas and periods, every time. Humans skip commas when speaking quickly.
- **Breath sounds**: Real humans breathe between phrases. Pure TTS doesn't (some modern TTS models add fake breath — that's evidence too).
- **Emphasis**: Real humans emphasize specific words for meaning. TTS emphasis is uniform unless prompted.

## Step 3: Decide and write

Write `recipes/<id>/tts.json`:

```json
{
  "script": "<full transcript text — concatenated words>",
  "language": "en",
  "duration_sec": 47.3,
  "likely_synthetic": true,
  "evidence": [
    "no filler words in 47s of speech",
    "even pacing across all 8 paragraphs",
    "no breath sounds at sentence boundaries"
  ],
  "model": null,
  "voice_id": null
}
```

`model` and `voice_id` stay `null` — phase 3 will fill them in via audio fingerprint. v1.1 just exposes the structure.

`evidence` is your reasoning: 1-3 short bullet points naming the cues you used. Be specific. Don't write "sounds like AI" — write "no breath sounds at sentence boundaries".

## Examples

**PJ Ace × Greg Isenberg interview**: Greg laughs, talks over himself, says "uh", "you know", varies pace dramatically. Real human.

```json
{
  "script": "What if I can show you the exact workflow for how to come up with AI video ads I get hundreds of millions of views? ...",
  "language": "en",
  "duration_sec": 19.44,
  "likely_synthetic": false,
  "evidence": [
    "natural laughter and excitement variation",
    "mid-sentence false start: 'AI video ads I get'",
    "uneven pacing: speeds up on 'hundreds of millions of views'"
  ],
  "model": null,
  "voice_id": null
}
```

**A typical AI explainer YouTube short** with a perfectly-paced voiceover: zero filler, comma-precise pauses, no breath:

```json
{
  "script": "...",
  "language": "en",
  "duration_sec": 60.0,
  "likely_synthetic": true,
  "evidence": [
    "zero filler words across 60s",
    "comma-precise pauses, no overlap or trail-off",
    "no audible breath at any sentence boundary"
  ],
  "model": null,
  "voice_id": null
}
```

## What NOT to do

- Don't guess the model. `model: null` is correct in v1.1.
- Don't classify `likely_synthetic: true` just because the speaker is articulate. Many real humans are clean speakers. The tells are the *absence* of human imperfections.
- Don't write tts.json when there's no voiceover. Empty transcript → skip.
- Don't paraphrase the script — copy from the transcript.
- Don't write prose around the JSON.
