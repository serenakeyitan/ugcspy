---
description: Watch a competitor and alert on breakout videos via Slack
argument-hint: "add <handle> --slack-webhook <url> [--threshold 2.0]  |  list  |  remove <id>"
---

You are running `ugcspy watch` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

Run via the Bash tool:

```bash
ugcspy watch $ARGUMENTS
```

Notes:
- Adding a watch requires a Slack webhook URL. If the user has set a `default_slack_webhook` in `ugcspy init`, the CLI will fall back to that.
- A new watch starts in `warming_up` state. It becomes `active` after 7 days AND ≥5 videos in the trailing window.
- After adding a watch, suggest the user run `ugcspy daemon --once` to populate baseline data immediately.
