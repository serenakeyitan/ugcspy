import chalk from "chalk";
import Table from "cli-table3";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { upsertVideos } from "../db/videos.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import type { RawVideo } from "../types.ts";

// Template/account discovery when the source accounts are UNKNOWN.
// Three lanes share this surface:
//   1. trend-riding  — `ugcspy trending` pulls the region's viral feed
//   2. cross-category — mineBrandCandidates() over ANY corpus finds brands
//      running UGC programs (the #brand_NNNN campaign-code fingerprint)
//   3. direct competitors — `ugcspy discover "<your niche>"` mines the
//      niche keyword corpus for competitor brand tags + recurring creators
// Code delivers candidates and evidence; judging fit for the user's brand is
// the /ugcspy-scout skill's job.

export interface BrandCandidate {
  tag: string;
  // Structural evidence — no dictionaries, no curated lists:
  campaignCodes: number; // #tag_NNNN sightings — the strongest UGC-program signal
  appVariant: boolean; // #tagapp seen
  authorMatch: boolean; // an account handle in the corpus contains the tag — brand-shaped
  background: boolean; // tag also lives in the cached network-wide trending corpus — generic by construction
  videos: number; // videos carrying the tag family
  authors: number; // distinct creators carrying it (organic spread)
  maxViews: number;
  score: number;
}

export interface CreatorCandidate {
  handle: string;
  videos: number;
  maxViews: number;
  totalViews: number;
}

const TAG_RE = /#([a-z0-9_]{2,40})/gi;

// Mine a corpus for brand-shaped hashtags. Purely structural:
//   - campaign codes (#x_0124) are how brands run trackable UGC programs —
//     one sighting is near-proof of a brand, and of an ACTIVE program
//   - app variants (#xapp) mark product brands
//   - recurrence across DISTINCT authors separates brand tags from one
//     creator's personal tagline; raw frequency alone would just surface
//     generic topic tags, so breadth beats depth in the score
// Generic topic tags (#fyp, #viral, #funny...) are NOT filtered by a word
// list — they score low naturally because they almost never carry campaign
// codes or app variants, and the score weights those signals heavily.
export function mineBrandCandidates(
  videos: RawVideo[],
  opts?: { minAuthors?: number; backgroundTags?: Set<string> },
): BrandCandidate[] {
  const minAuthors = opts?.minAuthors ?? 2;
  const background = opts?.backgroundTags ?? new Set<string>();
  // Brand-shaped tags often match an account in the same corpus
  // (#pingoai ↔ @pingoai.korean). Structural, not curated.
  const handleBlob = videos.map((v) => (v.author_handle ?? "").toLowerCase()).join("|");
  const stats = new Map<
    string,
    { codes: number; app: boolean; vids: Set<string>; authors: Set<string>; maxViews: number; plain: number }
  >();
  const bump = (base: string) => {
    let s = stats.get(base);
    if (!s) {
      s = { codes: 0, app: false, vids: new Set(), authors: new Set(), maxViews: 0, plain: 0 };
      stats.set(base, s);
    }
    return s;
  };
  for (const v of videos) {
    const caption = (v.caption ?? "").toLowerCase();
    for (const m of caption.matchAll(TAG_RE)) {
      const tag = m[1]!;
      const code = tag.match(/^([a-z0-9]+?)_(\d{2,6})$/);
      let base = tag;
      let isCode = false;
      if (code) {
        base = code[1]!;
        isCode = true;
      }
      let isApp = false;
      if (base.endsWith("app") && base.length > 5) {
        // #xapp counts as evidence for x, but keep #xapp itself out of the map
        base = base.slice(0, -3);
        isApp = true;
      }
      const s = bump(base);
      if (isCode) s.codes += 1;
      if (isApp) s.app = true;
      if (!isCode && !isApp) s.plain += 1;
      s.vids.add(v.external_id);
      if (v.author_handle) s.authors.add(v.author_handle.toLowerCase());
      if (v.view_count > s.maxViews) s.maxViews = v.view_count;
    }
  }
  const out: BrandCandidate[] = [];
  for (const [tag, s] of stats) {
    const authorMatch = tag.length >= 4 && handleBlob.includes(tag);
    const isBackground = background.has(tag);
    const hasBrandSignal = s.codes > 0 || s.app || authorMatch;
    if (!hasBrandSignal && s.authors.size < minAuthors) continue;
    // Brand signals dominate (campaign codes > app variant > author match);
    // author breadth beats raw frequency; views are a sqrt tiebreak so one
    // mega-video can't swamp structure. Tags that also live in the
    // network-wide trending corpus are generic by construction — crush them
    // unless they carry a hard brand signal.
    let score =
      s.codes * 25 +
      (s.app ? 15 : 0) +
      (authorMatch ? 12 : 0) +
      s.authors.size * 4 +
      s.vids.size +
      Math.sqrt(s.maxViews) / 100;
    if (isBackground && s.codes === 0) score *= 0.2;
    out.push({
      tag,
      campaignCodes: s.codes,
      appVariant: s.app,
      authorMatch,
      background: isBackground,
      videos: s.vids.size,
      authors: s.authors.size,
      maxViews: s.maxViews,
      score: Math.round(score * 10) / 10,
    });
  }
  return out.sort((a, b) => b.score - a.score);
}

// Tag set of a cached corpus (e.g. trend:US) — the background-frequency
// reference for genericity. Pure read; empty set when nothing is cached.
export function cachedCorpusTags(db: ReturnType<typeof openDb>, handlePrefix: string): Set<string> {
  const rows = db
    .prepare(
      `SELECT v.caption FROM videos v JOIN competitors c ON c.id = v.competitor_id
       WHERE c.handle LIKE ? || '%'`,
    )
    .all(handlePrefix) as Array<{ caption: string }>;
  const tags = new Set<string>();
  for (const r of rows) {
    for (const m of (r.caption ?? "").toLowerCase().matchAll(TAG_RE)) tags.add(m[1]!);
  }
  return tags;
}

// Recurring creators in the corpus — the "accounts worth copying" half of
// discovery. Multiple corpus appearances = the niche keeps surfacing them.
export function topCreators(videos: RawVideo[], minVideos = 2): CreatorCandidate[] {
  const byAuthor = new Map<string, { videos: number; maxViews: number; totalViews: number }>();
  for (const v of videos) {
    const h = (v.author_handle ?? "").toLowerCase();
    if (!h) continue;
    const s = byAuthor.get(h) ?? { videos: 0, maxViews: 0, totalViews: 0 };
    s.videos += 1;
    s.totalViews += v.view_count;
    if (v.view_count > s.maxViews) s.maxViews = v.view_count;
    byAuthor.set(h, s);
  }
  return [...byAuthor.entries()]
    .filter(([, s]) => s.videos >= minVideos)
    .map(([handle, s]) => ({ handle, ...s }))
    .sort((a, b) => b.maxViews - a.maxViews);
}

export interface DiscoverOptions {
  days: number;
  limit: number;
  json: boolean;
  // "keyword" corpus (a niche phrase) or "trending" corpus (a region code).
  source: "keyword" | "trending";
}

export async function runDiscover(query: string, opts: DiscoverOptions): Promise<void> {
  const db = openDb();
  const config = loadConfig();
  const provider = getProvider(config);

  const spinner = opts.json ? null : ora();
  let corpus: RawVideo[];
  let cacheKey: string;
  if (opts.source === "trending") {
    if (!provider.fetchTrendingVideos) {
      console.error(chalk.red(`Provider '${provider.name}' has no trending support.`));
      process.exit(1);
    }
    const region = (query || "US").toUpperCase();
    cacheKey = `trend:${region}`;
    spinner?.start(`Pulling the ${region} trending feed...`);
    corpus = await provider.fetchTrendingVideos(region, opts.days);
  } else {
    if (!provider.fetchKeywordVideos) {
      console.error(chalk.red(`Provider '${provider.name}' has no keyword support.`));
      process.exit(1);
    }
    cacheKey = `kw:${query}`;
    spinner?.start(`Scanning the "${query}" niche...`);
    corpus = await provider.fetchKeywordVideos(query, "tiktok", opts.days);
  }
  spinner?.succeed(`${corpus.length} videos in corpus`);

  // Cache the corpus under its synthetic competitor key so the downstream
  // chain (transcript → rebrand) can target these videos directly.
  if (corpus.length > 0) {
    const competitorId = upsertCompetitorKey(db, cacheKey);
    upsertVideos(db, competitorId, corpus);
  }

  // Background reference: the network-wide trending corpus, when cached
  // (run `ugcspy trending` first for sharper genericity discounting). Never
  // discount the corpus we're actually mining.
  const backgroundTags =
    opts.source === "trending" ? new Set<string>() : cachedCorpusTags(db, "trend:");
  const brands = mineBrandCandidates(corpus, { backgroundTags }).slice(0, opts.limit);
  const creators = topCreators(corpus).slice(0, opts.limit);

  if (opts.json) {
    console.log(JSON.stringify({ corpus_size: corpus.length, cache_key: cacheKey, brands, creators }, null, 2));
    return;
  }

  if (brands.length > 0) {
    console.log(chalk.bold(`\nBrand candidates (UGC-program signals) — ${cacheKey}`));
    const t = new Table({ head: ["#", "Tag", "Codes", "Signals", "Videos", "Creators", "Top views", "Score"] });
    brands.forEach((b, i) =>
      t.push([
        i + 1,
        `#${b.tag}`,
        b.campaignCodes || "",
        [b.appVariant ? "app" : "", b.authorMatch ? "acct" : "", b.background ? "bg" : ""]
          .filter(Boolean)
          .join(","),
        b.videos,
        b.authors,
        b.maxViews.toLocaleString(),
        b.score,
      ]),
    );
    console.log(t.toString());
    console.log(
      chalk.dim(
        "Codes = #tag_NNNN campaign-code sightings (the strongest run-a-UGC-program signal). Signals: app = #tagapp variant seen, acct = matches an account handle in-corpus, bg = also in the network-wide trending corpus (generic). Next: `ugcspy search <tag>` on a candidate.",
      ),
    );
  } else {
    console.log(chalk.yellow("\nNo brand-shaped tags surfaced in this corpus."));
  }

  if (creators.length > 0) {
    console.log(chalk.bold(`\nRecurring creators in this corpus`));
    const t = new Table({ head: ["#", "Creator", "Videos", "Top views", "Total views"] });
    creators.forEach((c, i) =>
      t.push([i + 1, `@${c.handle}`, c.videos, c.maxViews.toLocaleString(), c.totalViews.toLocaleString()]),
    );
    console.log(t.toString());
    console.log(
      chalk.dim(`Next: \`ugcspy transcript ${cacheKey} --talking --top 5\` for hooks, then /ugcspy-rebrand.`),
    );
  }
}

// Lane 1 surface: the region's viral hits themselves, ranked by views —
// the raw material for trend-riding. Cached under trend:<REGION> so the
// transcript → rebrand chain can target them like any competitor.
export async function runTrending(
  region: string,
  opts: { days: number; limit: number; json: boolean },
): Promise<void> {
  const db = openDb();
  const config = loadConfig();
  const provider = getProvider(config);
  if (!provider.fetchTrendingVideos) {
    console.error(chalk.red(`Provider '${provider.name}' has no trending support.`));
    process.exit(1);
  }
  const r = (region || "US").toUpperCase();
  const spinner = opts.json ? null : ora().start(`Pulling the ${r} trending feed...`);
  const videos = await provider.fetchTrendingVideos(r, opts.days);
  spinner?.succeed(`${videos.length} trending videos (${r})`);
  if (videos.length > 0) {
    const competitorId = upsertCompetitorKey(db, `trend:${r}`);
    upsertVideos(db, competitorId, videos);
  }
  const ranked = [...videos].sort((a, b) => b.view_count - a.view_count).slice(0, opts.limit);
  if (opts.json) {
    console.log(JSON.stringify(ranked, null, 2));
    return;
  }
  const t = new Table({ head: ["#", "Creator", "Views", "Caption"], colWidths: [4, 22, 14, 60], wordWrap: true });
  ranked.forEach((v, i) =>
    t.push([i + 1, `@${v.author_handle ?? "?"}`, v.view_count.toLocaleString(), (v.caption || "").slice(0, 120)]),
  );
  console.log(t.toString());
  console.log(
    chalk.dim(
      `Cached as trend:${r}. Next: \`ugcspy transcript trend:${r} --talking --top 5\` for remixable hooks, or \`ugcspy discover ${r} --trending\` to mine it for UGC-program brands.`,
    ),
  );
}

function upsertCompetitorKey(db: ReturnType<typeof openDb>, key: string): number {
  db.prepare(`INSERT OR IGNORE INTO competitors (handle, platform) VALUES (?, 'tiktok')`).run(key);
  const row = db.prepare(`SELECT id FROM competitors WHERE handle = ? AND platform = 'tiktok'`).get(key) as
    | { id: number }
    | undefined;
  if (!row) throw new Error("Failed to upsert discovery cache key");
  return row.id;
}
