import chalk from "chalk";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { effectiveAnthropicKey, loadConfig } from "../lib/config.ts";
import { detectBreakouts, evaluateWatchState, filterRecent24h } from "../lib/breakout.ts";
import { postBreakoutAlert } from "../lib/slack.ts";
import { extractHook } from "../extractors/hook.ts";
import { classifyFormat } from "../extractors/format.ts";
import { getProvider } from "../providers/index.ts";
import type { Competitor, FormatTag, Platform, RawVideo, VideoRecord, Watch } from "../types.ts";

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
        const enriched = await enrichVideos(fresh, effectiveAnthropicKey(config));
        upsertVideos(db, w.competitor_id, enriched);

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
          const result = await postBreakoutAlert(
            w.slack_webhook_url,
            { id: w.competitor_id, handle: w.handle, platform: w.platform as Platform, added_at: "" },
            c,
          );
          if (result.ok) {
            recordFired(db, c.video.id, w.id);
          } else {
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

interface EnrichedVideo extends RawVideo {
  hook_source: string;
  hook_text: string;
  hook_confidence: number;
  format_tag: FormatTag | null;
}

async function enrichVideos(
  videos: RawVideo[],
  anthropicKey: string | undefined,
): Promise<EnrichedVideo[]> {
  const out: EnrichedVideo[] = [];
  for (const v of videos) {
    const hook = await extractHook(v, anthropicKey);
    let format_tag: FormatTag | null = null;
    try {
      format_tag = await classifyFormat(v, anthropicKey);
    } catch {
      /* ignore */
    }
    out.push({
      ...v,
      hook_source: hook.source,
      hook_text: hook.text,
      hook_confidence: hook.confidence,
      format_tag,
    });
  }
  return out;
}

function upsertVideos(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  videos: EnrichedVideo[],
): void {
  const stmt = db.prepare(`
    INSERT INTO videos (
      competitor_id, platform, external_id, posted_at, caption, thumbnail_url, video_url,
      view_count, like_count, comment_count, share_count,
      hook_source, hook_text, hook_confidence, format_tag, raw_metrics_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(platform, external_id) DO UPDATE SET
      view_count = excluded.view_count,
      like_count = excluded.like_count,
      comment_count = excluded.comment_count,
      share_count = excluded.share_count,
      fetched_at = datetime('now')
  `);
  const tx = db.transaction((rows: EnrichedVideo[]) => {
    for (const v of rows) {
      stmt.run(
        competitorId,
        v.platform,
        v.external_id,
        v.posted_at,
        v.caption,
        v.thumbnail_url,
        v.video_url,
        v.view_count,
        v.like_count,
        v.comment_count,
        v.share_count,
        v.hook_source,
        v.hook_text,
        v.hook_confidence,
        v.format_tag,
        JSON.stringify({}),
      );
    }
  });
  tx(videos);
}

function readTrailingWindow(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  windowDays: number,
): VideoRecord[] {
  const cutoff = new Date(Date.now() - windowDays * 86_400_000).toISOString();
  return db
    .prepare(
      `SELECT * FROM videos WHERE competitor_id = ? AND posted_at >= ? ORDER BY posted_at DESC`,
    )
    .all(competitorId, cutoff) as VideoRecord[];
}

function alreadyFired(db: ReturnType<typeof openDb>, videoId: number, watchId: number): boolean {
  const row = db
    .prepare(`SELECT 1 FROM alerts_fired WHERE video_id = ? AND watch_id = ?`)
    .get(videoId, watchId);
  return Boolean(row);
}

function recordFired(db: ReturnType<typeof openDb>, videoId: number, watchId: number): void {
  db.prepare(`INSERT OR IGNORE INTO alerts_fired (video_id, watch_id) VALUES (?, ?)`).run(
    videoId,
    watchId,
  );
}

function setWatchState(db: ReturnType<typeof openDb>, id: number, state: "warming_up" | "active"): void {
  db.prepare(`UPDATE watches SET state = ? WHERE id = ?`).run(state, id);
}
