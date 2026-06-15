import type { Platform, RawVideo } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";

// ScrapeCreators — paid (free-trial) IG API. The ONE thing the free gallery-dl
// path can't do: keyword/caption search, which catches creators who mention a
// brand WITHOUT a hashtag or @-tag (in caption/bio). Endpoints + fields verified
// against docs.scrapecreators.com:
//   keyword reels:  GET /v2/instagram/reels/search?query=&date_posted=  → {reels:[...]}
//   hashtag posts:  GET /v1/instagram/search/hashtag?hashtag=&media_type=reels&date_posted=  → {posts:[...]}
//   user reels:     GET /v1/instagram/user/reels?handle=  → {reels|items:[...]}
// Auth: x-api-key header. Each result carries shortcode/caption/like_count/
// video_view_count/video_play_count/owner.username/taken_at/video_url — clean
// reels with view counts, no carousel/is_video guesswork.
const BASE = "https://api.scrapecreators.com";

// Map ScrapeCreators' relative window param from a day count.
function datePosted(days: number): string {
  if (days <= 1) return "last-day";
  if (days <= 7) return "last-week";
  if (days <= 31) return "last-month";
  return "last-year";
}

export class ScrapeCreatorsProvider implements DataProvider {
  readonly name = "scrapecreators";
  constructor(private apiKey: string) {}

  private requireKey(): void {
    if (!this.apiKey) {
      throw new ProviderError(
        "ScrapeCreators API key missing. Set UGCSPY_SCRAPER_API_KEY, run `ugcspy init`, " +
          "or put the key in ~/.ugcspy/scrapecreators.key.",
        this.name,
      );
    }
  }

  // GET helper: x-api-key auth, JSON, surfaces HTTP errors (401=bad key,
  // 402/429=out of credits/rate) as clear ProviderErrors.
  private async get(path: string, params: Record<string, string>): Promise<Record<string, unknown>> {
    this.requireKey();
    const qs = new URLSearchParams(params).toString();
    let res: Response;
    try {
      res = await fetch(`${BASE}${path}?${qs}`, {
        headers: { "x-api-key": this.apiKey, accept: "application/json" },
        signal: AbortSignal.timeout(30_000),
      });
    } catch (err) {
      throw new ProviderError(`scrapecreators: request failed: ${(err as Error).message}`, this.name);
    }
    if (res.status === 401) {
      throw new ProviderError("scrapecreators: 401 — invalid API key.", this.name);
    }
    if (res.status === 402 || res.status === 429) {
      throw new ProviderError(
        `scrapecreators: ${res.status} — out of credits or rate-limited (free trial is small).`,
        this.name,
      );
    }
    if (!res.ok) {
      throw new ProviderError(
        `scrapecreators: HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`,
        this.name,
      );
    }
    try {
      return (await res.json()) as Record<string, unknown>;
    } catch {
      throw new ProviderError("scrapecreators: response was not JSON.", this.name);
    }
  }

  // A specific creator's recent videos (daemon polling + handle search +
  // keyword-discovery fan-out). NOTE: this endpoint returns a DIFFERENT shape
  // from keyword/hashtag search — each row is wrapped in `.media` with
  // play_count/code/user.username/taken_at(epoch) instead of the flat
  // shortcode/owner.username/video_play_count/ISO shape. Use the dedicated
  // unwrapping mapper, not mapItems, or every creator returns 0 rows.
  async fetchRecentVideos(handle: string, platform: Platform, _days: number): Promise<RawVideo[]> {
    this.assertInstagram(platform, "fetchRecentVideos");
    const body = await this.get("/v1/instagram/user/reels", { handle: handle.replace(/^@/, "") });
    const items = body.items ?? body.reels ?? body.posts;
    if (Array.isArray(items) && items.some(isMediaWrapped)) return mapUserReelItems(items);
    // Some user-reels responses are already flat (older API shape) — fall back.
    return mapItems(items);
  }

  // KEYWORD/caption search — the untagged-mention unlock. Catches creators who
  // name the brand in caption/bio without a hashtag or @-tag.
  async fetchKeywordVideos(keyword: string, platform: Platform, days: number): Promise<RawVideo[]> {
    this.assertInstagram(platform, "fetchKeywordVideos");
    const body = await this.get("/v2/instagram/reels/search", {
      query: keyword,
      date_posted: datePosted(days),
    });
    return mapItems(body.reels);
  }

  // Hashtag search — robust (server-side reels filter, no carousel guesswork).
  async fetchHashtagVideos(tag: string, platform: Platform, days: number): Promise<RawVideo[]> {
    this.assertInstagram(platform, "fetchHashtagVideos");
    const body = await this.get("/v1/instagram/search/hashtag", {
      hashtag: tag.replace(/^#/, ""),
      media_type: "reels",
      date_posted: datePosted(days),
    });
    return mapItems(body.posts);
  }

  private assertInstagram(platform: Platform, fn: string): void {
    if (platform !== "instagram") {
      throw new ProviderError(
        `scrapecreators.${fn}: only instagram is wired here (got '${platform}'). Use tiktok-oss for TikTok.`,
        this.name,
      );
    }
  }
}

// Map a ScrapeCreators reel/post array → RawVideo[], validating + dropping
// malformed rows (mirrors the IG bridge's per-row guard). Exported for tests.
export function mapItems(items: unknown): RawVideo[] {
  if (!Array.isArray(items)) return [];
  const out: RawVideo[] = [];
  const num = (x: unknown): number => (typeof x === "number" && Number.isFinite(x) ? x : 0);
  const str = (x: unknown): string => (typeof x === "string" ? x : "");
  for (const it of items) {
    if (it === null || typeof it !== "object") continue;
    const r = it as Record<string, unknown>;
    const shortcode =
      typeof r.shortcode === "string" ? r.shortcode : typeof r.code === "string" ? r.code : null;
    if (!shortcode) continue; // need the unique id
    const owner = (r.owner ?? {}) as Record<string, unknown>;
    const captionField =
      typeof r.caption === "string"
        ? r.caption
        : str((r.caption as Record<string, unknown> | undefined)?.text);
    out.push({
      platform: "instagram",
      external_id: shortcode,
      posted_at: normalizeTaken(r.taken_at),
      caption: captionField,
      thumbnail_url: str(r.thumbnail_src) || str(r.thumbnail_url),
      video_url: str(r.video_url) || `https://www.instagram.com/reel/${shortcode}/`,
      // Prefer play_count (the headline IG "plays" metric), then view_count.
      view_count: num(r.video_play_count) || num(r.video_view_count),
      like_count: num(r.like_count),
      comment_count: num(r.comment_count),
      share_count: 0, // IG doesn't expose shares
      author_handle: (str(owner.username) || str(r.username)).replace(/^@/, "") || null,
    });
  }
  return out;
}

function normalizeTaken(taken: unknown): string {
  if (typeof taken === "string" && taken) return taken; // already ISO 8601
  if (typeof taken === "number" && Number.isFinite(taken)) {
    const ms = taken > 1e12 ? taken : taken * 1000; // seconds vs ms epoch
    return new Date(ms).toISOString();
  }
  return new Date(0).toISOString(); // unknown → epoch (never looks "fresh")
}

// True when a user-reels row carries the nested `.media` envelope.
function isMediaWrapped(it: unknown): boolean {
  return it !== null && typeof it === "object" && typeof (it as Record<string, unknown>).media === "object";
}

// Map the /v1/instagram/user/reels response → RawVideo[]. This endpoint's rows
// differ from keyword/hashtag search: each is `{media:{...}}` with `code`
// (not shortcode), `play_count`/`ig_play_count` (not video_play_count),
// `user.username` (not owner.username), and a NUMERIC epoch `taken_at` (not
// ISO). Rows without a usable code are dropped. Exported for tests.
export function mapUserReelItems(items: unknown): RawVideo[] {
  if (!Array.isArray(items)) return [];
  const out: RawVideo[] = [];
  const num = (x: unknown): number => (typeof x === "number" && Number.isFinite(x) ? x : 0);
  const str = (x: unknown): string => (typeof x === "string" ? x : "");
  for (const it of items) {
    if (it === null || typeof it !== "object") continue;
    // Accept both the wrapped `{media:{...}}` and an already-unwrapped row.
    const wrapped = (it as Record<string, unknown>).media;
    const m = (wrapped && typeof wrapped === "object" ? wrapped : it) as Record<string, unknown>;
    const code = typeof m.code === "string" ? m.code : typeof m.shortcode === "string" ? m.shortcode : null;
    if (!code) continue;
    const user = (m.user ?? m.owner ?? {}) as Record<string, unknown>;
    const captionField =
      typeof m.caption === "string"
        ? m.caption
        : str((m.caption as Record<string, unknown> | undefined)?.text);
    out.push({
      platform: "instagram",
      external_id: code,
      posted_at: normalizeTaken(m.taken_at),
      caption: captionField,
      thumbnail_url: str(m.display_uri) || str(m.thumbnail_src) || str(m.thumbnail_url),
      // Prefer the nested video_versions URL; fall back to a flat video_url
      // (older/mixed shape) before synthesizing the canonical reel permalink.
      video_url:
        str((m.video_versions as Record<string, unknown>[] | undefined)?.[0]?.url as string) ||
        str(m.video_url) ||
        `https://www.instagram.com/reel/${code}/`,
      // play_count/ig_play_count is the headline IG "plays"; fall back through
      // the nested view_count AND the flat-shape video_play_count/video_view_count
      // aliases so a mixed/flat row doesn't lose its metrics.
      view_count:
        num(m.play_count) ||
        num(m.ig_play_count) ||
        num(m.view_count) ||
        num(m.video_play_count) ||
        num(m.video_view_count),
      like_count: num(m.like_count),
      comment_count: num(m.comment_count),
      share_count: 0, // IG doesn't expose shares
      author_handle: (str(user.username) || str(m.username)).replace(/^@/, "") || null,
    });
  }
  return out;
}
