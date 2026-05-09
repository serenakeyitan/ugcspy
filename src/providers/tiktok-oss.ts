import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import type { Platform, RawVideo } from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";

// Bridge to davidteather/TikTok-Api (Python). Free, OSS, actively maintained
// (v7.3.3 shipped April 2026). Instagram is intentionally NOT supported by this
// provider — no production-grade free IG scraper exists right now (see Open Q
// in DESIGN.md). Use ScrapeCreators for IG when you need it.
//
// Setup (one-time):
//   pip install -r scripts/requirements.txt
//   python3 -m playwright install chromium
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

    const scriptPath = resolveScript();
    // Inherit the parent env so user-site Python packages
    // (`pip install --user`) and PATH are visible to the subprocess.
    // Without this, Bun.spawn runs with a stripped env and Python misses
    // ~/Library/Python/.../site-packages on macOS.
    const proc = Bun.spawn(["python3", scriptPath], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });

    proc.stdin.write(JSON.stringify({ handle, days }));
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
      throw new ProviderError(`tiktok-oss: bridge returned non-array: ${stdout.slice(0, 300)}`, this.name);
    }
    return parsed as RawVideo[];
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
  // Resolve scripts/tiktok_fetch.py relative to this file. Works in dev (src/) and after
  // `bun build` because we keep scripts/ alongside the published package.
  const here = dirname(fileURLToPath(import.meta.url));
  // src/providers/ -> ../../scripts/
  return resolve(here, "..", "..", "scripts", "tiktok_fetch.py");
}
