#!/usr/bin/env bun
import { Command } from "commander";
import { version } from "../package.json";
import { runInit } from "./commands/init.ts";
import { runInstallDeps } from "./commands/install-deps.ts";
import { normalizeSearchOptions, runSearch } from "./commands/search.ts";
import { runTranscript } from "./commands/transcript.ts";
import { runWatchAdd, runWatchList, runWatchRemove } from "./commands/watch.ts";
import { runDaemon } from "./commands/daemon.ts";
import { runRender } from "./commands/render.ts";
import { positiveFloat, positiveInt } from "./lib/cli-args.ts";
import type { Platform } from "./types.ts";

const program = new Command();

program
  .name("ugcspy")
  .description("BigSpy for organic UGC. Search, watch, and fork competitor TikTok + IG Reels.")
  // Single-sourced from package.json so the two can't drift.
  .version(version);

program
  .command("init")
  .description("Setup wizard — writes ~/.ugcspy/config.json")
  .option("-y, --yes", "non-interactive; accept defaults (provider=tiktok-oss)")
  .option(
    "--provider <name>",
    "tiktok-oss | scrapecreators | mock (used with --yes)",
  )
  .option(
    "--scraper-api-key <key>",
    "ScrapeCreators API key (only relevant for --provider scrapecreators)",
  )
  .option("--slack-webhook <url>", "default Slack webhook for optional alerts")
  .action(async (raw) => {
    await runInit({
      yes: Boolean(raw.yes),
      provider: raw.provider,
      scraperApiKey: raw.scraperApiKey,
      slackWebhook: raw.slackWebhook,
    });
  });

program
  .command("install-deps")
  .description("Install Python deps for the tiktok-oss provider into a managed venv (TikTokApi + yt-dlp; browser-free). Add --with-browser for the optional Chromium fallback (~150MB). Add --with-audio for Whisper (~+1.5GB, needed by /ugcspy-decode + /ugcspy-remix for spoken-narrative capture).")
  .option("--with-browser", "Also download the Chromium binary for the optional browser-assisted fallback (UGCSPY_USE_CHROMIUM=1) (~150MB)")
  .option("--with-audio", "Also install openai-whisper + torch for audio transcription (~3-5min, ~1.5GB)")
  .action(async (options: { withAudio?: boolean; withBrowser?: boolean }) => {
    await runInstallDeps({ withAudio: !!options.withAudio, withBrowser: !!options.withBrowser });
  });

program
  .command("search <query>")
  .description(
    "Find competitor UGC. `@handle` = one account's catalog; plain word or `#tag` = third-party creators tagging that brand; `--mode keyword \"<phrase>\"` = broad niche/topic discovery (no brand tag required).",
  )
  .option("-l, --limit <n>", "max rows", positiveInt, 20)
  .option("-s, --sort <mode>", "views | recency", "views")
  .option("-p, --platform <name>", "tiktok | instagram | all", "all")
  .option("-d, --days <n>", "trailing window in days", positiveInt, 30)
  .option(
    "-m, --mode <mode>",
    "user | hashtag | keyword — override auto-detection (keyword = niche/topic discovery)",
  )
  .option("--refresh", "force refetch even if cached")
  .option(
    "--prune",
    "with --refresh: treat the fetch as complete — delete in-window cached videos it didn't return (providers can return partial results, so this is opt-in)",
  )
  .option("--json", "emit JSON instead of a table")
  .action(async (query: string, raw) => {
    // normalizeSearchOptions handles the legacy "engagement" sort alias and
    // the --mode whitelist (tested in test/search.test.ts).
    await runSearch(query, normalizeSearchOptions(raw));
  });

program
  .command("transcript <query>")
  .description(
    "Hook + spoken transcript for videos. <query> = a cached brand/#tag/@handle (top N by views), a video id from `search --json`, or a TikTok URL. Classifies talking vs non-talking from the audio (music-bed lyrics don't count). Needs `install-deps --with-audio` + ffmpeg; ~10-40s per uncached video.",
  )
  .option("-t, --top <n>", "how many videos (brand/handle queries)", positiveInt, 3)
  .option("--talking", "only videos with real speech (scans down the ranked list)")
  .option("--non-talking", "only music/ambience videos with no real speech")
  .option("-d, --days <n>", "trailing window in days", positiveInt, 30)
  .option("-p, --platform <name>", "tiktok", "tiktok")
  .option("--json", "emit JSON instead of formatted sections")
  .action(async (query: string, raw) => {
    await runTranscript(query, {
      top: raw.top,
      talking: Boolean(raw.talking),
      nonTalking: Boolean(raw.nonTalking),
      days: raw.days,
      platform: raw.platform as Platform,
      json: Boolean(raw.json),
    });
  });

const watch = program.command("watch").description("Manage breakout-alert watches");

watch
  .command("add <handle>")
  .description("Watch a competitor and Slack-alert on breakout (≥ threshold × trailing median)")
  .option("--slack-webhook <url>", "Slack incoming webhook URL")
  .option("--threshold <n>", "breakout multiplier", positiveFloat, 2.0)
  .option("-p, --platform <name>", "tiktok | instagram", "tiktok")
  .action(async (handle: string, raw) => {
    await runWatchAdd(handle, {
      slackWebhook: raw.slackWebhook,
      threshold: raw.threshold,
      platform: raw.platform as Platform,
    });
  });

watch.command("list").description("List configured watches").action(async () => {
  await runWatchList();
});

watch.command("remove <id>").description("Remove a watch by id").action(async (id: string) => {
  await runWatchRemove(id);
});

program
  .command("daemon")
  .description("Poll watches and fire Slack alerts on breakouts")
  .option("--once", "run a single tick and exit")
  .option(
    "--interval <ms>",
    "ms between ticks when running continuously",
    positiveInt,
    21_600_000, // 6h default
  )
  .option("-d, --days <n>", "trailing window in days", positiveInt, 30)
  .action(async (raw) => {
    await runDaemon({
      once: Boolean(raw.once),
      intervalMs: raw.interval,
      windowDays: raw.days,
    });
  });

program
  .command("render")
  .description(
    "Internal: render one clip or one TTS segment. Stdin = JSON request, stdout = JSON result. Used by the video-recipe composer; users should run `/ugcspy-reproduce` instead.",
  )
  .action(async () => {
    await runRender();
  });

program.parseAsync(process.argv).catch((err) => {
  console.error(err);
  process.exit(1);
});
