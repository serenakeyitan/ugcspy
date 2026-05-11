---
description: One-shot setup for ugcspy on a fresh machine — installs deps, configures, runs a verification search
argument-hint: "(no arguments)"
---

You are doing the full ugcspy onboarding for the user. Treat them as a new install: they may not have Bun, Python, or the repo cloned. Walk through every step, run each command via the Bash tool, and surface clear errors with fixes.

## Goal

By the end, the user can run `/ugcspy-search <brand>` and see real TikTok UGC results.

## Step 1 — Check prerequisites

Run these checks in parallel via Bash:

```bash
which bun && bun --version
which python3 && python3 --version
which git
```

If any are missing:
- **bun missing**: tell the user to run `curl -fsSL https://bun.sh/install | bash` and re-invoke `/ugcspy-setup` afterward. Stop here; they need to restart their terminal first.
- **python3 missing**: macOS → `brew install python3`. Linux → `sudo apt install python3 python3-pip`. Stop here.
- **git missing**: macOS → install Xcode CLT (`xcode-select --install`). Linux → `sudo apt install git`. Stop here.

Otherwise, continue.

## Step 2 — Locate or clone the repo

Check if the repo already exists. If not, ask the user where they want it (default: `~/code/ugcspy`):

```bash
if [ -d ~/code/ugcspy ]; then
  echo "Found at ~/code/ugcspy"
elif [ -d ./ugcspy ]; then
  echo "Found at ./ugcspy"
else
  mkdir -p ~/code && cd ~/code && git clone https://github.com/serenakeyitan/ugcspy.git
fi
```

`cd` into the repo directory for the remaining steps.

## Step 3 — Install JS deps

```bash
cd <repo-path> && bun install
```

If it fails with `commander` or `ora` version errors, run `rm -rf node_modules bun.lock && bun install`. If it still fails, paste the exact error back to the user.

## Step 4 — Install Python deps + Chromium (the slow step, ~30s + ~150MB)

```bash
cd <repo-path> && bun run src/cli.ts install-deps
```

Tell the user upfront: "This downloads a Chromium browser binary (~150MB) used to bypass TikTok's bot detection. One-time, ~30 seconds."

If it fails:
- `pip install failed` → check Python version (need 3.9+), check internet, try `python3 -m pip install --upgrade pip` then re-run.
- `playwright install failed` → likely a network issue, retry. If persistent, manual: `python3 -m playwright install chromium`.

## Step 5 — Configure

```bash
cd <repo-path> && bun run src/cli.ts init
```

The init wizard asks 3 things. Recommend these answers:
- **Data provider**: `tiktok-oss` (free, default — covers TikTok)
- **ScrapeCreators API key**: leave blank (only needed for Instagram Reels — paid)
- **Default Slack webhook**: leave blank (only needed for the optional alerts feature)

## Step 6 — Link the binary for global access

```bash
cd <repo-path> && bun link
```

This makes `ugcspy` available on PATH. Verify with `which ugcspy`.

## Step 7 — Run a verification search

```bash
ugcspy search befreed --platform tiktok --limit 10
```

This should return ~10 TikTok creators promoting BeFreed. Wall time: ~60-90 seconds (four-pass discovery; subsequent searches on the same brand are cached and instant).

A Chromium window may flash open during the scrape — tell the user this is normal (TikTok blocks pure headless mode).

## Step 8 — Handle bot detection (if it trips)

If the search returns 0 videos or fails with "TikTok returned an empty response", the user's IP got bot-flagged. The fix is to set `MS_TOKEN` from their browser cookies.

Walk them through:
1. Open tiktok.com in Chrome (or whichever browser they use).
2. Open DevTools → Application → Cookies → `https://www.tiktok.com`.
3. Find the cookie named `msToken` and copy its value.
4. Set it in their shell:
   ```bash
   echo 'export MS_TOKEN="<paste-value-here>"' >> ~/.zshrc   # or ~/.bashrc
   source ~/.zshrc
   ```
5. Re-run the search.

If the search succeeds, they're done.

## Step 9 — Show them what they can do next

Print this summary inline in the chat:

```
✓ ugcspy is set up and working.

Next searches:
  ugcspy search <brand>                  # find creators promoting a brand
  ugcspy search @<handle>                # brand's own account posts
  ugcspy search <brand> --sort recency   # newest first instead of highest-reach
  ugcspy search <brand> --json | jq ...  # pipe results

Or inside Claude Code:
  /ugcspy-search befreed
  /ugcspy-fork <video-url>               # turn a competitor video into a creator brief

Optional (alerts on breakout videos via Slack):
  ugcspy watch add <brand> --slack-webhook <url>
  ugcspy daemon --once
```

## Honest caveats to mention

- First search on a brand takes ~60-90 seconds (cached after).
- Free TikTok scraper has a coverage ceiling — for the absolute top 1M+ view UGC that doesn't tag #brand explicitly, you'd need paid ScrapeCreators (not wired up yet).
- Instagram Reels requires ScrapeCreators (paid) — the free path is TikTok-only.
- Rate-limiting is real: running back-to-back searches at high concurrency can trip TikTok for ~10-20 min. Default `UGCSPY_CONCURRENCY=12` is empirically safe.
