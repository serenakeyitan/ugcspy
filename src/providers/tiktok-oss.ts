import { fileURLToPath } from "node:url";
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
    // Keyword/niche discovery is pure HTTP (tikwm + stdlib urllib) — it needs
    // NO TikTokApi/Chromium venv. So if the managed venv isn't set up, fall
    // back to system python3 for keyword mode rather than forcing a ~150MB
    // install. user/hashtag modes still require the venv (they use TikTokApi).
    const isKeyword = payload.mode === "keyword";
    let pythonBin: string;
    if (venvExists()) {
      pythonBin = venvPython();
    } else if (isKeyword) {
      pythonBin = "python3"; // stdlib-only path; resolved on PATH
    } else {
      throw new ProviderError(
        `tiktok-oss venv not found at ${venvPython()}. Run \`ugcspy install-deps\` to set it up (one-time, ~30s + ~150MB Chromium download). ` +
          `(Tip: keyword/niche search — \`--mode keyword\` — works without the venv.)`,
        this.name,
      );
    }
    const scriptPath = resolveScript();
    const proc = Bun.spawn([pythonBin, scriptPath], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });

    proc.stdin.write(JSON.stringify(payload));
    await proc.stdin.end();

    const [stdout, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    const exit = await proc.exited;

    if (exit !== 0) {
      const errBody = parseErrorBody(stdout) ?? stderr.trim() ?? "unknown error";
      throw new ProviderError(`tiktok-oss: ${errBody}`, this.name);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(stdout);
    } catch {
      throw new ProviderError(
        `tiktok-oss: bridge returned non-JSON output: ${stdout.slice(0, 300)}`,
        this.name,
      );
    }
    if (!Array.isArray(parsed)) {
      throw new ProviderError(
        `tiktok-oss: bridge returned non-array: ${stdout.slice(0, 300)}`,
        this.name,
      );
    }
    // Map the Python bridge's `_author` field onto our typed `author_handle`.
    return (parsed as Array<RawVideo & { _author?: string }>).map((v) => {
      const author = v._author;
      const out: RawVideo = { ...v };
      delete (out as { _author?: string })._author;
      if (author) out.author_handle = author;
      return out;
    });
  }
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
