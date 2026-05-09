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

First-run, free real-data path (TikTok only) takes ~60 seconds:

```bash
# 1. Set up config (pick `tiktok-oss` provider when prompted)
bun run src/cli.ts init

# 2. Install Python deps (TikTokApi + Chromium) — one-time, ~150MB
bun run src/cli.ts install-deps

# 3. Search real Glossier TikToks
bun run src/cli.ts search @glossier --platform tiktok --limit 10

# 4. Watch and Slack-alert on breakouts (≥ 2x trailing median)
bun run src/cli.ts watch add @glossier --slack-webhook https://hooks.slack.com/...
bun run src/cli.ts daemon --once

# 5. Pick a video id from `search --json` and fork it into a creator brief
#    (needs an Anthropic API key in `init`)
bun run src/cli.ts fork 42
```

Skip step 2 entirely if you only want to try the CLI shape — the default `mock` provider serves deterministic synthetic data with zero setup.

## Commands

| Command | What it does |
|---|---|
| `init` | Interactive setup — writes `~/.ugcspy/config.json` (chmod 0600) |
| `install-deps` | Install Python deps for the `tiktok-oss` provider (one-time) |
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

| Provider | Cost | Coverage | Setup |
|---|---|---|---|
| `tiktok-oss` | **Free** | TikTok only | `pip install -r scripts/requirements.txt && python3 -m playwright install chromium` |
| `scrapecreators` | Paid | TikTok + Instagram Reels | API key from scrapecreators.com |
| `mock` | Free | Synthetic | None — useful for trying the CLI shape |
| `apify`, `bright_data` | — | — | Stubs for future drop-in providers |

**Recommended path:**
- Try `mock` first to see how the CLI works.
- For real data: `tiktok-oss` is free and covers TikTok competitor tracking. It wraps [davidteather/TikTok-Api](https://github.com/davidteather/TikTok-Api) (6.3k stars, actively maintained — v7.3.3 shipped April 2026) via a Python subprocess.
- Add `scrapecreators` later if you need Instagram Reels coverage or hit rate limits on the OSS path.

**Why no free Instagram option?** No production-grade free Instagram Reels scraper is currently maintained. Meta is more aggressive than TikTok about killing scrapers, and the leading repos either require login (ban risk for the user's account) or have been abandoned. ScrapeCreators is the honest answer for IG until that changes.

## Why

Brand SMMs already pay $300-1000/mo for Trendpop, Pentos, Sprout, Dash. None of them solve "type a competitor handle, get their ranked organic UGC + extracted hooks + alerts on breakouts." The crowded space is full of platforms; nobody ships a BigSpy-shaped product (search-first, fast, scriptable, agent-native).

## Troubleshooting

**"TikTok returned an empty response. They are detecting you're a bot."**
Update to the latest version (`git pull && bun install`). The `tiktok-oss` provider already uses `chromium + headless=False` to bypass detection — you'll see a brief Chromium window flash open during scrapes, that's intentional. If you still get blocked:

```bash
# Grab an MS token from your own browser, then:
export MS_TOKEN="<paste-token-from-tiktok.com-cookies>"
ugcspy search @glossier
```

To get the token: open tiktok.com in Chrome, DevTools → Application → Cookies → `https://www.tiktok.com` → copy the `msToken` value.

**"TikTokApi not installed."** You haven't run `ugcspy install-deps` yet, or the install failed silently. Re-run it.

**Chromium window keeps flashing.** That's how `tiktok-oss` works — pure headless mode is blocked by TikTok. If this is a dealbreaker (e.g. running on a server with no display), use `scrapecreators` (paid, headless-friendly) instead.

**Engagement rate ranks tiny videos above huge ones.** Known issue with engagement-rate sort on short-form video — small denominators inflate the rate. Use `--sort recency` if you want absolute reach instead.

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
