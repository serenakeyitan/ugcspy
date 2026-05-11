---
description: One-shot setup for ugcspy on a fresh machine — installs deps, configures, runs a verification search
argument-hint: "(no arguments)"
---

You are doing the full ugcspy onboarding for the user.

Follow the canonical onboarding guide at [`ONBOARDING.md`](../../ONBOARDING.md) (in this repo) end-to-end via the Bash tool. That file is the single source of truth — it has the same 9 steps the README points to, kept in sync there so we don't drift.

If you're invoking this slash command from outside the repo (the user hasn't cloned it yet), fetch ONBOARDING.md from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/serenakeyitan/ugcspy/main/ONBOARDING.md
```

Then execute the steps in order, surfacing errors with the fixes documented in that file. Stop when the user has a working `ugcspy --version` of `0.2.0+` and a verification search has returned real results.
