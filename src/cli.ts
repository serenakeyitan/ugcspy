#!/usr/bin/env bun
import { Command } from "commander";
import { runInit } from "./commands/init.ts";
import { runInstallDeps } from "./commands/install-deps.ts";
import { runSearch, type SearchOptions } from "./commands/search.ts";
import { runWatchAdd, runWatchList, runWatchRemove } from "./commands/watch.ts";
import { runDaemon } from "./commands/daemon.ts";
import { runRender } from "./commands/render.ts";
import type { Platform } from "./types.ts";

const program = new Command();

program
  .name("ugcspy")
  .description("BigSpy for organic UGC. Search, watch, and fork competitor TikTok + IG Reels.")
  .version("0.2.0");

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
  .description("Install Python deps for the tiktok-oss provider (TikTokApi + Chromium). Add --with-audio for Whisper (~+1.5GB, needed by /ugcspy-decode + /ugcspy-remix for spoken-narrative capture).")
  .option("--with-audio", "Also install openai-whisper + torch for audio transcription (~3-5min, ~1.5GB)")
  .action(async (options: { withAudio?: boolean }) => {
    await runInstallDeps({ withAudio: !!options.withAudio });
  });

program
  .command("search <query>")
  .description(
    "Find competitor UGC. `@handle` searches one account; plain word or `#tag` searches third-party creators promoting that brand.",
  )
  .option("-l, --limit <n>", "max rows", (v) => parseInt(v, 10), 20)
  .option("-s, --sort <mode>", "views | recency", "views")
  .option("-p, --platform <name>", "tiktok | instagram | all", "all")
  .option("-d, --days <n>", "trailing window in days", (v) => parseInt(v, 10), 30)
  .option(
    "-m, --mode <mode>",
    "user | hashtag — override auto-detection from query prefix",
  )
  .option("--refresh", "force refetch even if cached")
  .option("--json", "emit JSON instead of a table")
  .action(async (query: string, raw) => {
    // Accept legacy "engagement" as an alias for "views" so existing scripts don't break.
    const sort = raw.sort === "engagement" ? "views" : raw.sort;
    const mode = raw.mode === "user" || raw.mode === "hashtag" ? raw.mode : undefined;
    const opts: SearchOptions = {
      limit: raw.limit,
      sort,
      platform: raw.platform,
      json: Boolean(raw.json),
      refresh: Boolean(raw.refresh),
      days: raw.days,
      mode,
    };
    await runSearch(query, opts);
  });

const watch = program.command("watch").description("Manage breakout-alert watches");

watch
  .command("add <handle>")
  .description("Watch a competitor and Slack-alert on breakout (≥ threshold × trailing median)")
  .option("--slack-webhook <url>", "Slack incoming webhook URL")
  .option("--threshold <n>", "breakout multiplier", (v) => parseFloat(v), 2.0)
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
    (v) => parseInt(v, 10),
    21_600_000, // 6h default
  )
  .option("-d, --days <n>", "trailing window in days", (v) => parseInt(v, 10), 30)
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
