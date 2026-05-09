---
description: Generate a creator brief from a competitor video
argument-hint: "<video-id-or-url> [--out path] [--copy]"
---

You are running `ugcspy fork` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy fork $ARGUMENTS
```

The fork command writes a markdown brief to `~/.ugcspy/briefs/` by default (or to `--out`, or to clipboard with `--copy`). After it completes, read the brief file and show the user the brief inline so they can act on it without leaving the chat. Offer to refine specific sections (hook, beat sheet, etc.) if they want a second pass.

`fork` requires an Anthropic API key (`ugcspy init`). If missing, relay the CLI error.
