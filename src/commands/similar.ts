import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import { ProviderError } from "../providers/types.ts";
import type { SimilarResult } from "../types.ts";

// Normalize a seed to a bare lowercase handle. Accepts a plain handle (`@x`,
// `x`) OR a TikTok URL (`https://tiktok.com/@x`, `.../@x/video/123`) — the skill
// advertises "drop handles or video links", so a pasted link must resolve to
// the creator, not become an invalid username that silently zeros the graph.
export function seedToHandle(raw: string): string {
  const s = raw.trim();
  const urlMatch = s.match(/tiktok\.com\/@([^/?#]+)/i);
  if (urlMatch) return urlMatch[1]!.toLowerCase();
  return s.replace(/^@/, "").toLowerCase();
}

// `ugcspy similar @a @b @c` — the creator-centric "find more like these" pass.
// Walks who the seed creators FOLLOW (depth-1 follow graph) and ranks the
// result by how many seeds follow each candidate. This is the scout's Path A
// primary engine: a brand hands over creators it already likes, and we surface
// the cluster around them.
//
// IMPORTANT CAVEAT (documented, not a bug): tikwm's /user/following is private
// or blocked for a large fraction of creators (~60% in measurement), so the
// follow graph is often THIN or EMPTY. That's a valid result, not a failure —
// the scout skill pairs this with corpus style-matching and reports the graph
// hit-rate. The command therefore never errors on an empty graph; it says so.

export interface SimilarOptions {
  json?: boolean;
}

// Enrich a follow-graph result with the candidate's best KNOWN view count from
// the local cache (free — no extra pull). Many candidates won't be cached yet
// (view 0); the scout verifies real reach with a roster pull. This is just a
// cheap "have we seen this creator do numbers?" hint for ranking/eyeballing.
function cachedMaxViews(handles: string[]): Map<string, number> {
  const out = new Map<string, number>();
  if (handles.length === 0) return out;
  const db = openDb();
  const placeholders = handles.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT author_handle AS handle, MAX(view_count) AS mx
         FROM videos WHERE author_handle IN (${placeholders})
         GROUP BY author_handle`,
    )
    .all(...handles) as Array<{ handle: string; mx: number }>;
  for (const r of rows) out.set(r.handle, r.mx ?? 0);
  return out;
}

// Summarize per-seed readability into a one-line hit-rate the human/skill can
// trust. -1 = blocked/unreadable, -2 = unresolved handle, >=0 = follow-count.
function hitRate(result: SimilarResult): { readable: number; total: number; line: string } {
  const sr = result.seedResults;
  const total = sr.length;
  const readable = sr.filter((s) => s.status >= 0).length;
  const blocked = sr.filter((s) => s.status === -1).length;
  const unresolved = sr.filter((s) => s.status === -2).length;
  const parts: string[] = [`${readable}/${total} seeds had readable following lists`];
  if (blocked) parts.push(`${blocked} blocked`);
  if (unresolved) parts.push(`${unresolved} unresolved`);
  return { readable, total, line: parts.join(" · ") };
}

export async function runSimilar(seedsRaw: string[], opts: SimilarOptions): Promise<void> {
  // Normalize seeds (handle OR pasted URL → bare handle), dedupe, for display.
  // The bridge re-normalizes handles authoritatively but can't recover a URL,
  // so URL→handle MUST happen here.
  const seeds = [...new Set(seedsRaw.map(seedToHandle).filter(Boolean))];
  if (seeds.length === 0) {
    throw new Error("similar: give at least one seed creator, e.g. `ugcspy similar @a @b @c`");
  }

  const config = loadConfig();
  const provider = getProvider(config);
  if (!provider.fetchSimilarCreators) {
    throw new ProviderError(
      `provider '${provider.name}' has no follow-graph similarity source`,
      provider.name,
    );
  }

  const spinner = opts.json
    ? null
    : ora(`Walking the follow graph of ${seeds.length} seed creator(s)...`).start();
  let result: SimilarResult;
  try {
    result = await provider.fetchSimilarCreators(seeds);
  } catch (e) {
    spinner?.fail("Follow-graph walk failed");
    throw e;
  }

  const views = cachedMaxViews(result.creators.map((r) => r.handle));
  const enriched = result.creators.map((r) => ({
    ...r,
    cachedMaxViews: views.get(r.handle) ?? 0,
  }));
  const hr = hitRate(result);

  if (opts.json) {
    // Machine-readable: the skill reads creators[] AND seedResults[] (hit-rate).
    console.log(
      JSON.stringify(
        { seeds, count: enriched.length, creators: enriched, seedResults: result.seedResults },
        null,
        2,
      ),
    );
    return;
  }

  spinner?.stop();
  console.log(chalk.dim(`Follow-graph readability: ${hr.line}.`));
  if (enriched.length === 0) {
    console.log(
      chalk.yellow(
        hr.readable === 0
          ? "Every seed's following list was blocked/unreadable — the graph saw nothing. " +
              "This is common (most lists are private). Fall back to corpus/keyword discovery."
          : "No shared followings across the readable seeds. The graph is a thin net here — " +
              "fall back to corpus/keyword discovery to find the style.",
      ),
    );
    return;
  }

  const table = new Table({
    head: ["#", "creator", "seeds following", "cached max views"].map((h) => chalk.dim(h)),
    style: { head: [], border: [] },
  });
  enriched.forEach((r, i) => {
    table.push([
      String(i + 1),
      `@${r.handle}`,
      String(r.seedsFollowing),
      r.cachedMaxViews ? r.cachedMaxViews.toLocaleString() : chalk.dim("—"),
    ]);
  });
  console.log(table.toString());
  console.log(
    chalk.dim(
      `${enriched.length} creator(s) surfaced. "seeds following" is a weak signal (the graph is a ` +
        `net, not a ranker) — verify real fit + reach with \`ugcspy search @<handle>\`.`,
    ),
  );
}
