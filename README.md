# ugcspy

BigSpy for organic UGC. A CLI for tracking competitor short-form video on TikTok and Instagram Reels — search, alert, and turn winning videos into creator briefs.

**Status:** V0 scaffold. Three commands wired end-to-end against a mock data provider; real-provider integration is the Day 0 spike per [docs/DESIGN.md](docs/DESIGN.md).

## Install

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run src/cli.ts --help
```

After publishing to npm: `npm install -g ugcspy`.

## Quick start

```bash
# 1. One-time setup (paste API keys, choose provider)
bun run src/cli.ts init

# 2. Search a competitor — works against mock data with no keys
bun run src/cli.ts search @glossier --platform tiktok --limit 10

# 3. Watch a competitor and Slack-alert on breakouts (≥ 2x trailing median)
bun run src/cli.ts watch add @glossier --slack-webhook https://hooks.slack.com/...

# 4. Tick the daemon once (or `bun run src/cli.ts daemon` to loop every 6h)
bun run src/cli.ts daemon --once

# 5. Pick a video id from `search --json` and fork it into a creator brief
bun run src/cli.ts fork 42
```

## Commands

| Command | What it does |
|---|---|
| `init` | Interactive setup — writes `~/.ugcspy/config.json` (chmod 0600) |
| `search <handle>` | Ranked feed of recent organic videos with extracted hooks |
| `watch add <handle>` | Register a competitor for breakout monitoring |
| `watch list` / `watch remove <id>` | Manage watches |
| `daemon` | Poll all watches, post Slack alerts on threshold breach |
| `fork <id-or-url>` | Sonnet 4.6 turns a video into a creator brief |

## Claude Code plugin

ugcspy ships as a Claude Code plugin. Inside Claude Code, the CLI is exposed as four slash commands plus an intent-triggered skill:

- `/ugcspy-search @glossier`
- `/ugcspy-watch add @glossier --slack-webhook ...`
- `/ugcspy-daemon --once`
- `/ugcspy-fork <video-url>`

The skill ([`.claude-plugin/skills/ugcspy/SKILL.md`](.claude-plugin/skills/ugcspy/SKILL.md)) also triggers on intent ("track @rarebeauty's organic UGC") so you don't always need the slash form.

Every command supports `--help`. `search` supports `--json` for programmatic use.

## How alerts work

A watch fires when a competitor's video crosses `threshold × trailing-median-views` (default 2x, configurable via `--threshold`).

**Cold-start gate:** alerts stay in `warming_up` state until both:
- 7 days have elapsed since the watch was created, AND
- ≥5 videos exist in the trailing 30-day window.

This prevents noise on day-1 ingestion. See [src/lib/breakout.ts](src/lib/breakout.ts) and the test suite in [test/breakout.test.ts](test/breakout.test.ts).

## Data providers

| Provider | Status | Use |
|---|---|---|
| `mock` | ✅ ready | Synthetic deterministic data — no API key needed, perfect for development |
| `scrapecreators` | 🚧 Day 0 | Real TikTok + IG Reels — implementation lands after the Day 0 spike |
| `apify` / `bright_data` | 📋 stub | Drop-in alternates if ScrapeCreators fails the spike |

## Why

Brand SMMs already pay $300-1000/mo for Trendpop, Pentos, Sprout, Dash. None of them solve "type a competitor handle, get their ranked organic UGC + extracted hooks + alerts on breakouts." The crowded space is full of platforms; nobody ships a BigSpy-shaped product (search-first, fast, scriptable, agent-native).

## Design

Read [docs/DESIGN.md](docs/DESIGN.md). It went through three rounds of adversarial review and one Codex cold-read; the V1 spec is locked.

## Development

```bash
bun install              # install deps
bun run typecheck        # tsc --noEmit
bun test                 # bun's native test runner
bun run dev -- search @glossier   # run CLI from source
bun run build            # produce dist/cli.js (single-file binary, ~670KB)
```

## License

MIT — see [LICENSE](LICENSE).
