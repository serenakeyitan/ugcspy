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

If they passed `--json`, parse the array and summarize the top 5 in a markdown table. Each row has an `id` field; the user can pass any id to `/ugcspy-fork` to generate a creator brief in this chat.

## Defaults

- Sort: `views` (highest reach first — BigSpy-style)
- Window: last 30 days
- Platform: `all` (the standalone CLI tries TikTok and Instagram; tiktok-oss only supports TikTok so it'll cleanly skip IG)

## Wall time

Hashtag-mode first-run on an active brand takes ~60-90 seconds (four discovery passes with concurrency=8 parallelism, plus repeat-querying within each hashtag). Tell the user this BEFORE invoking. User-mode (`@brand`) is much faster (~10-20s, single fetch).

Subsequent searches on the same brand serve from cache instantly. Use `--refresh` for a fresh fetch.

## Setup errors

If the user gets `tiktok-oss: TikTokApi not installed`, suggest `ugcspy install-deps`.

If the user gets a bot-detection error ("TikTok returned an empty response"), suggest setting `MS_TOKEN` from their browser cookies — see README troubleshooting.
