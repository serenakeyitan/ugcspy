---
description: Track a brand/creator and alert on Slack — relative breakouts OR an absolute view-milestone reminder that carries the video link + a ready /ugcspy-rebrand command
argument-hint: "add <handle> --slack-webhook <url> [--threshold 2.0 | --view-threshold 100000 --remix-brand <brand>]  |  list  |  remove <id>"
---

You are running `ugcspy watch` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy watch $ARGUMENTS
```

## Two alert modes

**1. Relative breakout (default).** Fires when a tracked video crosses `--threshold` × the creator's trailing-30-day median views — "tell me when this account has an unusual hit."

```bash
ugcspy watch add @glossier --threshold 2.0 --slack-webhook <url>
```

**2. Absolute view-milestone reminder (`--view-threshold`).** Fires the moment ANY tracked video crosses a fixed view count, and the Slack reminder leads with the **video link** plus a ready **`/ugcspy-rebrand <id> <brand>` command** — so the creator can turn the proven video into their own script on the spot. This is the "ping me when their video hits 100K so I can remix it" workflow.

```bash
# remind me when a glossier video crosses 100K, with a BeFreed remix CTA
ugcspy watch add @glossier --view-threshold 100000 --remix-brand BeFreed --slack-webhook <url>
```

`--remix-brand` is optional — without it the reminder's CTA carries a `<your-brand>` placeholder.

## Notes

- A Slack webhook is required (or set `default_slack_webhook` in `ugcspy init`).
- **Relative** watches start `warming_up`, becoming `active` after 7 days AND ≥5 videos. **Absolute** (`--view-threshold`) watches are `active` immediately — a fixed milestone is meaningful on day one.
- Each video fires its alert exactly once (per-video dedup), so a video already past the bar won't re-spam on every tick.
- After adding a watch, suggest `ugcspy daemon --once` to poll immediately (or set up a cron / GitHub Actions schedule for ongoing monitoring).
- `ugcspy watch list` shows each watch's trigger (relative `Nx median` or absolute `≥ N views`) and its remix brand.
