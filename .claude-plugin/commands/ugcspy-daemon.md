---
description: Tick the ugcspy daemon (poll watches, fire Slack alerts)
argument-hint: "[--once] [--interval ms] [--days N]"
---

You are running `ugcspy daemon` for the user. The CLI binary is `ugcspy` on PATH.

User arguments: `$ARGUMENTS`

By default, run a single tick (safer than entering an infinite loop in chat):

```bash
ugcspy daemon --once $ARGUMENTS
```

Only run without `--once` if the user explicitly asked for a long-running daemon — and warn them the loop blocks until they Ctrl+C.

After the tick, summarize: how many watches were polled, how many are still `warming_up`, how many alerts fired. If a watch is `warming_up`, that is expected — relay the reason verbatim from the CLI output.
