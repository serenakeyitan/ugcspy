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
- "Slack-alert me when a competitor breaks out" (only if explicitly asked) → see Watch + daemon below

Skip if the user is asking about **paid ads** (different tool — TikTok Creative Center / Facebook Ad Library).

## Commands

### Search (the core command)

```bash
ugcspy search <query> [--platform tiktok|instagram|all] [--limit N] \
                     [--sort views|recency] [--mode user|hashtag] [--json]
```

Auto-detects mode from query prefix:
- `befreed` (no prefix) → hashtag mode = third-party creators promoting BeFreed
- `@befreed` → user mode = BeFreed's own account posts
- `#befreed` → explicit hashtag mode

Returns videos ranked by **views descending** (default — BigSpy-style highest-reach-first) or recency. Hashtag mode includes a `Creator` column showing each row's actual poster, plus a "most prolific creators" summary at the bottom (the SMM insight: who's posting about this brand most often).

Precision filter: hashtag results only keep videos whose caption explicitly carries `#brand`, `#brand_NNNN` (campaign codes), or `@brand`. This rejects unrelated videos that TikTok's hashtag endpoint over-matches.

**First-run wall time is ~60-90 seconds for an active brand.** The CLI runs four discovery passes (user search → hashtags → campaign codes → seed-creator walk) with up to 8 concurrent fetches per pass, plus repeat-queries each hashtag until saturation. Tell the user this is expected before running. Subsequent searches on the same brand serve from cache instantly. Use `--refresh` to force a re-fetch (same time).

### Fork (video → creator brief)

```bash
ugcspy fork <video-id-or-url> [--out path] [--copy]
```

Produces a markdown brief with hook variations, format notes, beat sheet, b-roll, and CTA. Default output path is `~/.ugcspy/briefs/`.

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

## First-run

If the user has not run `ugcspy init`, suggest it. The wizard writes `~/.ugcspy/config.json` (chmod 0600) with just two things: their scraper choice (default `tiktok-oss`, free) and an optional default Slack webhook. **No Anthropic API key needed** — brief generation runs in this Claude Code chat using the user's existing subscription, and caption-based hooks come straight from the scraper output.

If they pick `tiktok-oss` and haven't run `ugcspy install-deps`, suggest that next.

## Tips

- The `--json` flag on `search` is the right tool when the user wants a list of URLs to feed into another action.
- After a search, the user can `/ugcspy-fork <id>` on any video — that prompt generates the brief in this chat directly, no API key, no per-brief cost.
- `watch list` is cheap; run it before adding a watch to avoid duplicates.
- If a `daemon --once` run shows `warming_up`, that is expected behavior, not a bug.
- Hooks in `search` output are caption-derived (free, deterministic). Format tags are NOT auto-classified by the standalone CLI — if the user wants format tagging, classify in this chat after the search returns.

## Repo

https://github.com/serenakeyitan/ugcspy — read [docs/DESIGN.md](https://github.com/serenakeyitan/ugcspy/blob/main/docs/DESIGN.md) for the full spec.
