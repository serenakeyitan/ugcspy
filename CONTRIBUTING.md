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

The `tiktok-oss` provider needs Python. Both default modes are browser-free: hashtag discovery runs over pure HTTP via the tikwm relay, and coverage (plus `user` mode) walks catalogs with yt-dlp. Chromium/playwright is only needed for the optional `UGCSPY_USE_CHROMIUM=1` fallbacks (user-mode rescue + an extra discovery source), which drive TikTokApi. `--mode keyword` needs neither TikTokApi nor Chromium — it runs on system `python3` with stdlib only.

Run:

```bash
bun run src/cli.ts install-deps                  # managed venv at ~/.ugcspy/venv
bun run src/cli.ts install-deps --with-browser   # add Chromium, only for the UGCSPY_USE_CHROMIUM=1 fallbacks
```

The managed venv is **required** for the `user`/`hashtag` modes — the provider resolves its interpreter from `~/.ugcspy/venv` and refuses to fall back to system Python (except for keyword mode), so a manual `pip install --user` of `scripts/requirements.txt` won't be picked up.

Then point at a known DTC handle:

```bash
bun run src/cli.ts search @glossier --platform tiktok --limit 5
```

This actually hits TikTok. A handle search runs in `user` mode — a browser-free yt-dlp walk of the creator's full catalog, so the first call takes ~10-20 seconds (no Chromium, no bot-detection negotiation). Hashtag/brand searches take the two-stage browser-free path (tikwm HTTP discovery + yt-dlp coverage walk). Subsequent calls are served from `~/.ugcspy/db.sqlite`.

## Project layout

```
src/
  cli.ts                     # commander entry point
  commands/
    init.ts                  # interactive config wizard
    install-deps.ts          # one-shot Python dep installer
    search.ts                # `ugcspy search` (hashtag + user + keyword modes)
    watch.ts                 # `ugcspy watch add/list/remove` (optional alerts)
    daemon.ts                # `ugcspy daemon` poll loop (optional alerts)
    render.ts                # `ugcspy render` — internal clip/TTS renderer used by /ugcspy-reproduce
  providers/
    types.ts                 # DataProvider interface
    mock.ts                  # deterministic synthetic data
    tiktok-oss.ts            # Bun -> Python bridge (tikwm + yt-dlp; TikTokApi fallbacks)
    scrapecreators.ts        # paid TikTok + IG provider (stub)
    index.ts                 # provider switch
  render/                    # Kling + TTS adapters backing /ugcspy-reproduce
    kling.ts                 # Kling video generation + lip-sync
    openai-tts.ts            # OpenAI TTS adapter
    elevenlabs-tts.ts        # ElevenLabs TTS adapter
    types.ts                 # render request/response types
  lib/
    breakout.ts              # alert math (medians, thresholds, 24h window)
    config.ts                # ~/.ugcspy/config.json
    slack.ts                 # webhook formatter + poster
    venv.ts                  # managed venv paths (~/.ugcspy/venv)
  db/
    schema.ts                # SQLite migrations
    index.ts                 # bun:sqlite open
  types.ts                   # platform-wide types

scripts/
  tiktok_fetch.py            # Python bridge — browser-free hashtag discovery (tikwm) + yt-dlp coverage walk
  requirements.txt           # pip deps (TikTokApi + yt-dlp; playwright only used by the UGCSPY_USE_CHROMIUM=1 fallbacks)
  requirements-audio.txt     # optional Whisper deps (install-deps --with-audio)

test/                        # bun:test suites + Python bridge tests (test_tikwm_adapter.py)
.claude-plugin/              # Claude Code plugin
  plugin.json                # plugin manifest
  commands/                  # slash commands: setup, search, fork, watch, daemon,
                             #   decode, recipe, remix, reproduce
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
