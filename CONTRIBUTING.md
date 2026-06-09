# Contributing to ugcspy

This is an early-stage project. The design doc at [docs/DESIGN.md](docs/DESIGN.md) is the source of truth for V1 scope; if you want to add something not in there, open an issue first so we can talk through whether it fits.

## Setup

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run typecheck   # strict TS, must be clean
bun test            # bun:test, must be green
```

Node-only environments aren't supported by V1 — Bun is required (we use `bun:sqlite` for SQLite without a native build).

## Running the CLI from source

```bash
bun run src/cli.ts <subcommand>
# or via the dev alias:
bun run dev -- <subcommand>
```

## Testing real-data paths

The `tiktok-oss` provider needs Python. Hashtag mode (the default discovery path) is browser-free — it runs over pure HTTP via the tikwm relay plus yt-dlp, so no Chromium is required. Chromium is only needed for the `user`/`keyword` modes and the optional Chromium fallback (`UGCSPY_USE_CHROMIUM=1`, off by default), which drive TikTokApi. Either run:

```bash
bun run src/cli.ts install-deps
```

Or do it manually if you want to control where deps land:

```bash
python3 -m pip install --user -r scripts/requirements.txt
python3 -m playwright install chromium   # only for user/keyword modes + Chromium fallback; skip for hashtag mode
```

Then point at a known DTC handle:

```bash
bun run src/cli.ts search @glossier --platform tiktok --limit 5
```

This actually hits TikTok. A handle search runs in `user` mode (TikTokApi), so the first call may take 20-40 seconds (cold Chromium start + bot-detection negotiation). Hashtag/brand searches take the browser-free path (tikwm HTTP discovery + yt-dlp coverage walk) and skip Chromium entirely. Subsequent calls are served from `~/.ugcspy/db.sqlite`.

## Project layout

```
src/
  cli.ts                     # commander entry point
  commands/
    init.ts                  # interactive config wizard
    install-deps.ts          # one-shot Python dep installer
    search.ts                # `ugcspy search` (hashtag + user modes)
    watch.ts                 # `ugcspy watch add/list/remove` (optional alerts)
    daemon.ts                # `ugcspy daemon` poll loop (optional alerts)
  providers/
    types.ts                 # DataProvider interface
    mock.ts                  # deterministic synthetic data
    tiktok-oss.ts            # Bun -> Python bridge to davidteather/TikTok-Api
    scrapecreators.ts        # paid TikTok + IG provider (stub)
    index.ts                 # provider switch
  lib/
    breakout.ts              # alert math (medians, thresholds, 24h window)
    config.ts                # ~/.ugcspy/config.json
    slack.ts                 # webhook formatter + poster
  db/
    schema.ts                # SQLite migrations
    index.ts                 # bun:sqlite open
  types.ts                   # platform-wide types

scripts/
  tiktok_fetch.py            # Python bridge — browser-free hashtag discovery (tikwm) + yt-dlp coverage walk
  requirements.txt           # pip deps (yt-dlp for hashtag mode; TikTokApi + playwright for user/keyword modes + Chromium fallback)

test/                        # bun:test suites
.claude-plugin/              # Claude Code plugin
  plugin.json                # plugin manifest
  commands/                  # slash commands: setup, search, fork, watch, daemon
  skills/ugcspy/SKILL.md     # intent-triggered skill
```

Note: brief generation (`/ugcspy-fork`) is implemented inside the Claude Code plugin command file, not as a standalone CLI subcommand — it uses the user's Claude Code subscription instead of an Anthropic API key.

## Adding a new data provider

1. Create `src/providers/my-provider.ts` exporting a class that implements `DataProvider` from `./types.ts`.
2. Add a case in `src/providers/index.ts` `getProvider` switch.
3. Add the variant to `Config["scraper_provider"]` in `src/types.ts`.
4. Add the option to the `init` wizard in `src/commands/init.ts`.
5. Add a test in `test/` covering the platform guard and the name field at minimum.

## Detection-quality bar

Per the design doc, before any change to the alert pipeline ships, run a backtest on a hand-labeled set:

- ≥80% recall on real breakouts
- ≤20% false-positive rate

The doc calls this the V1 launch gate. If your change moves these numbers in the wrong direction, it doesn't ship.

## Commits

Conventional commit prefixes: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`. Keep the first line ≤72 chars; explain the *why* in the body. One PR per problem (no mixing unrelated changes).

## Code style

- TS strict mode is on; tsc must be clean before `bun test`.
- No `any`. If you genuinely need `unknown`, parse it at the boundary and narrow before using.
- Prefer named exports. Default exports only when the file genuinely has one thing.
- Comments explain *why*, not *what*. Don't write comments that the next reader would learn faster by reading the code.
