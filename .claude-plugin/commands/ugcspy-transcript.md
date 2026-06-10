---
description: Hook + spoken transcript for top UGC videos (or one specific video), with talking/non-talking filtering from the audio
argument-hint: "<brand | video-id | tiktok-url> [--top N] [--talking | --non-talking] [--json]"
---

You are running `ugcspy transcript` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy transcript $ARGUMENTS
```

## What it does

For each selected video: downloads ONLY the audio track (~1-2MB), transcribes it with Whisper, and prints the **spoken hook** (the first real speech line — the 3-second retention line), the **full transcript**, and a **TALKING / NON-TALKING** classification. Results cache in SQLite — a video is transcribed once, ever.

The classification is trustworthy on music: Whisper hallucinates plausible lyrics over music beds, and the bridge blanks those segments (no_speech_prob gate) before counting words. "NON-TALKING" means montage/overlay content with no real speech, not "Whisper heard a song".

## Selection forms

- **Brand/handle query** (`befreed`, `#pingoai`, `@jacob.befreed`) — top N by views (default 3, `--top N`) from the CACHED search. Run `/ugcspy-search <brand>` first if there's no cache.
- **DB id** (from `search --json` output's `id` field) — one video. The search table's `#` column is a display position, NOT the id — resolve via `--json` first, same rule as `/ugcspy-decode`.
- **TikTok URL** — one video, works even if it was never searched (ad-hoc, not cached).

## Filters

- `--talking` — only videos with real speech (≥8 spoken words, not just a music bed)
- `--non-talking` — only music/overlay videos with no real speech

With a filter, the CLI scans DOWN the ranked list transcribing until it finds N matches, bounded at max(4×N, 12) transcriptions. It reports how many candidates were scanned — relay that to the user so a capped scan isn't read as "the whole roster".

## Wall time

~10-40s per UNCACHED video (audio download + local Whisper); cached videos are instant and marked `cached`. Warn the user before a filtered scan on a fresh brand — it can transcribe up to 12 videos (~2-5 min).

## Prerequisites

Needs `ugcspy install-deps --with-audio` (Whisper, one-time ~1.5GB) and `ffmpeg` on PATH. The error messages name the exact fix — relay them verbatim.

## Output

Without `--json`: one section per video — rank, creator, views, TALKING/NON-TALKING badge, hook (spoken, or caption fallback for non-talking videos), full transcript. Relay it as-is; for long transcripts summarize after showing the hooks.

With `--json`: array of `{id, external_id, author_handle, view_count, video_url, talking, audio_kind, lexical_word_count, duration_sec, language, hook, transcript, from_cache}`.

## Natural next steps after showing results

- "Decode how the #1 was made" → `/ugcspy-decode <id>`
- "Brief a creator to shoot this format" → `/ugcspy-remix <target> <source>`
- "Quick brief" → `/ugcspy-fork <id>`
