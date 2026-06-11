---
name: ugcspy
description: Use this skill when the user asks about competitor UGC on TikTok or Instagram Reels — for example "find creators promoting [brand]", "what's the UGC around [brand]", "who's posting about [brand]", "what is @[brand] posting", "track [brand] on TikTok", or any request to discover third-party creators promoting a brand or to research a brand's own social posts. Invoke `ugcspy` via the Bash tool.
---

# ugcspy — competitor UGC intelligence

`ugcspy` is a CLI installed on the user's machine. Run it via the Bash tool. The headline use case is **finding third-party creators promoting a brand** — sponsored UGC, organic mentions, the @creators worth reaching out to.

## When to use

The default question is "find me UGC creators promoting BRAND" — that's plain-word search:

- "Find creators promoting BeFreed" → `ugcspy search befreed` (hashtag mode, third-party)
- "Who's posting about Liquid Death?" → `ugcspy search liquiddeath`
- "Show me all the Glossier UGC from the last week" → `ugcspy search glossier --sort recency --days 7`
- "Top 20 creators talking about Notion" → `ugcspy search notion --limit 20`

Use `@handle` only if the user explicitly wants the BRAND'S OWN posts (rare in BigSpy-style research):

- "What is Glossier's account posting?" → `ugcspy search @glossier`

Other commands:

- "Turn this video into a brief I can hand to a creator" → `/ugcspy-fork <url>`
- "How was this video made? What technique? What overlay?" → `/ugcspy-decode <url>` (deep production breakdown — format, OCR'd narrative, brand-pitch placement, shot list. Writes decode.json + decode.html.)
- "Make a video like X but with creator Y" / "same format different creator" / "copy this structure for @other-creator" → `/ugcspy-remix <target> <source>` (decodes both, produces a hand-able brief that fits B's style into A's structure)
- "Reverse-engineer this video into a reproducible recipe (cuts, prompts, hook structure)" → `/ugcspy-recipe <url>` — uses the bundled video-recipe agent at `vendor/video-recipe/`. Heavier than `/ugcspy-fork` (requires ffmpeg + whisper + the full Python pipeline) but produces a structured `recipe.json` that an AI agent could use to attempt reproduction.
- "Slack-alert me when a competitor breaks out" (only if explicitly asked) → see Watch + daemon below

Skip if the user is asking about **paid ads** (different tool — TikTok Creative Center / Facebook Ad Library).

## Commands

### Search (the core command)

```bash
ugcspy search <query> [--platform tiktok|instagram|all] [--limit N] \
                     [--sort views|recency] [--mode user|hashtag|keyword] [--json]
```

Auto-detects mode from query prefix:
- `befreed` (no prefix) → hashtag mode = third-party creators promoting BeFreed
- `@befreed` → user mode = BeFreed's own account posts
- `#befreed` → explicit hashtag mode
- `--mode keyword` (explicit flag, never auto-detected) → broad niche/topic discovery — the corpus a script writer browses, NOT limited to videos tagging a brand. E.g. "find skincare routine UGC" → `ugcspy search --mode keyword "skincare routine"`. Pure HTTP, zero setup.

Returns videos ranked by **views descending** (default — BigSpy-style highest-reach-first) or recency. Hashtag mode includes a `Creator` column showing each row's actual poster, plus a "most prolific creators" summary at the bottom (the SMM insight: who's posting about this brand most often).

Precision filter: hashtag results only keep videos whose caption carries the brand via `#brand`, `#brand_NNNN` (campaign codes), `#brandapp`, `@brand`, or the plain-text brand token at word boundaries (e.g. "reading with befreed is so clutch"). This rejects unrelated videos that TikTok's hashtag endpoint over-matches.

**First-run wall time is a few minutes for an active brand (~5-8 min for BeFreed).** The CLI runs two stages: browser-free discovery (enumerate every brand hashtag + follow-graph snowball over the tikwm relay), then a yt-dlp coverage walk of each discovered creator's full catalog (16-way concurrent, `UGCSPY_WALK_CONCURRENCY`) — the walk is where the time goes. Tell the user this is expected before running. Subsequent searches on the same brand serve from cache instantly. Use `--refresh` to force a re-fetch (same time).

### Fork (video → creator brief)

The standalone CLI has **no `fork` command** — brief generation is plugin-only. Route to `/ugcspy-fork <video-id-or-url>`: it generates the brief (hook variations, format notes, beat sheet, b-roll, CTA) in chat using the user's Claude Code subscription, with an optional save to `~/.ugcspy/briefs/`.

### Transcript (hook + spoken narrative + talking/non-talking)

```bash
ugcspy transcript <brand|video-id|tiktok-url> [--top N] [--talking | --non-talking] [--json]
```

When the user asks for "the hook and transcript of the top 3 videos", "what does the #1 video say", or wants to filter "talking" vs "non-talking" (montage/music) content — route here (or `/ugcspy-transcript`). It audio-only-downloads each video, Whisper-transcribes locally, prints the spoken hook (first real speech line) + full transcript, and classifies TALKING vs NON-TALKING. Music beds can't fake it: Whisper's hallucinated lyrics are blanked via the no_speech_prob gate before words are counted. Transcripts cache in SQLite — each video is transcribed once (~10-40s), instant after. Filters scan down the ranked list bounded at max(4×N, 12) transcriptions and report the scan count. Needs `install-deps --with-audio` only (ffmpeg is bundled; the Whisper model pre-downloads at install); the errors name the fix.

### Watch + daemon (optional — Slack breakout alerts)

Power-user feature, not part of the core flow. Most users live in `search`. Surface this only when the user explicitly asks for monitoring/alerts/automation.

```bash
ugcspy watch add <handle> --slack-webhook <url> [--threshold 2.0] [--platform tiktok|instagram]
ugcspy watch list
ugcspy watch remove <id>
ugcspy daemon --once       # poll once
ugcspy daemon              # loop every 6h
```

Cold-start gate: alerts stay in `warming_up` for 7 days AND until ≥5 videos exist in the trailing window. Don't expect alerts on day 1.

## First-run / onboarding

If the user says they're new to ugcspy, can't run it, or asks how to install it, route them to `/ugcspy-setup` — it walks through every install step (deps, config, verification, bot-detection fix) in one prompt. Don't piecemeal it.

If the user has already run setup and just needs a config refresh, `ugcspy init` is the wizard for that. It writes `~/.ugcspy/config.json` (chmod 0600) with two things: their scraper choice (default `tiktok-oss`, free) and an optional default Slack webhook. **No Anthropic API key needed** — brief generation runs in this Claude Code chat using the user's existing subscription, and caption-based hooks come straight from the scraper output.

## Tips

- The `--json` flag on `search` is the right tool when the user wants a list of URLs to feed into another action.
- After a search, the user can `/ugcspy-fork <id>` on any video — that prompt generates the brief in this chat directly, no API key, no per-brief cost.
- `watch list` is cheap; run it before adding a watch to avoid duplicates.
- If a `daemon --once` run shows `warming_up`, that is expected behavior, not a bug.
- Hooks in `search` output are caption-derived (free, deterministic). Format tags are NOT auto-classified by the standalone CLI — if the user wants format tagging, classify in this chat after the search returns.

## Repo

https://github.com/serenakeyitan/ugcspy — read [docs/DESIGN.md](https://github.com/serenakeyitan/ugcspy/blob/main/docs/DESIGN.md) for the full spec.
