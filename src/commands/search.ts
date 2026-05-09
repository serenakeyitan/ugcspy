import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import type { Platform, RawVideo, VideoRecord } from "../types.ts";

export type SearchSort = "views" | "recency";

export interface SearchOptions {
  limit: number;
  sort: SearchSort;
  platform?: Platform | "all";
  json: boolean;
  refresh: boolean;
  days: number;
}

// Hook = first sentence-ish chunk of the caption, capped at 120 chars.
// Free, deterministic, no API key. The Claude Code plugin handles richer
// extraction (overlay text via vision, format classification) on demand.
function captionHook(caption: string): { text: string; source: string } {
  const trimmed = caption.trim();
  if (!trimmed) return { text: "", source: "none" };
  const match = trimmed.match(/^[^.!?\n]{1,120}/);
  const text = match ? match[0]!.trim() : trimmed.slice(0, 120);
  return { text, source: "caption" };
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
        upsertVideos(db, competitorId, fresh);
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
  // BigSpy-style ranking: highest reach first by default. SMMs want to see which
  // competitor video got the most views, not which had the best like/view ratio.
  rows.sort((a, b) =>
    opts.sort === "recency"
      ? new Date(b.posted_at).getTime() - new Date(a.posted_at).getTime()
      : b.view_count - a.view_count,
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

function upsertVideos(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  videos: RawVideo[],
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
      hook_text = excluded.hook_text,
      hook_source = excluded.hook_source
  `);
  const tx = db.transaction((rows: RawVideo[]) => {
    for (const v of rows) {
      const hook = captionHook(v.caption);
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
        hook.source,
        hook.text,
        hook.text ? 1.0 : 0,
        null,
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

function printTable(handle: string, rows: VideoRecord[]): void {
  if (rows.length === 0) {
    console.log(chalk.yellow(`\nNo videos found for ${handle}.`));
    return;
  }
  const table = new Table({
    head: ["#", "Platform", "Posted", "Views", "Likes", "Hook"],
    style: { head: ["cyan"], border: ["gray"] },
    colWidths: [4, 10, 12, 12, 10, 65],
    wordWrap: true,
  });
  rows.forEach((v, i) => {
    table.push([
      String(i + 1),
      v.platform,
      v.posted_at.slice(0, 10),
      v.view_count.toLocaleString(),
      v.like_count.toLocaleString(),
      v.hook_text || chalk.dim(`(${v.hook_source})`),
    ]);
  });
  console.log("");
  console.log(chalk.bold(`${handle} — top ${rows.length}`));
  console.log(table.toString());
}
