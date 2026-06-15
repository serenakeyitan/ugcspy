import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import type { Platform, RawVideo, TranscriptDoc } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";
import { TikTokOssProvider } from "./tiktok-oss.ts";
import { venvExists, venvPython } from "../lib/venv.ts";

// Browser-free Instagram bridge — the IG sibling of tiktok-oss. Free/OSS, built
// on a HYBRID of two tools driven off a logged-in IG browser session:
//   1. gallery-dl  — walks a creator's roster (shortcode, likes, caption,
//      downloadable video_url) fast, in bulk.
//   2. instaloader — enriches each shortcode with view_count / play_count
//      (gallery-dl's listing endpoint omits these; instaloader's single-post
//      GraphQL call returns them).
// The combination yields a complete IG VideoRecord WITH view counts, so IG
// breakout/threshold alerts run at parity with TikTok. See DESIGN.md for the
// data-source bakeoff that established this.
//
// What IG does NOT support (no honest free source — TikTok-only): trending,
// snowball/similar (follow-graph is private), keyword search (no free IG search
// relay; tikwm is TikTok-only).
//
// AUTH: needs a live logged-in IG session (cookies exported from a browser,
// default safari — override with UGCSPY_IG_COOKIE_BROWSER). Sessions expire — a
// missing/expired sessionid surfaces as a clear "re-login required"
// ProviderError rather than a silent empty result.
//
// All real work happens in scripts/instagram_fetch.py (managed venv); this class
// is the spawn + parse seam, mirroring tiktok-oss.ts.
export class InstagramOssProvider implements DataProvider {
  readonly name = "instagram-oss";

  // True if the most recent fetch hit an IG throttle. The daemon reads this to
  // skip remaining IG watches for the tick (a cooldown) rather than launching
  // fresh enrich loops against an already-rate-limited account (codex P2).
  lastRunThrottled = false;

  // How many roster posts to enrich with view/play counts (the per-post GraphQL
  // step that costs ~4s each). Resolved from the user's depth choice (tier) by
  // the caller; the bridge caps at this. undefined → the bridge's own default.
  constructor(private enrichCount?: number) {}

  async fetchRecentVideos(handle: string, platform: Platform, days: number): Promise<RawVideo[]> {
    if (platform !== "instagram") {
      throw new ProviderError(
        `Provider 'instagram-oss' only supports instagram (got '${platform}'). Use 'tiktok-oss' for TikTok.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "user", handle, days, max_enrich: this.enrichCount });
  }

  // DISCOVER all creators who posted under a brand hashtag (the IG analog of the
  // TikTok hashtag scout). gallery-dl's explore/tags/<tag>/ extractor + the
  // logged-in session returns posts each carrying their OWN creator, so the
  // caller can rank every UGC creator for the brand. Then enrich_views adds the
  // view counts. Third-party UGC discovery — full parity with TikTok.
  async fetchHashtagVideos(tag: string, platform: Platform, days: number): Promise<RawVideo[]> {
    if (platform !== "instagram") {
      throw new ProviderError(
        `Provider 'instagram-oss' only supports instagram (got '${platform}').`,
        this.name,
      );
    }
    return this.runBridge({ mode: "hashtag", tag, days, max_enrich: this.enrichCount });
  }

  // Is the configured browser logged into Instagram? Surfaces the session
  // health the IG path depends on (the daemon/init can warn before a walk).
  async sessionCheck(): Promise<{ loggedIn: boolean; igCookieCount: number; browser: string }> {
    const raw = await this.spawnBridge({ mode: "session_check" });
    const parsed = parseIgJson(raw.stdout, raw.stderr);
    return {
      loggedIn: !!parsed.logged_in,
      igCookieCount: Number(parsed.ig_cookie_count ?? 0),
      browser: String(parsed.browser ?? ""),
    };
  }

  // Transcription is platform-NEUTRAL: the transcript bridge mode just downloads
  // a video's audio (yt-dlp on the http(s) URL — proven to work on Instagram
  // Reels with NO cookies) and runs Whisper. So IG delegates to the same proven
  // transcript path as TikTok rather than duplicating the Whisper pipeline. The
  // IG video_url comes from fetchRecentVideos (a real cdninstagram .mp4) or a
  // pasted instagram.com/reel/<shortcode> URL.
  private transcriber = new TikTokOssProvider();

  async fetchTranscript(videoUrl: string): Promise<TranscriptDoc> {
    return this.transcriber.fetchTranscript(videoUrl);
  }

  async fetchTranscriptBatch(
    videoUrls: string[],
  ): Promise<Array<TranscriptDoc | { error: string }>> {
    return this.transcriber.fetchTranscriptBatch(videoUrls);
  }

  private async runBridge(payload: Record<string, unknown>): Promise<RawVideo[]> {
    const raw = await this.spawnBridge(payload);
    const { videos, throttled } = parseIgVideosResponse(raw.stdout, raw.stderr);
    this.lastRunThrottled = throttled;
    if (throttled) {
      // IG rate-limited the view-enrichment mid-run. The roster (likes/caption)
      // is still fresh and un-enriched videos keep their last-known view counts
      // (the upsert preserves a stored positive view_count against a 0). Warn the
      // operator to ease off — sustained pushing deepens the throttle and risks
      // an account flag. stderr, not a throw: the partial result is still useful.
      process.stderr.write(
        "⚠ instagram-oss: Instagram rate-limited view enrichment this run — " +
          "view counts may be stale for some videos (likes/captions are current). " +
          "Ease off (fewer creators / a lower --enrich tier / longer poll interval); " +
          "the limit is per-account and recovers in minutes-to-hours.\n",
      );
    }
    return videos;
  }

  private async spawnBridge(
    payload: Record<string, unknown>,
  ): Promise<{ exit: number; stdout: string; stderr: string }> {
    if (!venvExists()) {
      throw new ProviderError(
        `instagram-oss venv not found at ${venvPython()}. Run \`ugcspy install-deps\` to set it up (installs gallery-dl + instaloader).`,
        this.name,
      );
    }
    const scriptPath = resolveIgScript();
    const proc = Bun.spawn([venvPython(), scriptPath], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });
    proc.stdin.write(JSON.stringify(payload));
    await proc.stdin.end();

    // Deadline so a hung walk (rate-limit loop, stuck enrich) can't wedge a
    // daemon tick forever. The per-post enrich sleep makes IG slower than
    // TikTok, so the default is generous; tune via UGCSPY_BRIDGE_TIMEOUT_MS.
    const timeoutMs = Number(process.env.UGCSPY_BRIDGE_TIMEOUT_MS ?? 30 * 60 * 1000);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill();
    }, timeoutMs);

    let exit: number;
    let stdout: string;
    let stderr: string;
    try {
      [stdout, stderr] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
      ]);
      exit = await proc.exited;
    } finally {
      clearTimeout(timer);
      if (proc.exitCode === null && proc.signalCode === null) proc.kill();
    }

    if (timedOut) {
      throw new ProviderError(
        `instagram-oss: bridge timed out after ${timeoutMs}ms — raise UGCSPY_BRIDGE_TIMEOUT_MS if a large roster walk legitimately needs longer.`,
        this.name,
      );
    }
    return { exit, stdout, stderr };
  }
}

function resolveIgScript(): string {
  // scripts/instagram_fetch.py at the repo root. Same dev/dist/npm walk as the
  // TikTok bridge resolver.
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "scripts", "instagram_fetch.py"),
    resolve(here, "..", "scripts", "instagram_fetch.py"),
  ];
  for (const path of candidates) {
    try {
      if (Bun.file(path).size > 0) return path;
    } catch {
      // try next
    }
  }
  return candidates[0]!;
}

const PROVIDER = "instagram-oss";

// Parse the bridge's JSON stdout (exported so it can be unit-tested without a
// live spawn). Throws a clear ProviderError on empty/non-JSON output.
export function parseIgJson(stdout: string, stderr: string): Record<string, unknown> {
  const trimmed = (stdout ?? "").trim();
  if (!trimmed) {
    throw new ProviderError(
      `${PROVIDER}: bridge produced no output. stderr: ${(stderr ?? "").slice(0, 300)}`,
      PROVIDER,
    );
  }
  try {
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch {
    throw new ProviderError(
      `${PROVIDER}: bridge output was not JSON: ${trimmed.slice(0, 300)}`,
      PROVIDER,
    );
  }
}

export interface IgVideosResult {
  videos: RawVideo[];
  // True → IG rate-limited the view-enrichment mid-run (the caller warns + keeps
  // last-known view counts). The video list is still valid (likes/caption fresh).
  throttled: boolean;
}

// Parse a {videos, throttled} / {error,code} response. The bridge reports errors
// in-band as {error, code}; `re_login_required` is the one the operator must act
// on, so it gets an actionable hint.
export function parseIgVideosResponse(stdout: string, stderr: string): IgVideosResult {
  const parsed = parseIgJson(stdout, stderr);
  if (parsed.error) {
    const code = String(parsed.code ?? "error");
    const hint =
      code === "re_login_required"
        ? " (set UGCSPY_IG_COOKIE_BROWSER to a browser logged into Instagram, or log in again)"
        : "";
    // Carry the structured code so callers branch on it (not message text).
    throw new ProviderError(`${PROVIDER}: ${parsed.error}${hint}`, PROVIDER, undefined, code);
  }
  const videos = parsed.videos;
  if (!Array.isArray(videos)) {
    throw new ProviderError(
      `${PROVIDER}: bridge returned no videos array: ${(stdout ?? "").slice(0, 300)}`,
      PROVIDER,
    );
  }
  // VALIDATE + drop malformed rows rather than blindly casting (codex P2): a
  // null/wrong-platform/missing-field row would otherwise crash ingestion
  // (e.g. a null caption rolls back the whole upsert transaction) or persist
  // bad cross-platform data. Mirrors the TikTok parser's per-row guard.
  const valid: Record<string, unknown>[] = [];
  for (const v of videos) {
    if (isValidIgRawVideo(v)) valid.push(v);
  }
  return { videos: valid.map(coerceIgRawVideo), throttled: parsed.throttled === true };
}

// A posted_at is "real" only if it parses AND is meaningfully after the epoch.
// IMPORTANT (codex P2 round 3): the Python bridge stamps a missing date as the
// epoch in OFFSET form ("1970-01-01T00:00:00+00:00"), which does NOT
// string-equal JS's `new Date(0).toISOString()` ("…00.000Z"). So we compare by
// PARSED TIMESTAMP, not string — robust to any epoch representation. We allow a
// 1-day grace so a legitimately-1970 post (none exist on IG) isn't the cutoff;
// anything at/below ~epoch is treated as "no real date".
const EPOCH_GRACE_MS = 24 * 60 * 60 * 1000;
function hasRealDate(posted_at: unknown): boolean {
  if (typeof posted_at !== "string" || posted_at === "") return false;
  const t = Date.parse(posted_at);
  return Number.isFinite(t) && t > EPOCH_GRACE_MS;
}

// A bridge row is usable only if it has the keys the DB layer dereferences. We
// require the identity fields AND a real posted_at; other fields are coerced to
// safe defaults in coerceIgRawVideo so a partial row degrades instead of crashing.
function isValidIgRawVideo(v: unknown): v is Record<string, unknown> {
  if (v === null || typeof v !== "object" || Array.isArray(v)) return false;
  const r = v as Record<string, unknown>;
  // external_id is the unique key; platform must be instagram (never accept a
  // row claiming another platform from the IG bridge).
  if (typeof r.external_id !== "string" || r.external_id.length === 0) return false;
  if (r.platform !== "instagram") return false;
  // DROP rows with no real date: the bridge stamps a missing date as the epoch.
  // Persisting that would make the row look ancient AND, on a re-fetch where the
  // row came back partial, the upsert would OVERWRITE a previously-correct date
  // with epoch — silently yanking the video out of relative-breakout windows. A
  // dateless row is unusable for windowing anyway.
  if (!hasRealDate(r.posted_at)) return false;
  return true;
}

// Coerce a validated row into a complete RawVideo, defaulting the fields the DB
// upsert binds (a null caption/url is what crashes videos.ts). Defensive: the
// bridge already emits these, but a partial gallery-dl row might omit some.
function coerceIgRawVideo(v: Record<string, unknown>): RawVideo {
  const num = (x: unknown): number => (typeof x === "number" && Number.isFinite(x) ? x : 0);
  const str = (x: unknown): string => (typeof x === "string" ? x : "");
  return {
    platform: "instagram",
    external_id: String(v.external_id),
    // Guaranteed a real (non-epoch) date — isValidIgRawVideo dropped dateless rows.
    posted_at: str(v.posted_at),
    caption: str(v.caption),
    thumbnail_url: str(v.thumbnail_url),
    video_url: str(v.video_url),
    view_count: num(v.view_count),
    like_count: num(v.like_count),
    comment_count: num(v.comment_count),
    share_count: num(v.share_count),
    author_handle: typeof v.author_handle === "string" ? v.author_handle : null,
  };
}
