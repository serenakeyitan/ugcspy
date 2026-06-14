import chalk from "chalk";
import Table from "cli-table3";
import { openDb } from "../db/index.ts";
import { loadConfig } from "../lib/config.ts";
import type { Competitor, Platform, Watch } from "../types.ts";

export interface WatchOptions {
  slackWebhook?: string;
  threshold: number;
  platform: Platform;
  // Absolute view-count alert: fire when a tracked video crosses this many
  // views (overrides the relative threshold mode). Undefined = relative mode.
  viewThreshold?: number;
  // Optional target brand for the reminder's /ugcspy-rebrand CTA.
  remixBrand?: string;
}

export async function runWatchAdd(
  handleRaw: string,
  opts: WatchOptions,
  db: ReturnType<typeof openDb> = openDb(),
): Promise<void> {
  const handle = handleRaw.startsWith("@") ? handleRaw : `@${handleRaw}`;
  // The CLI layer validates --threshold, but guard here too for programmatic
  // callers: SQLite binds NaN as NULL, and a NULL multiplier turns EVERY video
  // into a "breakout" (median * null = 0).
  if (!Number.isFinite(opts.threshold) || opts.threshold <= 0) {
    console.error(chalk.red(`--threshold must be a positive number (got ${opts.threshold}).`));
    process.exit(1);
  }
  // Absolute view-threshold mode (--view-threshold): must be a positive integer.
  const absoluteMode = opts.viewThreshold !== undefined;
  if (absoluteMode && (!Number.isFinite(opts.viewThreshold) || opts.viewThreshold! <= 0)) {
    console.error(
      chalk.red(`--view-threshold must be a positive number (got ${opts.viewThreshold}).`),
    );
    process.exit(1);
  }
  const viewThreshold = absoluteMode ? Math.round(opts.viewThreshold!) : null;
  const remixBrand = opts.remixBrand?.trim() || null;
  const webhook = opts.slackWebhook ?? loadConfig().default_slack_webhook;
  if (!webhook) {
    console.error(
      chalk.red("No Slack webhook URL provided. Pass --slack-webhook or run `ugcspy init`."),
    );
    process.exit(1);
  }
  try {
    new URL(webhook);
  } catch {
    console.error(chalk.red(`Slack webhook is not a valid URL: ${webhook}`));
    process.exit(1);
  }

  db.prepare(`INSERT OR IGNORE INTO competitors (handle, platform) VALUES (?, ?)`).run(
    handle,
    opts.platform,
  );
  const competitor = db
    .prepare(`SELECT id FROM competitors WHERE handle = ? AND platform = ?`)
    .get(handle, opts.platform) as { id: number } | undefined;
  if (!competitor) throw new Error("Failed to register competitor");

  // Re-running `watch add` for the same handle+webhook is the natural way to
  // change a threshold — update in place instead of silently creating a
  // duplicate watch (which would double every Slack alert forever). A
  // DIFFERENT webhook for the same handle is a legit second channel.
  const existing = db
    .prepare(`SELECT id FROM watches WHERE competitor_id = ? AND slack_webhook_url = ?`)
    .get(competitor.id, webhook) as { id: number } | undefined;
  if (existing) {
    db.prepare(
      `UPDATE watches SET threshold_multiplier = ?, view_threshold = ?, remix_brand = ? WHERE id = ?`,
    ).run(opts.threshold, viewThreshold, remixBrand, existing.id);
    console.log(
      chalk.green(
        absoluteMode
          ? `✓ Updated watch for ${handle} on ${opts.platform} → reminder at ${viewThreshold!.toLocaleString()} views${remixBrand ? ` (remix → ${remixBrand})` : ""}.`
          : `✓ Updated existing watch for ${handle} on ${opts.platform} → ${opts.threshold}x baseline.`,
      ),
    );
    return;
  }

  // Absolute-threshold watches skip warmup (the milestone is meaningful at once);
  // relative-breakout watches stay warming_up until the cold-start gate clears.
  db.prepare(
    `INSERT INTO watches (competitor_id, slack_webhook_url, threshold_multiplier, view_threshold, remix_brand, state)
     VALUES (?, ?, ?, ?, ?, ?)`,
  ).run(
    competitor.id,
    webhook,
    opts.threshold,
    viewThreshold,
    remixBrand,
    absoluteMode ? "active" : "warming_up",
  );

  if (absoluteMode) {
    console.log(
      chalk.green(
        `✓ Reminding on ${handle} (${opts.platform}) when a video crosses ${viewThreshold!.toLocaleString()} views${remixBrand ? ` — with a /ugcspy-rebrand CTA for ${remixBrand}` : ""}.`,
      ),
    );
  } else {
    console.log(
      chalk.green(`✓ Watching ${handle} on ${opts.platform} at ${opts.threshold}x baseline.`),
    );
    console.log(chalk.dim(`Cold-start: alerts stay warming_up until 7 days + ≥5 videos.`));
  }
  console.log(`Run ${chalk.cyan("ugcspy daemon --once")} to manually poll, or set up cron.`);
}

export async function runWatchList(): Promise<void> {
  const db = openDb();
  const rows = db
    .prepare(
      `SELECT w.id, w.threshold_multiplier, w.view_threshold, w.remix_brand, w.state, w.created_at, c.handle, c.platform
       FROM watches w JOIN competitors c ON c.id = w.competitor_id
       ORDER BY w.created_at DESC`,
    )
    .all() as (Pick<
      Watch,
      "id" | "threshold_multiplier" | "view_threshold" | "remix_brand" | "state" | "created_at"
    > &
      Pick<Competitor, "handle" | "platform">)[];

  if (rows.length === 0) {
    console.log(chalk.yellow("No watches configured. Try `ugcspy watch add @glossier`."));
    return;
  }
  const table = new Table({
    head: ["ID", "Handle", "Platform", "Trigger", "Remix", "State", "Added"],
    style: { head: ["cyan"], border: ["gray"] },
  });
  for (const r of rows) {
    // Absolute mode shows the view milestone; relative mode shows the multiplier.
    const trigger =
      r.view_threshold != null ? `≥ ${r.view_threshold.toLocaleString()} views` : `${r.threshold_multiplier}x median`;
    table.push([
      String(r.id),
      r.handle,
      r.platform,
      trigger,
      r.remix_brand ?? chalk.dim("—"),
      r.state === "active" ? chalk.green(r.state) : chalk.yellow(r.state),
      r.created_at,
    ]);
  }
  console.log(table.toString());
}

export async function runWatchRemove(idStr: string): Promise<void> {
  const id = Number(idStr);
  if (!Number.isInteger(id)) {
    console.error(chalk.red("watch id must be an integer"));
    process.exit(1);
  }
  const db = openDb();
  const result = db.prepare(`DELETE FROM watches WHERE id = ?`).run(id);
  if (result.changes === 0) {
    console.log(chalk.yellow(`No watch with id ${id}.`));
  } else {
    console.log(chalk.green(`✓ Removed watch ${id}.`));
  }
}
