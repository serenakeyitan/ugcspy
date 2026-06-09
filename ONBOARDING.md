# ugcspy onboarding

This file is the canonical install + first-run guide. The README onboarding section and the GitHub Release notes both point Claude here to keep instructions in one place.

If you're a human reading this directly: skip to "Manual install" at the bottom.

If you're Claude Code reading this on behalf of a user installing ugcspy: do the steps below end-to-end via the Bash tool, surfacing errors with fixes.

## Goal

By the end, the user can run `ugcspy search <brand>` from any directory and see real TikTok UGC results, and (optionally) `/ugcspy-recipe <id>` to reverse-engineer a video into a reproducible recipe.

## Step 1 — Prerequisites

Run these checks in parallel:

```bash
bun --version
python3 --version
git --version
```

If any are missing:
- **bun**: `curl -fsSL https://bun.sh/install | bash` then restart terminal. Stop here, ask the user to re-invoke.
- **python3**: macOS `brew install python3`, Linux `sudo apt install python3 python3-pip`. Stop.
- **git**: macOS `xcode-select --install`, Linux `sudo apt install git`. Stop.

## Step 2 — Clone the repo

Default location is `~/code/ugcspy`. Check if it's already there; if not:

```bash
mkdir -p ~/code && cd ~/code && git clone https://github.com/serenakeyitan/ugcspy.git
```

`cd ~/code/ugcspy` for the rest.

## Step 3 — JS deps + Python deps

```bash
bun install
bun run src/cli.ts install-deps              # core: ~30s + ~150MB
# OR, if the user will use /ugcspy-decode + /ugcspy-remix for AI remix briefs:
bun run src/cli.ts install-deps --with-audio  # +Whisper: ~3-5min + ~1.5GB total
```

The core install pulls down the Python deps for the `user` and `keyword` modes (and an optional Chromium binary, ~150MB, used only as a fallback — see below). The default `ugcspy search <brand>` hashtag flow is browser-free and does NOT need Chromium to run. Tell the user upfront so the download size isn't a surprise.

`--with-audio` ALSO installs openai-whisper + torch for spoken-narrative capture (口型 / lip-sync source for AI remix). Strongly recommended if the user plans to use `/ugcspy-decode` or `/ugcspy-remix` — the spoken audio is the primary content for most UGC formats, and without Whisper the decoder only sees on-screen overlay text. Skip if the user only wants `ugcspy search` + `/ugcspy-fork`.

## Step 4 — Configure

```bash
bun run src/cli.ts init --yes
```

Non-interactive — accepts the recommended defaults (provider=tiktok-oss, no scraper key, no Slack webhook).

If the user explicitly wants a different provider or to add an API key now, pass the relevant flag:

```bash
bun run src/cli.ts init --yes --provider scrapecreators --scraper-api-key <key>
bun run src/cli.ts init --yes --provider tiktok-oss --slack-webhook https://hooks.slack.com/...
```

Or drop `--yes` for the interactive wizard if the user prefers to walk through the prompts themselves.

## Step 5 — Install the binary globally

```bash
bun run build
npm install --global .
```

`bun run build` produces `dist/cli.js`; `npm install --global .` symlinks it onto PATH so the user can run `ugcspy` from any directory.

We use `npm` here rather than `bun install --global .` because the bun version has a known [DependencyLoop bug](https://github.com/oven-sh/bun/issues) when installing a package globally from its own source directory. `npm install --global .` is the standard, reliable way to install a local Node CLI tool globally; it works regardless of which package manager built the project.

Verify:

```bash
which ugcspy        # should print a path (~/.bun/bin/ugcspy, /usr/local/bin/ugcspy, or similar)
ugcspy --version    # must print 0.2.0
```

If `which ugcspy` is empty, the npm global bin dir isn't on PATH:

```bash
# Find where npm puts globals
npm prefix -g
# Then add that prefix's bin to PATH, e.g.:
echo 'export PATH="$(npm prefix -g)/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

If `--version` prints an older number, the user has a stale install on PATH. Run `npm uninstall -g ugcspy && npm install --global .` from the current repo directory.

## Step 6 — Verification search

```bash
ugcspy search befreed --platform tiktok --limit 10
```

Wall time ~60-90s. Discovery runs browser-free over pure HTTP (the tikwm relay) — no Chromium window, no login. The wall time is mostly Stage 2 coverage: yt-dlp walking each ranked creator's full catalog (16-way concurrent by default).

## Step 7 — Fallbacks (only if step 6 returns 0 videos)

Hashtag discovery is browser-free and does not use `MS_TOKEN`, so a 0-result run is almost always a transient relay/network blip — retry the search first; the stable tikwm client (retry + backoff) usually self-heals. If you want to widen the gap between feed reads on a flaky connection, bump `UGCSPY_HASHTAG_FEED_DELAY`.

If retries still return nothing, you can enable the optional Chromium fallback (OFF by default because it crashes/hangs on most hosts):

```bash
UGCSPY_USE_CHROMIUM=1 ugcspy search befreed --platform tiktok --limit 10
```

`MS_TOKEN` only matters for the `user` and `keyword` modes (which go through TikTokApi), not for hashtag search. If you do need it for those modes, set it from browser cookies:

1. Open tiktok.com in Chrome → DevTools → Application → Cookies → tiktok.com
2. Copy the `msToken` value
3. `echo 'export MS_TOKEN="<value>"' >> ~/.zshrc && source ~/.zshrc`

## Step 8 — (Optional) Install video-recipe deps

ugcspy bundles [`video-recipe`](https://github.com/serenakeyitan/video-recipe) at `vendor/video-recipe/`. Given any video URL, it produces a `recipe.json` with cuts, per-clip generation prompts, hook pattern, voiceover transcript, and likely models. Useful when a UGC video looks AI-generated and the user wants to recreate it.

Ask the user if they want to install now or skip. If they skip, `/ugcspy-recipe` will surface a clear install error on first call — no harm.

If installing now:

**Python version check first** — video-recipe requires Python ≥ 3.11. ugcspy itself only needs 3.9, so users may have an older Python.

```bash
python3 --version
```

If 3.9 or 3.10: upgrade first.
- macOS: `brew install python@3.11`
- Linux: `pyenv install 3.11` or `sudo apt install python3.11`

Then use the correct binary (`python3.11` if you just installed it):

```bash
brew install ffmpeg tesseract                                  # or: sudo apt install ffmpeg tesseract-ocr
cd vendor/video-recipe && python3.11 -m pip install --user -e ".[dev]"
cd vendor/video-recipe && python3.11 -m scripts.doctor         # must report all 11 ✓
```

If doctor reports any ✗, fix those before calling step 8 done.

## Step 9 — Show next-steps summary

Print this to the user:

```
✓ ugcspy 0.2.0 is set up and working.

Search commands:
  ugcspy search <brand>                find creators promoting a brand
  ugcspy search @<handle>              brand's own account posts
  ugcspy search <brand> --sort recency newest first
  ugcspy search <brand> --json         pipe-friendly output

Inside Claude Code:
  /ugcspy-search <brand>               ranked third-party UGC creators
  /ugcspy-fork <id>                    quick creator brief (hook + beat sheet)
  /ugcspy-decode <id>                  deep production decode — format, overlay
                                       narrative, brand-pitch placement, shot
                                       list. Writes decode.json + decode.html.
                                       For human-shot videos AND AI montages.
  /ugcspy-remix <target> <source>      take video A's format, write a brief for
                                       creator B to shoot their own version.
                                       Cross-video format transfer.
  /ugcspy-recipe <id>                  full reverse-engineered recipe for AI
                                       reproduction (uses bundled video-recipe)
  /ugcspy-reproduce <id>               render reproduction.mp4 via paid APIs
                                       (Kling + OpenAI TTS) — AI-montage only

Optional (Slack alerts on breakout videos):
  ugcspy watch add <brand> --slack-webhook <url>
  ugcspy daemon --once
```

The most common flow for "I found a great video, I want to make something like it":

```
/ugcspy-search <brand>           # find ranked UGC
/ugcspy-decode <id>              # understand HOW that one was made
/ugcspy-remix <id> <other-id>    # OR brief a different creator to shoot it
/ugcspy-fork <id>                # OR just get a quick brief
```

## Honest limits to mention if asked

- First search per brand takes ~60-90s; cached after.
- Free path covers TikTok only; Instagram needs paid ScrapeCreators.
- Creator coverage is ~89% of the brand's UGC roster (51/57 for BeFreed); the unreachable few are very-low-view creators whose videos never enter any challenge feed, so the hashtag + follow-graph snowball never sees them.
- Stage 2 coverage walks each creator's full public catalog from www.tiktok.com — that path is not rate-limited, so the 16-way default (`UGCSPY_WALK_CONCURRENCY=16`) is safe. Lower it if a host is CPU-bound.
- video-recipe step 4 (vision reading) needs Claude Code or another harness with file + vision tools.

---

## Manual install (no Claude Code)

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run src/cli.ts install-deps     # ~30s + ~150MB (Python deps + optional Chromium fallback; hashtag search is browser-free)
# (add --with-audio if you'll use /ugcspy-decode or /ugcspy-remix — adds Whisper for spoken-audio capture, ~3-5min + ~1.5GB)
bun run src/cli.ts init --yes        # non-interactive; defaults to tiktok-oss
bun run build                        # produces dist/cli.js
npm install --global .               # symlinks ugcspy onto PATH
ugcspy search befreed --platform tiktok --limit 10

# Optional: video-recipe deps (needs Python 3.11+)
python3 --version                                       # confirm 3.11+
brew install ffmpeg tesseract                           # macOS (or apt on Linux)
cd vendor/video-recipe && pip install -e ".[dev]"
cd vendor/video-recipe && python -m scripts.doctor      # should be all ✓
```
