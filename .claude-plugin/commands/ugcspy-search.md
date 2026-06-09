---
description: Find competitor UGC on TikTok — third-party creators promoting a brand, or a brand's own posts
argument-hint: "<brand-or-@handle> [--platform tiktok|instagram|all] [--limit N] [--json] [--sort views|recency]"
---

You are running `ugcspy search` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy search $ARGUMENTS
```

## Two search modes (auto-detected from query prefix)

- **Plain word** (e.g. `befreed`, `glossier`, `liquiddeath`) → **hashtag mode**: finds third-party creators promoting the brand. This is the BigSpy-for-UGC default — most users want this.
- **`@handle`** (e.g. `@befreed`) → **user mode**: pulls the brand's OWN posts.
- **`#tag`** → explicit hashtag mode.
- **`--mode user|hashtag`** flag overrides auto-detection.

When a user says "what's working for [brand] on TikTok" or "find creators promoting [brand]", default to hashtag mode (no prefix). When they say "what is [brand] posting", that's user mode (`@brand`).

## Output

If the user did not pass `--json`, the CLI prints a formatted table with brand hashtags highlighted, plus a summary of the most prolific creators when in hashtag mode. Relay the table as-is (it's already formatted).

If they passed `--json`, parse the array and summarize the top 5 in a markdown table. Each row has an `id` field.

## After showing results — offer the natural next steps

Once results are visible, ask the user a one-liner like:

> Want me to:
> - **Fork** one of these into a quick creator brief (hook + beat sheet)? → `/ugcspy-fork <id>`
> - **Recipe** one of these — reverse-engineer it into a reproducible structure with per-clip prompts, cuts, hook pattern, voiceover (heavier, takes a few minutes, useful if it's an AI-generated video you want to replicate)? → `/ugcspy-recipe <id>`

Don't force the question if the user clearly only wanted the list. But if they showed interest in one specific row ("this one looks interesting", "wow #1 has 335K views"), proactively offer both options for that row.

`/ugcspy-fork` is the right answer for a human-shot creator video (lighter, brief-shaped output). `/ugcspy-recipe` is the right answer for AI-generated videos (heavier, recipe.json + per-clip generation prompts).

## Defaults

- Sort: `views` (highest reach first — BigSpy-style)
- Window: last 30 days
- Platform: `all` (the standalone CLI tries TikTok and Instagram; tiktok-oss only supports TikTok so it'll cleanly skip IG)

## Wall time

Hashtag-mode first-run on an active brand takes a few minutes (~5-8 min for BeFreed): browser-free discovery (enumerate every brand hashtag + follow-graph snowball over the tikwm relay) followed by a yt-dlp coverage walk of each discovered creator's full catalog (16-way concurrent by default, `UGCSPY_WALK_CONCURRENCY`) — the walk dominates. Tell the user this BEFORE invoking. User-mode (`@brand`) is much faster (~10-20s, single fetch).

Subsequent searches on the same brand serve from cache instantly. Use `--refresh` for a fresh fetch.

## Setup errors

If the user gets `tiktok-oss: TikTokApi not installed`, suggest `ugcspy install-deps`.

Hashtag mode is browser-free (HTTP via the tikwm relay), so it doesn't need `MS_TOKEN`. If discovery returns few or zero candidates on a brand you'd expect to have UGC, tikwm is likely throttling — wait a bit and retry, and/or raise `UGCSPY_HASHTAG_FEED_DELAY` to widen the gap between feed reads. `MS_TOKEN` only matters for the `user`/`keyword` modes (and the optional Chromium fallback) — see README troubleshooting.
