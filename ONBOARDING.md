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

## Step 3 — JS deps + Python deps + Chromium

```bash
bun install
bun run src/cli.ts install-deps
```

The second command downloads a Chromium binary (~150MB, ~30s) used to bypass TikTok bot detection. Tell the user upfront.

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

Wall time ~60-90s (four-pass discovery). A Chromium window briefly flashes — that's normal.

## Step 7 — Bot-detection fallback (only if step 6 returns 0 videos)

If TikTok flagged the IP, set `MS_TOKEN` from browser cookies:

1. Open tiktok.com in Chrome → DevTools → Application → Cookies → tiktok.com
2. Copy the `msToken` value
3. `echo 'export MS_TOKEN="<value>"' >> ~/.zshrc && source ~/.zshrc`
4. Retry the search

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
  /ugcspy-recipe <id>                  full reverse-engineered recipe
                                       (cuts, per-clip prompts, hook pattern,
                                       voiceover — uses bundled video-recipe)

Optional (Slack alerts on breakout videos):
  ugcspy watch add <brand> --slack-webhook <url>
  ugcspy daemon --once
```

## Honest limits to mention if asked

- First search per brand takes ~60-90s; cached after.
- Free path covers TikTok only; Instagram needs paid ScrapeCreators.
- Coverage ceiling ~440 videos per active brand; viral posts that don't tag #brand are unreachable on the free path.
- TikTok rate-limits aggressive scraping (10-20 min cooldown). Default concurrency=12 is empirically safe.
- video-recipe step 4 (vision reading) needs Claude Code or another harness with file + vision tools.

---

## Manual install (no Claude Code)

```bash
git clone https://github.com/serenakeyitan/ugcspy.git
cd ugcspy
bun install
bun run src/cli.ts install-deps     # ~30s + 150MB Chromium download
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
