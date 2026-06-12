# ugcspy

[![release](https://img.shields.io/github/v/release/serenakeyitan/ugcspy)](https://github.com/serenakeyitan/ugcspy/releases/latest)
[![ci](https://github.com/serenakeyitan/ugcspy/actions/workflows/ci.yml/badge.svg)](https://github.com/serenakeyitan/ugcspy/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

BigSpy for organic UGC. A Claude Code plugin (and standalone CLI) for spying on competitor short-form video on TikTok and Instagram Reels — type a brand name, get the third-party creators promoting it ranked by reach, then turn any video into a creator brief without leaving your chat.

**No API key needed.** Brief generation runs in your existing Claude Code subscription. Scraping is free via the OSS TikTok provider.

---

## 🚀 Onboarding — paste this into Claude Code

```
Install the latest ugcspy from https://github.com/serenakeyitan/ugcspy/releases/latest by following
https://github.com/serenakeyitan/ugcspy/blob/main/ONBOARDING.md end-to-end.
```

That's the whole prompt. Claude fetches [ONBOARDING.md](ONBOARDING.md), runs the 9 steps via Bash, and stops with a working `ugcspy` plus optionally the bundled `video-recipe` agent. ~2-3 minutes (the core install is browser-free — no Chromium download).

**Latest release:** [v0.2.0](https://github.com/serenakeyitan/ugcspy/releases/latest)

## What's in this repo

| Path | What it is |
|---|---|
| `src/`, `scripts/`, `.claude-plugin/` | **ugcspy core** — the BigSpy-for-UGC search engine, alerts, plugin commands. |
| `vendor/video-recipe/` | **Bundled video-recipe agent** — reverse-engineers any video URL into a `recipe.json` with cuts, per-clip prompts, hook pattern, voiceover. Maintained as its own repo at [github.com/serenakeyitan/video-recipe](https://github.com/serenakeyitan/video-recipe); imported here as a git subtree so the two stay loosely coupled. Updated via `git subtree pull --prefix=vendor/video-recipe ...`. |

The two products share a natural workflow: ugcspy finds the top UGC videos for a brand; video-recipe explains how each one was made. Inside Claude Code, `/ugcspy-recipe <video-id>` chains them automatically.

### Manual install (if you don't use Claude Code)

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run src/cli.ts install-deps     # ~30-60s — TikTokApi + yt-dlp into a managed venv; browser-free
bun run src/cli.ts init --yes        # non-interactive; defaults to tiktok-oss
bun run build                        # produces dist/cli.js
npm install --global .               # symlinks ugcspy onto PATH
ugcspy search liquiddeath --platform tiktok --limit 10   # first run on an active brand: ~5-8 min
```

After publishing to npm: `npm install -g ugcspy`.

---

## Quick start (after install)

```bash
ugcspy search liquiddeath --platform tiktok --limit 10
```

That's it — the table is the brand's **top UGC videos ranked by views** (last 30 days by default), with the creators behind them. To turn a video into a creator brief, use the Claude Code plugin: `/ugcspy-fork <video-id>` from inside Claude Code.

**Heads up on wall time.** A FIRST search on an active UGC brand takes a few minutes, not seconds — the mid-size brand we benchmarked (~150 discovered creators) runs ~5-8 minutes. Stage 1 discovers every creator via brand-hashtag feeds + a follow-graph snowball (pure HTTP); Stage 2 walks each creator's full catalog with yt-dlp, 16-way concurrent — that walk is where the time goes. Subsequent searches on the same brand serve from SQLite cache instantly. Add `--refresh` to force a fresh crawl. See [How hashtag search actually works](#how-hashtag-search-actually-works-browser-free-two-stage) for the architecture.

If you only want to try the CLI shape without setting up the scraper, pick `mock` in `ugcspy init` instead of `tiktok-oss` — it serves deterministic synthetic data with zero setup.

## The core flow

Three search modes — two auto-detected from the query prefix, one explicit:

```bash
# Plain word → hashtag mode = third-party creators promoting the brand
# (the BigSpy use case — finding UGC, not the brand's own marketing)
ugcspy search liquiddeath --platform tiktok
ugcspy search rarebeauty
ugcspy search notion

# @handle → user mode = the brand's OWN account posts (full catalog)
ugcspy search @glossier --platform tiktok

# #tag → explicit hashtag mode
ugcspy search "#booktok"

# --mode keyword → NICHE/TOPIC discovery: the broad corpus a script writer
# browses, NOT limited to videos tagging a brand. Finds untagged competitor /
# niche UGC by topic phrase. Works with ZERO setup (pure HTTP, no Chromium venv).
ugcspy search --mode keyword "skincare routine"
ugcspy search --mode keyword "cozy desk setup" --sort views

# Newest first instead of highest-reach
ugcspy search liquiddeath --sort recency

# JSON output for piping
ugcspy search liquiddeath --json | jq '.[] | {creator: .author_handle, views: .view_count}'

# Force a refresh (bypass the SQLite cache)
ugcspy search liquiddeath --refresh
```

The default sort is **views descending** — same as BigSpy ranks ads by impressions.

### Why hashtag mode is the default

The BigSpy-for-UGC question is "who's posting about this brand?", not "what is the brand posting?". Hashtag mode answers the first; `@handle` answers the second. Both are useful, but the wedge — the thing you can't easily get from any existing SaaS — is finding the third-party creator cohort.

A precision filter rejects videos that TikTok's hashtag endpoint over-matches (e.g. for a brand named "notion", captions using the everyday word "notion" in unrelated contexts collide with `#notion`). Only videos whose caption carries the brand via `#brand`, a `#brand_NNNN` campaign code, the `#brandapp` variant, an `@brand` mention, or the plain-text brand token at word boundaries are kept.

### How hashtag search actually works (browser-free, two-stage)

Single-hashtag scraping has a soft ceiling: TikTok's `#liquiddeath` challenge feed dedupes hard per-creator and only surfaces a handful of each creator's posts. So one hashtag call is **wildly incomplete** for any active UGC brand. ugcspy fixes this by separating two jobs that people usually conflate:

- **Discovery** — *which creators exist?* (find handles). Done over HTTP via the [tikwm](https://www.tikwm.com) relay. No browser.
- **Coverage** — *all of each creator's brand videos?* Done with **yt-dlp**, which walks a creator's full public catalog directly from `www.tiktok.com`.

The flow:

```
  ugcspy search "#liquiddeath"
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ STAGE 1 — DISCOVERY  (find creator handles, pure HTTP)       │
│                                                              │
│  ① ALL brand hashtags             ② Follow-graph snowball    │
│     challenge/search → every         walk who the core seeds │
│     #liquiddeath* tag (main +        FOLLOW (depth-1) →      │
│     #liquiddeath_0124,               recover the low-view    │
│     #drinkliquiddeath)               long tail that never    │
│     deep-page each feed              reaches a feed          │
│         │                               │                    │
│         └───────────────┬───────────────┘                    │
│                         ▼                                    │
│            union + score by signal                           │
│            (in N brand challenges, followed by N seeds)      │
│            → ranked creator roster                           │
└─────────────────────────┬────────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ STAGE 2 — COVERAGE  (yt-dlp, 16-way concurrent)              │
│                                                              │
│  for each creator (highest signal first):                    │
│     yt-dlp walks their FULL catalog                          │
│         │                                                    │
│         ▼                                                    │
│     brand filter per video  (#brand / #brand_NNNN /          │
│         │                    @brand / plain "brand")         │
│         │   ↳ caption clipped at the 72-char boundary?       │
│         │     re-fetch the full caption from tikwm (rescue)  │
│         ▼                                                    │
│     keep brand videos, with CURRENT view counts              │
└─────────────────────────┬────────────────────────────────────┘
                          ▼
            dedup + rank by views  →  SQLite cache  →  table
```

**Why two stages.** Discovery (tikwm) finds *names*; it doesn't need every video. Coverage (yt-dlp) pulls *all* videos for a found name — verified 100% catalog (e.g. 151/151 for one benchmarked creator) and reaches years-old posts. A single yt-dlp walk is fast (~6-7s even for 150 videos), so Stage 2's wall-time is dominated by fan-out across the roster, which is why it runs **16-way concurrent** (`UGCSPY_WALK_CONCURRENCY`).

**Brand precision.** A video is only kept if its caption carries the brand via `#brand`, a `#brand_NNNN` campaign code, an `@brand` mention, or the plain-text brand token. The discovery stage is intentionally *wide* (it collects every creator a hashtag surfaces); precision is enforced here, per video.

**The caption-truncation rescue.** yt-dlp's flat-playlist clips captions to ~72 chars and appends `…`. When the brand tag sits at that boundary, `#yourbrand_0124` arrives as `#yourbr…` and the filter would wrongly drop a genuine — often high-view — brand video. ugcspy detects the clip signature and re-fetches that one video's **full** caption from tikwm before discarding it. (This rescue is what surfaced a 2.6M-view video as a brand's true #1 clip — it had been silently dropped.)

**The stable tikwm client.** tikwm honors `count` loosely (8–18 videos for a requested 30) and intermittently serves a transient empty/error page mid-feed. Every tikwm call goes through a retry-with-backoff helper that tolerates this variance, so a deep challenge walk is **deterministic** — the same query returns the same creators run after run (measured 176/176/176, where the naive client swung 29↔111).

**Coverage vs the trade-off.** Pure-hashtag + snowball reaches ~89% of a brand's real creator roster (51/57 for the mid-size brand we benchmarked) — cleanly and fast. The creators it can't reach are a handful of very-low-view accounts whose videos never enter *any* TikTok challenge feed; no hashtag method can surface them. (An earlier full-text keyword search caught them but at ~8% precision, forcing the walk to chew through ~90% noise — so it was dropped in favor of the clean hashtag path.)

**What we cannot do (free path limits):**
- **Pull a brand's "following"/"liked" lists.** TikTok gates these behind auth.
- **Reach the very-low-view long tail.** Creators whose brand videos sit at a few hundred views never enter a challenge feed, so hashtag discovery can't see them.

For those, you'd need paid ScrapeCreators (handles auth-required endpoints) or residential proxies.

## Claude Code plugin

The plugin is the recommended way to use ugcspy. Inside Claude Code:

| Slash command | What it does |
|---|---|
| `/ugcspy-search <brand>` | Runs the search, renders the table inline |
| `/ugcspy-fork <id>` | Quick creator brief — hook + beat sheet. Generated in chat using your Claude Code subscription. **No API key.** |
| `/ugcspy-transcript <brand-or-id>` | Spoken hook + full Whisper transcript for the top N videos (or one). `--talking` / `--non-talking` filters montage vs voiceover content from the audio itself. Cached per video. |
| `/ugcspy-scout <your-brand>` | Template/account discovery when the sources are UNKNOWN — three lanes: today's viral hits (trend-riding), cross-category UGC playbooks (hook formulas that transfer), and direct competitors (niche mining). Ranked, remixability-judged shortlist feeding `/ugcspy-rebrand`. |
| `/ugcspy-rebrand <video> <brand>` | Minimal-edit script rebrand: swap/insert the promotion for a target brand at one smooth, content-matched beat. The hook is never touched; everything outside the brand beat stays byte-identical. |
| `/ugcspy-decode <id>` | Deep production decode — format, OCR'd overlay narrative, brand-pitch placement (soft 软广 vs hard sell), shot list. Writes `decode.json` + `decode.html`. Works on both human-shot AND AI-montage videos. |
| `/ugcspy-remix <target> <source>` | Cross-video format transfer. Decodes BOTH videos and writes a hand-able brief telling creator B how to shoot their own version of video A's format. |
| `/ugcspy-recipe <id>` | Reverse-engineer an AI-montage into a reproducible `recipe.json` (cuts, per-clip prompts, voiceover) |
| `/ugcspy-reproduce <id>` | Render an actual `reproduction.mp4` via Kling + OpenAI TTS. AI-montage only. |
| `/ugcspy-watch add <brand> --slack-webhook ...` | (Optional) Register a competitor for breakout alerts |
| `/ugcspy-daemon --once` | (Optional) Tick the watch poller |

**Common flow** for "I found a great video, I want to make something like it":

```
/ugcspy-search liquiddeath            # find ranked third-party UGC
/ugcspy-decode 4                       # understand HOW the #1 video was made
/ugcspy-remix 4 12                     # OR: brief creator @12 to shoot their version
/ugcspy-fork 4                         # OR: just get a quick brief
```

The skill ([`.claude-plugin/skills/ugcspy/SKILL.md`](.claude-plugin/skills/ugcspy/SKILL.md)) also triggers on intent — say "track @rarebeauty's organic UGC" or "how was this video made" and Claude picks the right command itself.

## Commands (standalone CLI)

| Command | What it does |
|---|---|
| `init` | Interactive setup — writes `~/.ugcspy/config.json` (chmod 0600) |
| `install-deps` | Install Python deps for the `tiktok-oss` provider (one-time) |
| `search <handle>` | Top videos by reach (default) or recency. The thing you came for. |
| `trending [region]` | Network-wide viral hits (no brand filter), ranked by views, cached as `trend:<REGION>` for the transcript/rebrand chain. |
| `discover <niche\|region>` | Mine a corpus for template sources: brand candidates via the `#brand_NNNN` campaign-code fingerprint + app-variant/account-match signals, plus recurring creators. |
| `transcript <brand\|id\|url>` | Spoken hook + transcript per video, talking/non-talking classification from the audio. Needs `install-deps --with-audio` (self-contained — bundles ffmpeg); transcribed once, cached in SQLite. |
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
- For real data: `tiktok-oss` is free and covers TikTok competitor tracking. The default path runs on the tikwm relay (discovery) + yt-dlp (coverage); [davidteather/TikTok-Api](https://github.com/davidteather/TikTok-Api) (6.3k stars, v7.3.3 shipped April 2026) is kept for the optional `UGCSPY_USE_CHROMIUM=1` fallbacks (user-mode rescue + extra discovery) — all via a Python subprocess.
- Add `scrapecreators` if you need Instagram Reels coverage or hit rate limits on the OSS path.

**Why no free Instagram option?** No production-grade free Instagram Reels scraper is currently maintained. Meta is more aggressive than TikTok about killing scrapers, and the leading repos either require login (ban risk for the user's account) or have been abandoned. ScrapeCreators is the honest answer for IG until that changes.

## Why

Brand SMMs already pay $300-1000/mo for Trendpop, Pentos, Sprout, Dash. None of them solve "type a competitor handle, get their ranked organic UGC, then turn any video into a creator brief — all in the agent I already use." The crowded space is full of platforms; nobody ships a BigSpy-shaped product that's free, scriptable, and agent-native.

## Troubleshooting

**Search returned far fewer creators than expected / "0 candidates".**
Hashtag mode is **browser-free** by default — discovery goes through the tikwm relay (pure HTTP, no Chromium). tikwm is an unofficial relay and can rate-limit a bursty caller. The client retries with backoff and tolerates feed variance, so a normal run is stable; but if you hammer it with many runs back-to-back it can hard-block the IP for a while (returns 403). Wait a few minutes and retry, or space runs out. You can tune the gap between challenge-feed reads with `UGCSPY_HASHTAG_FEED_DELAY` (seconds, default 0.3).

**Walk phase is slow on a big roster.** Stage 2 walks each creator's catalog with yt-dlp at `UGCSPY_WALK_CONCURRENCY` (default 16). yt-dlp hits `www.tiktok.com` directly (not rate-limited like tikwm), so you can raise it on a fast connection — or lower it if you see empty walks on a constrained machine.

**Optional Chromium fallback.** Discovery is browser-free by default — the Chromium binary isn't even downloaded unless you ask for it. If you want the legacy Chromium-assisted discovery as an *extra* source (e.g. on a residential IP where it's stable), provision it once with `ugcspy install-deps --with-browser` (~150MB), then set `UGCSPY_USE_CHROMIUM=1`. It's off by default because it crashes/times out on most hosts and the tikwm sources cover the same ground.

**"TikTokApi not installed."** The bridge imports TikTokApi for `user` and `hashtag` modes (even though it only *uses* live TikTokApi sessions in the optional `UGCSPY_USE_CHROMIUM=1` fallbacks), so either mode can raise this on a bare interpreter. Run `ugcspy install-deps` to provision the managed venv (one-time). `--mode keyword` is pure HTTP (tikwm) and never needs TikTokApi.

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
bun run build            # produce dist/cli.js (single-file binary, ~450KB)
```

## License

MIT — see [LICENSE](LICENSE).
