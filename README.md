# ugcspy

BigSpy for organic UGC. A CLI for tracking competitor short-form video on TikTok and Instagram Reels — search, alert, and turn winning videos into creator briefs.

**Status:** design-only. No code yet. See [docs/DESIGN.md](docs/DESIGN.md) for the full V1 spec.

## What it does (V1)

Three commands. That's the whole product.

```bash
ugcspy search @glossier              # ranked feed of last 30 days, with extracted hooks
ugcspy watch @glossier --slack ...   # Slack alert when a video crosses 2x median in 24h
ugcspy fork <video-url>              # generates a creator brief from any video
```

Plus a Claude Code plugin so the same commands work as `/ugcspy-search` etc. inside agentic workflows.

## Why

Brand SMMs already pay $300-1000/mo for Trendpop, Pentos, Sprout, Dash. None of them solve "type a competitor handle, get their ranked organic UGC + extracted hooks + alerts on breakouts." The crowded space is full of platforms; nobody ships a BigSpy-shaped product (search-first, fast, scriptable, agent-native).

## Design

Read [docs/DESIGN.md](docs/DESIGN.md). It went through three rounds of adversarial review and one Codex cold-read; the spec is locked at V1 scope.

## License

MIT (planned, on first code commit).
