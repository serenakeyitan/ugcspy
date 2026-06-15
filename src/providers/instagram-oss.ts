import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import type { Platform, RawVideo } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";
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

  private async runBridge(payload: Record<string, unknown>): Promise<RawVideo[]> {
    const raw = await this.spawnBridge(payload);
    return parseIgVideosResponse(raw.stdout, raw.stderr);
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

// Parse a {videos} / {error,code} response into RawVideo[]. The bridge reports
// errors in-band as {error, code}; `re_login_required` is the one the operator
// must act on, so it gets an actionable hint.
export function parseIgVideosResponse(stdout: string, stderr: string): RawVideo[] {
  const parsed = parseIgJson(stdout, stderr);
  if (parsed.error) {
    const code = String(parsed.code ?? "error");
    const hint =
      code === "re_login_required"
        ? " (set UGCSPY_IG_COOKIE_BROWSER to a browser logged into Instagram, or log in again)"
        : "";
    throw new ProviderError(`${PROVIDER}: ${parsed.error}${hint}`, PROVIDER);
  }
  const videos = parsed.videos;
  if (!Array.isArray(videos)) {
    throw new ProviderError(
      `${PROVIDER}: bridge returned no videos array: ${(stdout ?? "").slice(0, 300)}`,
      PROVIDER,
    );
  }
  return videos as RawVideo[];
}
