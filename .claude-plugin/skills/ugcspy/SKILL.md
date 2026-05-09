---
name: ugcspy
description: Use this skill when the user asks to research, track, or analyze a competitor's organic short-form video (TikTok or Instagram Reels) — for example "what is @glossier posting", "track @rarebeauty's organic UGC", "what's working for [brand] on TikTok", or any request to find top-performing competitor videos, set up alerts on viral competitor content, or generate a creator brief inspired by a competitor video. Invoke `ugcspy` via the Bash tool.
---

# ugcspy — competitor organic UGC intelligence

`ugcspy` is a CLI installed on the user's machine. Run it via the Bash tool. Three subcommands cover the V1 surface; everything is JSON-friendly via `--json`.

## When to use

- "What is @glossier posting on TikTok?" → `ugcspy search @glossier` (the headline use case)
- "Show me Rare Beauty's top 20 videos" → `ugcspy search @rarebeauty --limit 20`
- "Newest first, not by views" → `ugcspy search @brand --sort recency`
- "Turn this video into a brief I can hand to a creator" → `ugcspy fork <url>`
- "Slack-alert me when a competitor breaks out" (only if explicitly asked) → see Watch + daemon below

Skip if the user is asking about **paid ads** (different tool — TikTok Creative Center / Facebook Ad Library).

## Commands

### Search (the core command)

```bash
ugcspy search <handle> [--platform tiktok|instagram|all] [--limit N] \
                      [--sort views|recency] [--format <tags>] [--json]
```

Returns the handle's recent organic videos ranked by **views descending** (default — BigSpy-style highest-reach-first) or recency, with extracted hooks and format tags. Use `--json` when you need to feed results into the rest of a workflow.

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

If the user has not run `ugcspy init`, suggest it. The wizard writes `~/.ugcspy/config.json` (chmod 0600) with their data-provider choice, scraper API key, Anthropic API key, and optional default Slack webhook. With no keys configured, the `mock` provider returns deterministic synthetic data — useful for trying the CLI shape, not real research.

## Tips

- The `--json` flag on `search` is the right tool when the user wants a list of URLs to feed into another action.
- `watch list` is cheap; run it before adding a watch to avoid duplicates.
- If a `daemon --once` run shows `warming_up`, that is expected behavior, not a bug.
- Anthropic API calls happen for hook extraction, format tagging, and brief generation. With no Anthropic key, hooks fall back to caption-only and format tags are `null`.

## Repo

https://github.com/serenakeyitan/ugcspy — read [docs/DESIGN.md](https://github.com/serenakeyitan/ugcspy/blob/main/docs/DESIGN.md) for the full spec.
