---
description: Search a competitor's recent organic TikTok / Instagram Reels videos
argument-hint: "<handle> [--platform tiktok|instagram|all] [--limit N] [--json]"
---

You are running `ugcspy search` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy search $ARGUMENTS
```

If the user did not specify `--json`, the CLI prints a formatted table; relay it as a code block. If they passed `--json`, parse and summarize the top 5 in a markdown table, then offer to deep-dive on any row (each row has an `id` you can pass to `/ugcspy-fork` to generate a creator brief in this chat).

The default sort is `views` (highest reach first, BigSpy-style). If the user wants newest-first, suggest `--sort recency`.

If the user has not run `ugcspy init`, the CLI will surface a clear error — relay it and suggest they run init.
