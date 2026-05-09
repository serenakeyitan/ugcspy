import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import type { DataProvider } from "../providers/index.ts";
import type { Platform, RawVideo, VideoRecord } from "../types.ts";

export type SearchSort = "views" | "recency";
export type SearchMode = "user" | "hashtag";

export interface SearchOptions {
  limit: number;
  sort: SearchSort;
  platform?: Platform | "all";
  json: boolean;
  refresh: boolean;
  days: number;
  mode?: SearchMode; // explicit override; otherwise auto-detected from query
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

// Precision filter for hashtag results.
// TikTok's hashtag endpoint sometimes returns videos that don't actually carry
// the hashtag in the caption (it matches on related signals like sounds, OCR,
// or shadow tags). For brand UGC discovery, we only want videos where the
// creator EXPLICITLY tagged the brand. Required signal:
//   1) the literal hashtag we searched for (case-insensitive), OR
//   2) a campaign-code variant like #brand_NNNN (BeFreed uses these), OR
//   3) the brand handle mentioned (e.g. @befreed)
//
// Without this, "befreed" search collides with "be freed" / "freed" usage on
// completely unrelated videos. Verified empirically against BEFREED:
// removes 4 false positives ("speaking my truth", "Time to be free", etc.)
// while keeping all 6 real BeFreed UGC posts.
export function isHashtagMatch(caption: string, tag: string): boolean {
  if (!caption) return false;
  const lower = caption.toLowerCase();
  const cleanTag = tag.replace(/^[#@]/, "").toLowerCase();
  // Match the exact hashtag, or any campaign-code variant: #brand or #brand_NNN
  const hashtagPattern = new RegExp(`#${escapeRegex(cleanTag)}(?![a-z0-9])|#${escapeRegex(cleanTag)}_\\d+`, "i");
  if (hashtagPattern.test(lower)) return true;
  // Also accept a creator @-mentioning the brand handle (some sponsored posts
  // tag the brand instead of using the hashtag).
  const mentionPattern = new RegExp(`@${escapeRegex(cleanTag)}(?![a-z0-9])`, "i");
  if (mentionPattern.test(lower)) return true;
  return false;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface ParsedQuery {
  mode: SearchMode;
  // Canonical key used for the competitors table:
  //   user mode    -> "@handle"
  //   hashtag mode -> "#tag"
  key: string;
  // The raw value to pass to the provider (no prefix).
  value: string;
}

// Auto-detect: @x → user, #x → hashtag, plain x → hashtag (the wedge for
// finding third-party UGC). Explicit override via opts.mode wins.
export function parseQuery(raw: string, override?: SearchMode): ParsedQuery {
  const trimmed = raw.trim();
  if (override === "user") {
    const handle = trimmed.replace(/^[@#]/, "");
    return { mode: "user", key: `@${handle}`, value: handle };
  }
  if (override === "hashtag") {
    const tag = trimmed.replace(/^[@#]/, "");
    return { mode: "hashtag", key: `#${tag}`, value: tag };
  }
  if (trimmed.startsWith("@")) {
    const handle = trimmed.slice(1);
    return { mode: "user", key: `@${handle}`, value: handle };
  }
  if (trimmed.startsWith("#")) {
    const tag = trimmed.slice(1);
    return { mode: "hashtag", key: `#${tag}`, value: tag };
  }
  // Plain word: default to hashtag — that's the BigSpy-for-UGC use case
  // (find creators promoting a brand). Use --mode user or "@handle" to
  // search a specific account.
  return { mode: "hashtag", key: `#${trimmed}`, value: trimmed };
}

export async function runSearch(queryRaw: string, opts: SearchOptions): Promise<void> {
  const query = parseQuery(queryRaw, opts.mode);
  const config = loadConfig();
  const db = openDb();

  const platforms: Platform[] =
    !opts.platform || opts.platform === "all" ? ["tiktok", "instagram"] : [opts.platform];
  const provider = getProvider(config);

  const allVideos: VideoRecord[] = [];

  for (const platform of platforms) {
    const competitorId = upsertCompetitor(db, query.key, platform);
    const cached = readCachedVideos(db, competitorId, platform);
    let videos = cached;

    if (opts.refresh || cached.length === 0) {
      const spinner = opts.json
        ? null
        : ora(
            `Fetching ${chalk.cyan(query.key)} on ${platform} (${query.mode} mode)...`,
          ).start();
      try {
        const raw = await fetchByMode(provider, query, platform, opts.days);
        // Hashtag mode returns over-broad results from TikTok. Filter to keep
        // only videos whose caption actually carries the brand hashtag/mention.
        const filtered =
          query.mode === "hashtag"
            ? raw.filter((v) => isHashtagMatch(v.caption, query.value))
            : raw;
        const droppedCount = raw.length - filtered.length;
        upsertVideos(db, competitorId, filtered);
        videos = readCachedVideos(db, competitorId, platform);
        const suffix =
          droppedCount > 0
            ? ` (filtered ${droppedCount} unrelated post${droppedCount === 1 ? "" : "s"})`
            : "";
        spinner?.succeed(`${platform}: ${chalk.cyan(videos.length)} videos${suffix}`);
      } catch (err) {
        spinner?.fail(`${platform}: ${(err as Error).message}`);
        continue;
      }
    }
    allVideos.push(...videos);
  }

  let rows = allVideos;
  // BigSpy-style ranking: highest reach first by default.
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
  printTable(query, rows);
}

async function fetchByMode(
  provider: DataProvider,
  query: ParsedQuery,
  platform: Platform,
  days: number,
): Promise<RawVideo[]> {
  if (query.mode === "user") {
    return provider.fetchRecentVideos(query.value, platform, days);
  }
  if (!provider.fetchHashtagVideos) {
    throw new Error(
      `Provider '${provider.name}' does not support hashtag search. Use a handle search like @${query.value} instead.`,
    );
  }
  return provider.fetchHashtagVideos(query.value, platform, days);
}

function upsertCompetitor(
  db: ReturnType<typeof openDb>,
  key: string,
  platform: Platform,
): number {
  db.prepare(
    `INSERT OR IGNORE INTO competitors (handle, platform) VALUES (?, ?)`,
  ).run(key, platform);
  const row = db
    .prepare(`SELECT id FROM competitors WHERE handle = ? AND platform = ?`)
    .get(key, platform) as { id: number } | undefined;
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
      hook_source, hook_text, hook_confidence, format_tag, author_handle, raw_metrics_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(platform, external_id) DO UPDATE SET
      view_count = excluded.view_count,
      like_count = excluded.like_count,
      comment_count = excluded.comment_count,
      share_count = excluded.share_count,
      fetched_at = datetime('now'),
      hook_text = excluded.hook_text,
      hook_source = excluded.hook_source,
      author_handle = excluded.author_handle
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
        v.author_handle ?? null,
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

function printTable(query: ParsedQuery, rows: VideoRecord[]): void {
  if (rows.length === 0) {
    console.log(chalk.yellow(`\nNo videos found for ${query.key}.`));
    if (query.mode === "user") {
      console.log(
        chalk.dim(`Try ${chalk.cyan(query.value)} (no @) to search the hashtag for third-party UGC.`),
      );
    }
    return;
  }

  // Hashtag results have a Creator column (varies per row).
  // Handle results don't (every row is the same author).
  const showCreator = query.mode === "hashtag";

  const head = showCreator
    ? ["#", "Creator", "Posted", "Views", "Likes", "Hook"]
    : ["#", "Platform", "Posted", "Views", "Likes", "Hook"];
  const colWidths = showCreator
    ? [4, 20, 12, 12, 10, 55]
    : [4, 10, 12, 12, 10, 65];

  const table = new Table({
    head,
    style: { head: ["cyan"], border: ["gray"] },
    colWidths,
    wordWrap: true,
  });
  rows.forEach((v, i) => {
    if (showCreator) {
      table.push([
        String(i + 1),
        v.author_handle ? `@${v.author_handle}` : chalk.dim("(unknown)"),
        v.posted_at.slice(0, 10),
        v.view_count.toLocaleString(),
        v.like_count.toLocaleString(),
        v.hook_text || chalk.dim(`(${v.hook_source})`),
      ]);
    } else {
      table.push([
        String(i + 1),
        v.platform,
        v.posted_at.slice(0, 10),
        v.view_count.toLocaleString(),
        v.like_count.toLocaleString(),
        v.hook_text || chalk.dim(`(${v.hook_source})`),
      ]);
    }
  });
  console.log("");
  console.log(
    chalk.bold(
      `${query.key} — top ${rows.length} ${query.mode === "hashtag" ? "(third-party UGC)" : "(account posts)"}`,
    ),
  );
  console.log(table.toString());
}
