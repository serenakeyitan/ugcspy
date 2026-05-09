import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { effectiveAnthropicKey, loadConfig } from "../lib/config.ts";
import { extractHook } from "../extractors/hook.ts";
import { classifyFormat } from "../extractors/format.ts";
import { getProvider } from "../providers/index.ts";
import type { FormatTag, Platform, RawVideo, VideoRecord } from "../types.ts";

export interface SearchOptions {
  limit: number;
  sort: "engagement" | "recency";
  format?: string;
  platform?: Platform | "all";
  json: boolean;
  refresh: boolean;
  days: number;
}

export async function runSearch(handleRaw: string, opts: SearchOptions): Promise<void> {
  const handle = normalizeHandle(handleRaw);
  const config = loadConfig();
  const db = openDb();

  const platforms: Platform[] =
    !opts.platform || opts.platform === "all" ? ["tiktok", "instagram"] : [opts.platform];
  const provider = getProvider(config);

  const allVideos: VideoRecord[] = [];

  for (const platform of platforms) {
    const competitorId = upsertCompetitor(db, handle, platform);
    const cached = readCachedVideos(db, competitorId, platform);
    let videos = cached;

    if (opts.refresh || cached.length === 0) {
      const spinner = opts.json
        ? null
        : ora(`Fetching ${chalk.cyan(handle)} on ${platform}...`).start();
      try {
        const fresh = await provider.fetchRecentVideos(handle, platform, opts.days);
        const enriched = await enrichVideos(fresh, effectiveAnthropicKey(config));
        upsertVideos(db, competitorId, enriched);
        videos = readCachedVideos(db, competitorId, platform);
        spinner?.succeed(`${platform}: ${chalk.cyan(videos.length)} videos`);
      } catch (err) {
        spinner?.fail(`${platform}: ${(err as Error).message}`);
        continue;
      }
    }
    allVideos.push(...videos);
  }

  let rows = allVideos;
  if (opts.format) {
    const wanted = opts.format.split(",").map((s) => s.trim());
    rows = rows.filter((v) => v.format_tag && wanted.includes(v.format_tag));
  }
  rows.sort((a, b) =>
    opts.sort === "engagement"
      ? engagement(b) - engagement(a)
      : new Date(b.posted_at).getTime() - new Date(a.posted_at).getTime(),
  );
  rows = rows.slice(0, opts.limit);

  if (opts.json) {
    console.log(JSON.stringify(rows, null, 2));
    return;
  }
  printTable(handle, rows);
}

function normalizeHandle(handle: string): string {
  return handle.startsWith("@") ? handle : `@${handle}`;
}

function upsertCompetitor(db: ReturnType<typeof openDb>, handle: string, platform: Platform): number {
  db.prepare(
    `INSERT OR IGNORE INTO competitors (handle, platform) VALUES (?, ?)`,
  ).run(handle, platform);
  const row = db
    .prepare(`SELECT id FROM competitors WHERE handle = ? AND platform = ?`)
    .get(handle, platform) as { id: number } | undefined;
  if (!row) throw new Error("Failed to upsert competitor");
  return row.id;
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
  const enriched: EnrichedVideo[] = [];
  for (const v of videos) {
    const hook = await extractHook(v, anthropicKey);
    const format_tag = await safeClassify(v, anthropicKey);
    enriched.push({
      ...v,
      hook_source: hook.source,
      hook_text: hook.text,
      hook_confidence: hook.confidence,
      format_tag,
    });
  }
  return enriched;
}

async function safeClassify(
  video: RawVideo,
  anthropicKey: string | undefined,
): Promise<FormatTag | null> {
  try {
    return await classifyFormat(video, anthropicKey);
  } catch {
    return null;
  }
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
      fetched_at = datetime('now'),
      hook_source = excluded.hook_source,
      hook_text = excluded.hook_text,
      hook_confidence = excluded.hook_confidence,
      format_tag = excluded.format_tag
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

function readCachedVideos(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  platform: Platform,
): VideoRecord[] {
  return db
    .prepare(`SELECT * FROM videos WHERE competitor_id = ? AND platform = ? ORDER BY posted_at DESC`)
    .all(competitorId, platform) as VideoRecord[];
}

function engagement(v: VideoRecord): number {
  if (v.view_count === 0) return 0;
  return (v.like_count + v.comment_count * 2 + v.share_count * 3) / v.view_count;
}

function printTable(handle: string, rows: VideoRecord[]): void {
  if (rows.length === 0) {
    console.log(chalk.yellow(`\nNo videos found for ${handle}.`));
    return;
  }
  const table = new Table({
    head: ["#", "Platform", "Posted", "Views", "Eng%", "Format", "Hook"],
    style: { head: ["cyan"], border: ["gray"] },
    colWidths: [4, 10, 12, 12, 7, 16, 60],
    wordWrap: true,
  });
  rows.forEach((v, i) => {
    table.push([
      String(i + 1),
      v.platform,
      v.posted_at.slice(0, 10),
      v.view_count.toLocaleString(),
      `${(engagement(v) * 100).toFixed(1)}%`,
      v.format_tag ?? chalk.dim("—"),
      v.hook_text || chalk.dim(`(${v.hook_source})`),
    ]);
  });
  console.log("");
  console.log(chalk.bold(`${handle} — top ${rows.length}`));
  console.log(table.toString());
}
