import { fileURLToPath } from "node:url";
import { platform } from "node:os";
import { dirname, resolve } from "node:path";
import type { Platform, RawVideo } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";
import { venvExists, venvPython } from "../lib/venv.ts";

// Bridge to davidteather/TikTok-Api (Python). Free, OSS, actively maintained
// (v7.3.3 shipped April 2026). Instagram is intentionally NOT supported by this
// provider — no production-grade free IG scraper exists right now (see Open Q
// in DESIGN.md). Use ScrapeCreators for IG when you need it.
//
// Setup (one-time): `ugcspy install-deps` creates a managed venv at
// ~/.ugcspy/venv and installs TikTokApi + Chromium there. This provider
// invokes that venv's python so a system-Python upgrade can't silently
// invalidate the deps.
//
// Two modes via the Python bridge:
//   - user mode:    fetch a handle's own posts
//   - hashtag mode: fetch posts tagged with #X by ANY creator (third-party UGC)
export class TikTokOssProvider implements DataProvider {
  readonly name = "tiktok-oss";

  async fetchRecentVideos(
    handle: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok. For Instagram Reels use 'scrapecreators'.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "user", handle, days });
  }

  async fetchHashtagVideos(
    tag: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok. For Instagram Reels use 'scrapecreators'.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "hashtag", tag, days });
  }

  // Keyword / niche discovery. Served by the tikwm relay (free, no key) inside
  // the Python bridge — this is the broad-corpus path that the brand-hashtag
  // model structurally cannot reach. The bridge uses stdlib urllib (no
  // TikTokApi/Chromium session needed for this mode), but still runs in the
  // managed venv for one code path + the shared RawVideo contract.
  async fetchKeywordVideos(
    keyword: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok keyword search.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "keyword", keyword, days });
  }

  private async runBridge(payload: Record<string, unknown>): Promise<RawVideo[]> {
    const pythonBin = resolveBridgePython(venvExists(), payload.mode);
    const scriptPath = resolveScript();
    const proc = Bun.spawn([pythonBin, scriptPath], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });

    proc.stdin.write(JSON.stringify(payload));
    await proc.stdin.end();

    // Overall deadline so a hung bridge (stuck network read, throttle loop)
    // can't wedge a search or a daemon tick forever. Tune via
    // UGCSPY_BRIDGE_TIMEOUT_MS; the default 30min leaves room for a full
    // Stage-2 creator walk on an active brand.
    const timeoutMs = bridgeTimeoutMs();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill();
    }, timeoutMs);

    const [stdout, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    const exit = await proc.exited;
    clearTimeout(timer);

    if (timedOut) {
      throw new ProviderError(
        `tiktok-oss: bridge timed out after ${timeoutMs}ms — raise UGCSPY_BRIDGE_TIMEOUT_MS if the walk legitimately needs longer.`,
        this.name,
      );
    }
    return parseBridgeOutput(exit, stdout, stderr);
  }
}

// Decide which python runs the bridge. Keyword/niche discovery is pure HTTP
// (tikwm + stdlib urllib) — it needs NO venv, so it can fall back to a system
// interpreter rather than forcing an install. user/hashtag modes still require
// the venv (yt-dlp walk; TikTokApi fallbacks). On Windows the system binary is
// `python` (python.org installs no `python3`, and the WindowsApps `python3.exe`
// is a Store stub) — match venv.ts / install-deps' platform handling.
// Exported for tests.
export function resolveBridgePython(hasVenv: boolean, mode: unknown): string {
  if (hasVenv) return venvPython();
  if (mode === "keyword") {
    return platform() === "win32" ? "python" : "python3"; // stdlib-only path; resolved on PATH
  }
  throw new ProviderError(
    `tiktok-oss venv not found at ${venvPython()}. Run \`ugcspy install-deps\` to set it up (one-time, ~30-60s; browser-free). ` +
      `(Tip: keyword/niche search — \`--mode keyword\` — works without the venv.)`,
    "tiktok-oss",
  );
}

// Bridge deadline in ms (default 30min). Invalid/absent env values fall back
// to the default. Exported for tests.
export function bridgeTimeoutMs(
  env: Record<string, string | undefined> = process.env,
): number {
  const raw = env.UGCSPY_BRIDGE_TIMEOUT_MS;
  const n = raw === undefined ? NaN : Number.parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : 1_800_000;
}

// Pure parse of the bridge's (exit, stdout, stderr) — exported for tests.
//
// Maps the Python bridge's `_author` field onto our typed `author_handle`.
// Fallback: when the bridge couldn't supply an author (e.g. the tikwm feed
// item had no author.unique_id), parse the handle out of the video_url —
// every TikTok URL is `https://www.tiktok.com/@<handle>/video/<id>`, so the
// author is ALREADY present in data we hold. This is free (no extra fetch /
// no /user/info lookup) and recovers the rows that previously rendered as
// "(unknown)". Prefer the explicit field; only derive from the URL when it's
// missing, so a real author is never overwritten by a URL parse.
export function parseBridgeOutput(exit: number, stdout: string, stderr: string): RawVideo[] {
  const name = "tiktok-oss";
  if (exit !== 0) {
    // NOTE: `||` (not `??`) for the stderr fallback — trim() always returns a
    // string, so a bridge that died with empty output (OOM-kill, SIGKILL)
    // must still produce a non-blank error naming the exit code.
    const errBody =
      parseErrorBody(stdout) ?? (stderr.trim() || `bridge exited ${exit} with no output`);
    throw new ProviderError(`tiktok-oss: ${errBody}`, name);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-JSON output: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  if (!Array.isArray(parsed)) {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-array: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  return (parsed as Array<RawVideo & { _author?: string }>).map((v) => {
    const out: RawVideo = { ...v };
    delete (out as { _author?: string })._author;
    const author = v._author?.trim() || authorFromUrl(out.video_url);
    if (author) out.author_handle = author;
    return out;
  });
}

// Extract the @handle from a TikTok video URL. Returns "" when the URL has no
// /@handle/ segment (e.g. a bare `tiktok.com/video/<id>` the relay sometimes
// returns). Strips a leading @ and lower-cases for consistency with the rest of
// the pipeline (handles are case-insensitive on TikTok).
export function authorFromUrl(url: string | undefined): string {
  if (!url) return "";
  const m = url.match(/tiktok\.com\/@([^/?#]+)/i);
  return m ? m[1]!.replace(/^@/, "").toLowerCase() : "";
}

function parseErrorBody(stdout: string): string | null {
  try {
    const obj = JSON.parse(stdout);
    if (obj && typeof obj === "object" && typeof obj.error === "string") return obj.error;
  } catch {
    /* ignore */
  }
  return null;
}

function resolveScript(): string {
  // The Python bridge lives in scripts/tiktok_fetch.py at the repo root.
  // This file may be running from one of three locations depending on how
  // ugcspy was invoked, so we walk up from import.meta.url and try each
  // candidate path until we find the script:
  //
  //   - dev:   src/providers/tiktok-oss.ts -> ../../scripts/
  //   - dist:  dist/cli.js                  -> ../scripts/
  //   - npm:   node_modules/ugcspy/dist/cli.js -> ../scripts/
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "scripts", "tiktok_fetch.py"),
    resolve(here, "..", "scripts", "tiktok_fetch.py"),
  ];
  for (const path of candidates) {
    try {
      // Bun.file is sync metadata; existsSync would be cheaper but Bun.file
      // works without an extra import.
      const f = Bun.file(path);
      if (f.size > 0) return path;
    } catch {
      // file doesn't exist; try next
    }
  }
  // Fall back to the dev path so the error is human-readable.
  return candidates[0]!;
}
