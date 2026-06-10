import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { reconcileVideosWindow, upsertVideos } from "../db/videos.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import type { DataProvider } from "../providers/index.ts";
import type { Platform, RawVideo, VideoRecord } from "../types.ts";

export type SearchSort = "views" | "recency";
// Search modes (issue: competitor-UGC coverage gap):
//   user     — one account's catalog (the competitor's own posts). No brand-tag filter.
//   hashtag  — third-party UGC that EXPLICITLY tags a brand. Brand-tag filter ON.
//   keyword  — niche/topic discovery: any UGC matching a keyword, brand-tag NOT required.
//              This is the capability the brand-hashtag model structurally cannot reach
//              (the exact-tag caption filter dropped it; TikTokApi v7 has no video search).
//              Served by the tikwm keyword provider; bypasses isHashtagMatch entirely.
export type SearchMode = "user" | "hashtag" | "keyword";

export interface SearchOptions {
  limit: number;
  sort: SearchSort;
  platform?: Platform | "all";
  json: boolean;
  refresh: boolean;
  prune: boolean; // with --refresh: treat the fetch as complete and drop in-window rows it didn't return
  days: number;
  mode?: SearchMode; // explicit override; otherwise auto-detected from query
}

// Raw commander option object → typed SearchOptions. Exported (and pure) so
// the two back-compat contracts here stay under test: the legacy "engagement"
// sort alias (existing scripts pass it; it must keep mapping to "views") and
// the --mode whitelist (anything else falls back to auto-detection).
export function normalizeSearchOptions(raw: {
  limit: number;
  sort: string;
  platform?: string;
  json?: unknown;
  refresh?: unknown;
  prune?: unknown;
  days: number;
  mode?: string;
}): SearchOptions {
  const sort: SearchSort = raw.sort === "engagement" ? "views" : (raw.sort as SearchSort);
  const mode: SearchMode | undefined =
    raw.mode === "user" || raw.mode === "hashtag" || raw.mode === "keyword"
      ? raw.mode
      : undefined;
  return {
    limit: raw.limit,
    sort,
    platform: raw.platform as Platform | "all" | undefined,
    json: Boolean(raw.json),
    refresh: Boolean(raw.refresh),
    prune: Boolean(raw.prune),
    days: raw.days,
    mode,
  };
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
// Audited against BeFreed: keeps explicit-tag UGC AND plain-text brand mentions,
// while rejecting unrelated "be free"/"#freedom"/"#befree" posts. The accepted
// signals (any one):
//   1. Exact hashtag boundary: #befreed (not #befreedish)
//   2. Campaign code: #befreed_0117
//   3. Brand-app variant: #befreedapp
//   4. Handle mention: @befreed (not @befreedom)
//   5. Plain-text brand token: the literal brand name as a standalone word
//      (e.g. "reading with befreed is so clutch")
//
// Signal #5 is the fix for the dropped-top-performers bug: the highest-reach
// genuine BeFreed UGC (776K views, "Learning with befreed...") writes the brand
// as plain text, no # or @. Requiring a tag dropped exactly the videos a
// "rank by performance" product most needs. Verified on BeFreed's full 1,223-
// video raw feed: signal #5 recovers 16 genuine brand videos and re-admits
// ZERO junk — "#freedom", "#befree" (a Russian clothing brand), "be free",
// horse-breeding, etc. don't contain the literal token "befreed", so the word-
// boundary match excludes them. (The token IS the brand name, so this is
// brand-specific precision, not a generic loosening.)
export function isHashtagMatch(caption: string, tag: string): boolean {
  if (!caption) return false;
  const lower = caption.toLowerCase();
  const cleanTag = tag.replace(/^[#@]/, "").toLowerCase();
  const escaped = escapeRegex(cleanTag);

  // 1-3. Hashtag forms: exact, campaign code, brand-app variant.
  const hashtagPattern = new RegExp(
    `#${escaped}(?![a-z0-9_])|#${escaped}_\\d+|#${escaped}app(?![a-z0-9_])`,
    "i",
  );
  if (hashtagPattern.test(lower)) return true;

  // 4. Brand handle mention (@befreed, but not @befreedom).
  const mentionPattern = new RegExp(`@${escaped}(?![a-z0-9_])`, "i");
  if (mentionPattern.test(lower)) return true;

  // 5. Plain-text brand token — the brand name as a standalone word, no # or @.
  // Word boundaries on both sides so "befreedom" / "unbefreed" don't match and
  // "#befreed"/"@befreed" (already caught above) don't double-count. Safe
  // because the token equals the brand name; generic words like "free" are NOT
  // the tag, so junk ("#freedom", "be free") still fails.
  const plainPattern = new RegExp(`(?<![a-z0-9_])${escaped}(?![a-z0-9_])`, "i");
  if (plainPattern.test(lower)) return true;

  return false;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Mode-aware precision filtering. The brand filter applies ONLY in hashtag
// mode: keyword captions by design carry no brand tag, and user mode is the
// brand's own catalog — re-applying isHashtagMatch in either would silently
// zero their results (the original capped-results bug class). Exported so the
// WIRING (not just isHashtagMatch itself) stays under test.
export function applyHashtagPrecision(
  videos: RawVideo[],
  mode: SearchMode,
  brand: string,
): RawVideo[] {
  if (mode !== "hashtag") return videos;
  // 1. Drop videos whose caption doesn't actually carry the brand
  //    hashtag/mention — TikTok's hashtag endpoint over-matches.
  // 2. Drop the brand's own account from third-party UGC results. If a user
  //    wants `@brand`'s posts, they pass `@brand`. The third-party-UGC view
  //    should show CREATORS, not the brand.
  const brandHandle = brand.replace(/^[#@]/, "").toLowerCase();
  return videos
    .filter((v) => isHashtagMatch(v.caption, brand))
    .filter((v) => (v.author_handle ?? "").toLowerCase() !== brandHandle);
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
  if (override === "keyword") {
    // Keyword/niche discovery: the query is a free-text topic phrase, not a
    // handle or tag. Keep the raw phrase (spaces and all) as the provider
    // value. The competitors-table key is prefixed `kw:` so a keyword search
    // doesn't collide with a same-named #hashtag or @handle row.
    const phrase = trimmed.replace(/^#/, "");
    return { mode: "keyword", key: `kw:${phrase}`, value: phrase };
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
  const failedPlatforms: Platform[] = [];

  for (const platform of platforms) {
    const competitorId = upsertCompetitor(db, query.key, platform);
    const cached = readCachedVideos(db, competitorId, platform, opts.days);
    let videos = cached;

    if (opts.refresh || cached.length === 0) {
      const spinner = opts.json
        ? null
        : ora(
            `Fetching ${chalk.cyan(query.key)} on ${platform} (${query.mode} mode)...`,
          ).start();
      try {
        const raw = await fetchByMode(provider, query, platform, opts.days);
        const filtered = applyHashtagPrecision(raw, query.mode, query.value);
        const droppedCount = raw.length - filtered.length;
        upsertVideos(db, competitorId, filtered);
        if (opts.refresh && opts.prune && filtered.length > 0) {
          // Pruning is OPT-IN (--prune): providers return best-effort partial
          // results (keyword mode succeeds on partial pages; the hashtag walk
          // skips failed creators), so treating every refresh as the complete
          // source of truth would delete valid cached rows on a flaky run —
          // and alerts cascade with their videos. Only the user can declare
          // "this fetch is the truth"; with --prune we drop in-window rows
          // the provider didn't return (deleted/private/stale videos).
          reconcileVideosWindow(
            db,
            competitorId,
            platform,
            opts.days,
            filtered.map((v) => v.external_id),
          );
        }
        videos = readCachedVideos(db, competitorId, platform, opts.days);
        const suffix =
          droppedCount > 0
            ? ` (filtered ${droppedCount} unrelated/own-account post${droppedCount === 1 ? "" : "s"})`
            : "";
        spinner?.succeed(`${platform}: ${chalk.cyan(videos.length)} videos${suffix}`);
      } catch (err) {
        failedPlatforms.push(platform);
        spinner?.fail(`${platform}: ${(err as Error).message}`);
        if (opts.json) {
          // --json consumers parse stdout; surface provider failures as a
          // structured line on STDERR so a partial result isn't silently
          // mistaken for "no videos exist".
          console.error(
            JSON.stringify({
              warning: "provider_failure",
              platform,
              provider: provider.name,
              message: (err as Error).message,
            }),
          );
        }
        continue;
      }
    }
    allVideos.push(...videos);
  }

  // Partial success (one platform from cache or fetch) stays exit 0; only a
  // total wipeout — every requested platform failed to fetch — is an error.
  if (failedPlatforms.length === platforms.length) {
    process.exitCode = 1;
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

// Exported for tests: keyword mode must fail with an actionable error on
// providers that don't implement fetchKeywordVideos (back-compat contract).
export async function fetchByMode(
  provider: DataProvider,
  query: Pick<ParsedQuery, "mode" | "value">,
  platform: Platform,
  days: number,
): Promise<RawVideo[]> {
  if (query.mode === "user") {
    return provider.fetchRecentVideos(query.value, platform, days);
  }
  if (query.mode === "keyword") {
    if (!provider.fetchKeywordVideos) {
      throw new Error(
        `Provider '${provider.name}' does not support keyword/niche search. ` +
          `Keyword discovery needs the 'tiktok-oss' provider (free, via the tikwm relay).`,
      );
    }
    return provider.fetchKeywordVideos(query.value, platform, days);
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

export function readCachedVideos(
  db: ReturnType<typeof openDb>,
  competitorId: number,
  platform: Platform,
  windowDays?: number,
): VideoRecord[] {
  // Honor the trailing-window flag on CACHED reads too. The DB accumulates
  // every video ever fetched for a competitor (a prior `--days 365` run leaves
  // year-old rows behind), so without this filter a later `--days 30` query
  // would still surface those stale older rows — e.g. a 31-day-old clip showing
  // up in a "last 30 days" view. The fetch path already applies the same cutoff;
  // this keeps the cached path consistent with it.
  if (windowDays && windowDays > 0) {
    const cutoff = new Date(Date.now() - windowDays * 86_400_000).toISOString();
    // datetime() on both sides: new rows are normalized to UTC Z at upsert,
    // but legacy rows may carry "+00:00" offsets — a raw TEXT >= compare
    // misorders those against a Z-suffixed cutoff.
    return db
      .prepare(
        `SELECT * FROM videos WHERE competitor_id = ? AND platform = ? AND datetime(posted_at) >= datetime(?)
         ORDER BY datetime(posted_at) DESC`,
      )
      .all(competitorId, platform, cutoff) as VideoRecord[];
  }
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
