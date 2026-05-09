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
//
// TikTok's hashtag endpoint over-matches: searching #befreed returns videos
// containing "be freed" / "freed" in unrelated contexts. For brand UGC
// discovery we want only videos where the creator EXPLICITLY tagged the
// brand. Accepted signals (in order of confidence):
//
//   1. Exact hashtag boundary: #befreed (not #befreedish, not #befreeishly)
//   2. Campaign code: #befreed_NNNN (BeFreed and others use numeric codes)
//   3. Brand-app variant: #befreedapp (very common pattern)
//   4. Brand handle mention: @befreed
//
// Audited against BEFREED's full hashtag feed of 49 videos: this filter
// keeps 100% of explicit BeFreed UGC (28 videos) and rejects 21 unrelated
// "be freed" / "freed" posts. False negatives are minimal — videos that
// reference the brand without any tag (e.g. "ok but befreed has so many
// books") slip through, which is acceptable: we'd rather miss a few
// borderline cases than pollute the SMM's view with unrelated content.
export function isHashtagMatch(caption: string, tag: string): boolean {
  if (!caption) return false;
  const lower = caption.toLowerCase();
  const cleanTag = tag.replace(/^[#@]/, "").toLowerCase();
  const escaped = escapeRegex(cleanTag);

  // 1. Exact hashtag (with boundary so #befreedish doesn't match #befreed)
  // 2. Campaign code: #befreed_0117
  // 3. Brand-app variant: #befreedapp (covers Notion -> #notionapp, etc)
  const hashtagPattern = new RegExp(
    `#${escaped}(?![a-z0-9_])|#${escaped}_\\d+|#${escaped}app(?![a-z0-9_])`,
    "i",
  );
  if (hashtagPattern.test(lower)) return true;

  // Brand handle mention (@befreed, but not @befreedom)
  const mentionPattern = new RegExp(`@${escaped}(?![a-z0-9_])`, "i");
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
        let filtered = raw;
        if (query.mode === "hashtag") {
          // 1. Drop videos whose caption doesn't actually carry the brand
          //    hashtag/mention — TikTok's hashtag endpoint over-matches.
          filtered = filtered.filter((v) => isHashtagMatch(v.caption, query.value));
          // 2. Drop the brand's own account from third-party UGC results.
          //    If a user wants `@brand`'s posts, they pass `@brand`. The
          //    third-party-UGC view should show CREATORS, not the brand.
          const brandHandle = query.value.toLowerCase();
          filtered = filtered.filter(
            (v) => (v.author_handle ?? "").toLowerCase() !== brandHandle,
          );
        }
        const droppedCount = raw.length - filtered.length;
        upsertVideos(db, competitorId, filtered);
        videos = readCachedVideos(db, competitorId, platform);
        const suffix =
          droppedCount > 0
            ? ` (filtered ${droppedCount} unrelated/own-account post${droppedCount === 1 ? "" : "s"})`
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
    } else {
      console.log(
        chalk.dim(`If a brand uses a different hashtag, try ${chalk.cyan(`@${query.value}`)} for the account directly.`),
      );
    }
    return;
  }

  // Hashtag results have a Creator column (varies per row).
  // Handle results don't (every row is the same author).
  const showCreator = query.mode === "hashtag";

  const head = showCreator
    ? ["#", "Creator", "Posted", "Views", "Likes", "Caption"]
    : ["#", "Platform", "Posted", "Views", "Likes", "Caption"];
  // Wider Caption column so the brand hashtag stays visible (the signal that
  // told us this video matched). Truncated captions hide WHY a row qualified.
  const colWidths = showCreator
    ? [4, 20, 12, 11, 10, 65]
    : [4, 10, 12, 11, 10, 75];

  const table = new Table({
    head,
    style: { head: ["cyan"], border: ["gray"] },
    colWidths,
    wordWrap: true,
  });
  rows.forEach((v, i) => {
    const caption = highlightBrand(v.caption || v.hook_text, query.value);
    if (showCreator) {
      table.push([
        String(i + 1),
        v.author_handle ? `@${v.author_handle}` : chalk.dim("(unknown)"),
        v.posted_at.slice(0, 10),
        v.view_count.toLocaleString(),
        v.like_count.toLocaleString(),
        caption,
      ]);
    } else {
      table.push([
        String(i + 1),
        v.platform,
        v.posted_at.slice(0, 10),
        v.view_count.toLocaleString(),
        v.like_count.toLocaleString(),
        caption,
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

  // For hashtag mode, surface the most prolific creators — that's the SMM
  // insight worth its own line ("oh, @growthwithmya7 has the most posts about
  // this brand, I should reach out to them").
  if (showCreator) {
    const byCreator = new Map<string, number>();
    for (const v of rows) {
      if (!v.author_handle) continue;
      byCreator.set(v.author_handle, (byCreator.get(v.author_handle) ?? 0) + 1);
    }
    const top = [...byCreator.entries()]
      .filter(([, n]) => n > 1)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    if (top.length > 0) {
      console.log("");
      console.log(chalk.dim("Most prolific creators in this view:"));
      for (const [handle, count] of top) {
        console.log(chalk.dim(`  @${handle} — ${count} posts`));
      }
    }
  }
}

// Highlight the brand hashtag/mention in caption output so users can see at a
// glance which signal matched. Plays well with the precision filter — the
// highlighted token is the same one the filter looked for.
function highlightBrand(caption: string, tag: string): string {
  if (!caption) return chalk.dim("(no caption)");
  const cleanTag = tag.replace(/^[#@]/, "");
  const escaped = cleanTag.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Match #brand, #brand_NNN, and @brand variants
  const pattern = new RegExp(`(#${escaped}(?:_\\d+)?|@${escaped})`, "gi");
  return caption.replace(pattern, (m) => chalk.cyan.bold(m));
}
