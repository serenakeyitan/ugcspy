import chalk from "chalk";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { upsertVideos } from "../db/videos.ts";
import { loadConfig } from "../lib/config.ts";
import { detectBreakouts, evaluateWatchState, filterRecent24h } from "../lib/breakout.ts";
import { postBreakoutAlert } from "../lib/slack.ts";
import { getProvider } from "../providers/index.ts";
import type { Competitor, Platform, VideoRecord, Watch } from "../types.ts";

export interface DaemonOptions {
  once: boolean;
  intervalMs: number;
  windowDays: number;
}

export async function runDaemon(opts: DaemonOptions): Promise<void> {
  const config = loadConfig();
  const provider = getProvider(config);
  const db = openDb();

  const tick = async () => {
    const watches = db
      .prepare(
        `SELECT w.*, c.handle, c.platform
         FROM watches w JOIN competitors c ON c.id = w.competitor_id`,
      )
      .all() as (Watch & Pick<Competitor, "handle" | "platform">)[];

    if (watches.length === 0) {
      console.log(chalk.yellow("No watches. Add one with `ugcspy watch add`."));
      return;
    }

    for (const w of watches) {
      const spinner = ora(`Polling ${w.handle} (${w.platform})...`).start();
      try {
        const fresh = await provider.fetchRecentVideos(w.handle, w.platform as Platform, opts.windowDays);
        upsertVideos(db, w.competitor_id, fresh);

        const trailing = readTrailingWindow(db, w.competitor_id, opts.windowDays);
        const status = evaluateWatchState(w.created_at, trailing.length);

        if (status.state === "warming_up") {
          spinner.info(`${w.handle}: warming_up — ${status.reason}`);
          if (w.state !== "warming_up") setWatchState(db, w.id, "warming_up");
          continue;
        }

        if (w.state !== "active") setWatchState(db, w.id, "active");

        const recent = filterRecent24h(trailing);
        const candidates = detectBreakouts(recent, trailing, w.threshold_multiplier);
        const fresh_candidates = candidates.filter((c) => !alreadyFired(db, c.video.id, w.id));

        if (fresh_candidates.length === 0) {
          spinner.succeed(`${w.handle}: no new breakouts (${trailing.length} videos in window)`);
          continue;
        }

        for (const c of fresh_candidates) {
          // Claim the alert in the DB BEFORE posting (INSERT OR IGNORE +
          // changes check) so a concurrent tick can never double-send the
          // same breakout. At-most-once by design: if the Slack post fails
          // after the claim we log and move on — a missed alert beats
          // re-spamming the channel on every subsequent tick.
          if (!claimAlert(db, c.video.id, w.id)) continue;
          const result = await postBreakoutAlert(
            w.slack_webhook_url,
            { id: w.competitor_id, handle: w.handle, platform: w.platform as Platform, added_at: "" },
            c,
          );
          if (!result.ok) {
            console.error(chalk.red(`Slack post failed (${result.status}): ${result.body}`));
          }
        }
        spinner.succeed(
          `${w.handle}: fired ${chalk.cyan(fresh_candidates.length)} alert(s)`,
        );
      } catch (err) {
        spinner.fail(`${w.handle}: ${(err as Error).message}`);
      }
    }
  };

  await tick();
  if (opts.once) return;

  console.log(chalk.dim(`\nLooping every ${opts.intervalMs / 1000}s. Ctrl+C to stop.`));
  while (true) {
    await sleep(opts.intervalMs);
    await tick();
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function readTrailingWindow(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  windowDays: number,
): VideoRecord[] {
  const cutoff = new Date(Date.now() - windowDays * 86_400_000).toISOString();
  // datetime() compare so legacy offset-format posted_at rows still land in
  // the right side of the cutoff (new rows are normalized to UTC Z at upsert).
  return db
    .prepare(
      `SELECT * FROM videos WHERE competitor_id = ? AND datetime(posted_at) >= datetime(?) ORDER BY datetime(posted_at) DESC`,
    )
    .all(competitorId, cutoff) as VideoRecord[];
}

function alreadyFired(db: ReturnType<typeof openDb>, videoId: number, watchId: number): boolean {
  const row = db
    .prepare(`SELECT 1 FROM alerts_fired WHERE video_id = ? AND watch_id = ?`)
    .get(videoId, watchId);
  return Boolean(row);
}

// Atomically claim a (video, watch) alert. Returns true only for the writer
// that actually inserted the row — every concurrent/repeat caller gets false.
// Exported for tests (the claim-before-send dedupe depends on this contract).
export function claimAlert(
  db: ReturnType<typeof openDb>,
  videoId: number,
  watchId: number,
): boolean {
  const result = db
    .prepare(`INSERT OR IGNORE INTO alerts_fired (video_id, watch_id) VALUES (?, ?)`)
    .run(videoId, watchId);
  return result.changes > 0;
}

function setWatchState(db: ReturnType<typeof openDb>, id: number, state: "warming_up" | "active"): void {
  db.prepare(`UPDATE watches SET state = ? WHERE id = ?`).run(state, id);
}
