# ugcspy

BigSpy for organic UGC. A Claude Code plugin (and standalone CLI) for spying on competitor short-form video on TikTok and Instagram Reels — type a handle, get the videos that are actually getting views, then turn any video into a creator brief without leaving your chat.

**No API key needed.** Brief generation runs in your existing Claude Code subscription. Scraping is free via the OSS TikTok provider.

## Install

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run src/cli.ts --help
```

After publishing to npm: `npm install -g ugcspy`.

## Quick start (~60 seconds)

```bash
# 1. Set up config — pick `tiktok-oss` (free) when prompted
bun run src/cli.ts init

# 2. Install Python deps for the OSS scraper — one-time, ~150MB
bun run src/cli.ts install-deps

# 3. Spy on a competitor — top videos by reach, BigSpy-style
bun run src/cli.ts search @glossier --platform tiktok --limit 10
```

That's it. To turn a video into a creator brief, use the Claude Code plugin: `/ugcspy-fork <video-id>` from inside Claude Code.

Skip step 2 if you only want to try the CLI shape — the `mock` provider serves deterministic synthetic data with zero setup.

## The core flow

Two search modes, auto-detected from the query prefix:

```bash
# Plain word → hashtag mode = third-party creators promoting the brand
# (the BigSpy use case — finding UGC, not the brand's own marketing)
ugcspy search befreed --platform tiktok
ugcspy search liquiddeath
ugcspy search notion

# @handle → user mode = the brand's OWN account posts
ugcspy search @glossier --platform tiktok

# #tag → explicit hashtag mode
ugcspy search "#booktok"

# Newest first instead of highest-reach
ugcspy search befreed --sort recency

# JSON output for piping
ugcspy search befreed --json | jq '.[] | {creator: .author_handle, views: .view_count}'

# Force a refresh (bypass the SQLite cache)
ugcspy search befreed --refresh
```

The default sort is **views descending** — same as BigSpy ranks ads by impressions.

### Why hashtag mode is the default

The BigSpy-for-UGC question is "who's posting about this brand?", not "what is the brand posting?". Hashtag mode answers the first; `@handle` answers the second. Both are useful, but the wedge — the thing you can't easily get from any existing SaaS — is finding the third-party creator cohort.

A precision filter rejects videos that TikTok's hashtag endpoint over-matches (e.g. "be freed" / "freed" appearing in unrelated contexts collide with `#befreed`). Only videos with an explicit `#brand`, `#brand_NNNN` campaign code, or `@brand` mention are kept.

## Claude Code plugin

The plugin is the recommended way to use ugcspy. Inside Claude Code:

- `/ugcspy-search @glossier` — runs the search, renders the table inline
- `/ugcspy-fork <video-id-or-url>` — generates a creator brief in chat using your Claude Code subscription. **No API key.**
- `/ugcspy-watch add @glossier --slack-webhook ...` — (optional) register a competitor for breakout alerts
- `/ugcspy-daemon --once` — (optional) tick the watch poller

The skill ([`.claude-plugin/skills/ugcspy/SKILL.md`](.claude-plugin/skills/ugcspy/SKILL.md)) also triggers on intent — say "track @rarebeauty's organic UGC" and Claude picks the skill itself.

## Commands (standalone CLI)

| Command | What it does |
|---|---|
| `init` | Interactive setup — writes `~/.ugcspy/config.json` (chmod 0600) |
| `install-deps` | Install Python deps for the `tiktok-oss` provider (one-time) |
| `search <handle>` | Top videos by reach (default) or recency. The thing you came for. |
| `watch add <handle>` | (Optional) Register a competitor for breakout alerts — see below |
| `watch list` / `watch remove <id>` | Manage watches |
| `daemon` | (Optional) Poll watches, post Slack alerts on threshold breach |

**Brief generation lives in the Claude Code plugin, not the standalone CLI.** This is intentional — it means no Anthropic API key, no per-brief cost, no extra setup. If you want briefs without Claude Code, use `search --json` and pipe the output to your LLM of choice.

## Optional: breakout alerts

If you want to be Slack-pinged when a competitor video crosses a view threshold, set up a watch + daemon. **Not part of the core flow** — most users live in `search`. The alert pipeline exists for power users who want passive monitoring.

```bash
# 1. Watch a competitor — Slack pings when a video posted in the last 24h
#    crosses 2x the trailing-30-day median views
bun run src/cli.ts watch add @glossier --slack-webhook https://hooks.slack.com/services/... --threshold 2

# 2. Tick the daemon manually, or set it up as a cron / GitHub Actions schedule
bun run src/cli.ts daemon --once

# 3. List or remove watches
bun run src/cli.ts watch list
bun run src/cli.ts watch remove 1
```

**Cold-start gate:** alerts stay in `warming_up` state until both 7 days have elapsed AND ≥5 videos exist in the trailing window. Prevents noise on day-1 ingestion. See [src/lib/breakout.ts](src/lib/breakout.ts) for the math and [test/breakout.test.ts](test/breakout.test.ts) for the tests.

## Data providers

| Provider | Cost | Coverage | Setup |
|---|---|---|---|
| `tiktok-oss` | **Free** | TikTok only | `ugcspy install-deps` (one-time) |
| `scrapecreators` | Paid | TikTok + Instagram Reels | API key from scrapecreators.com |
| `mock` | Free | Synthetic | None — useful for trying the CLI shape |
| `apify`, `bright_data` | — | — | Stubs for future drop-in providers |

**Recommended path:**
- Try `mock` first to see how the CLI works.
- For real data: `tiktok-oss` is free and covers TikTok competitor tracking. It wraps [davidteather/TikTok-Api](https://github.com/davidteather/TikTok-Api) (6.3k stars, v7.3.3 shipped April 2026) via a Python subprocess.
- Add `scrapecreators` if you need Instagram Reels coverage or hit rate limits on the OSS path.

**Why no free Instagram option?** No production-grade free Instagram Reels scraper is currently maintained. Meta is more aggressive than TikTok about killing scrapers, and the leading repos either require login (ban risk for the user's account) or have been abandoned. ScrapeCreators is the honest answer for IG until that changes.

## Why

Brand SMMs already pay $300-1000/mo for Trendpop, Pentos, Sprout, Dash. None of them solve "type a competitor handle, get their ranked organic UGC, then turn any video into a creator brief — all in the agent I already use." The crowded space is full of platforms; nobody ships a BigSpy-shaped product that's free, scriptable, and agent-native.

## Troubleshooting

**"TikTok returned an empty response. They are detecting you're a bot."**
The `tiktok-oss` provider already uses `chromium + headless=False` to bypass detection — you'll see a brief Chromium window flash open during scrapes, that's intentional. If you still get blocked:

```bash
# Grab an MS token from your own browser, then:
export MS_TOKEN="<paste-token-from-tiktok.com-cookies>"
ugcspy search @glossier
```

To get the token: open tiktok.com in Chrome, DevTools → Application → Cookies → `https://www.tiktok.com` → copy the `msToken` value.

**"TikTokApi not installed."** You haven't run `ugcspy install-deps` yet, or the install failed silently. Re-run it.

**Chromium window keeps flashing.** That's how `tiktok-oss` works — pure headless mode is blocked by TikTok. If this is a dealbreaker (e.g. running on a server with no display), use `scrapecreators` (paid, headless-friendly) instead.

**The brand name is ambiguous (e.g. `#headway` matches a book-summary app AND a therapy platform AND a dance studio).** The hashtag itself doesn't disambiguate — the precision filter only confirms each result has `#headway`, not which Headway. Use a more specific hashtag if the brand has one (e.g. `headwayapp`, `liquiddeath` is unambiguous, `notion` collides with the unrelated word but is mostly safe), or pipe `--json` and filter on related hashtags (`booksummary`, `microlearning`) yourself.

**Search returned 0 videos for a real hashtag.** If the bridge throws `'Hashtag' object has no attribute 'id'` and we caught it as an empty result, that's because TikTok doesn't have an indexed hashtag page for it (very small brand or new tag). Try the brand's own account: `ugcspy search @brand` (user mode).

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
