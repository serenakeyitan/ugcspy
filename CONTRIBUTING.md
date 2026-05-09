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

The `tiktok-oss` provider needs Python and a Chromium binary. Either run:

```bash
bun run src/cli.ts install-deps
```

Or do it manually if you want to control where deps land:

```bash
python3 -m pip install --user -r scripts/requirements.txt
python3 -m playwright install chromium
```

Then point at a known DTC handle:

```bash
bun run src/cli.ts search @glossier --platform tiktok --limit 5
```

This actually hits TikTok and may take 20-40 seconds on the first call (cold Chromium start + bot-detection negotiation). Subsequent calls are served from `~/.ugcspy/db.sqlite`.

## Project layout

```
src/
  cli.ts                     # commander entry point
  commands/
    init.ts                  # interactive config wizard
    install-deps.ts          # one-shot Python dep installer
    search.ts                # `ugcspy search`
    watch.ts                 # `ugcspy watch add/list/remove`
    daemon.ts                # `ugcspy daemon` poll loop
    fork.ts                  # `ugcspy fork` -> Sonnet 4.6 brief
  providers/
    types.ts                 # DataProvider interface
    mock.ts                  # deterministic synthetic data
    tiktok-oss.ts            # Bun -> Python bridge to davidteather/TikTok-Api
    scrapecreators.ts        # paid TikTok + IG provider (stub for now)
    index.ts                 # provider switch
  extractors/
    hook.ts                  # caption -> Sonnet vision -> Whisper fallback
    format.ts                # 10-tag closed-list classifier (Haiku)
  lib/
    breakout.ts              # detection math (medians, thresholds, 24h window)
    config.ts                # ~/.ugcspy/config.json
    slack.ts                 # webhook formatter + poster
  db/
    schema.ts                # SQLite migrations
    index.ts                 # bun:sqlite open
  types.ts                   # platform-wide types

scripts/
  tiktok_fetch.py            # Python bridge (TikTokApi wrapper)
  requirements.txt           # pip deps

test/                        # bun:test suites
.claude-plugin/              # Claude Code plugin (slash commands + skill)
```

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
