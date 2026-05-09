#!/usr/bin/env bun
import { Command } from "commander";
import { runInit } from "./commands/init.ts";
import { runInstallDeps } from "./commands/install-deps.ts";
import { runSearch, type SearchOptions } from "./commands/search.ts";
import { runWatchAdd, runWatchList, runWatchRemove } from "./commands/watch.ts";
import { runDaemon } from "./commands/daemon.ts";
import { runFork } from "./commands/fork.ts";
import type { Platform } from "./types.ts";

const program = new Command();

program
  .name("ugcspy")
  .description("BigSpy for organic UGC. Search, watch, and fork competitor TikTok + IG Reels.")
  .version("0.1.0");

program
  .command("init")
  .description("Interactive setup — writes ~/.ugcspy/config.json")
  .action(async () => {
    await runInit();
  });

program
  .command("install-deps")
  .description("Install Python deps for the tiktok-oss provider (TikTokApi + Chromium)")
  .action(async () => {
    await runInstallDeps();
  });

program
  .command("search <handle>")
  .description("Rank a competitor's recent organic videos by reach (default) or recency")
  .option("-l, --limit <n>", "max rows", (v) => parseInt(v, 10), 20)
  .option("-s, --sort <mode>", "views | recency", "views")
  .option("-f, --format <tags>", "comma-separated format tags to filter by")
  .option("-p, --platform <name>", "tiktok | instagram | all", "all")
  .option("-d, --days <n>", "trailing window in days", (v) => parseInt(v, 10), 30)
  .option("--refresh", "force refetch even if cached")
  .option("--json", "emit JSON instead of a table")
  .action(async (handle: string, raw) => {
    // Accept legacy "engagement" as an alias for "views" so existing scripts don't break.
    const sort = raw.sort === "engagement" ? "views" : raw.sort;
    const opts: SearchOptions = {
      limit: raw.limit,
      sort,
      format: raw.format,
      platform: raw.platform,
      json: Boolean(raw.json),
      refresh: Boolean(raw.refresh),
      days: raw.days,
    };
    await runSearch(handle, opts);
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
  .command("fork <id-or-url>")
  .description("Generate a creator brief from a video (id from search --json or a video URL)")
  .option("-o, --out <path>", "output path (default: ~/.ugcspy/briefs/)")
  .option("--copy", "copy to clipboard instead of writing a file")
  .action(async (idOrUrl: string, raw) => {
    await runFork(idOrUrl, { out: raw.out, copy: Boolean(raw.copy) });
  });

program.parseAsync(process.argv).catch((err) => {
  console.error(err);
  process.exit(1);
});
