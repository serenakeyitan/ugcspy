import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { Config } from "../types.ts";

export const CONFIG_PATH = join(homedir(), ".ugcspy", "config.json");

// Default to mock so a fresh clone runs end-to-end with zero setup. The `init` wizard
// recommends `tiktok-oss` (free, TikTok-only) as the real-data option — it just needs
// a one-time `pip install -r scripts/requirements.txt`.
const DEFAULT_CONFIG: Config = {
  scraper_provider: "mock",
};

export function loadConfig(path: string = CONFIG_PATH): Config {
  if (!existsSync(path)) return { ...DEFAULT_CONFIG };
  const raw = readFileSync(path, "utf8");
  let parsed: Partial<Config>;
  try {
    parsed = JSON.parse(raw) as Partial<Config>;
  } catch {
    // Every command calls loadConfig first, so a truncated/corrupted file used
    // to brick the whole CLI (including `ugcspy init`) with a raw SyntaxError.
    // Fail with the recovery path instead.
    throw new Error(
      `${path} is corrupted (invalid JSON) — delete it and re-run \`ugcspy init\`.`,
    );
  }
  return { ...DEFAULT_CONFIG, ...parsed };
}

export function saveConfig(config: Config, path: string = CONFIG_PATH): void {
  // 0700 dir: the config holds API keys / webhook URLs, keep the whole dir owner-only.
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  // Atomic + private: write a 0600 temp file, then rename over the target.
  // A direct writeFileSync could leave a truncated config.json on crash/^C,
  // and creating with the umask default briefly exposed secrets at 0644.
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, JSON.stringify(config, null, 2), { mode: 0o600 });
  renameSync(tmp, path);
  // Best-effort repair for files created before the mode option existed.
  // Skips silently on platforms that don't support it.
  try {
    chmodSync(path, 0o600);
  } catch {
    /* noop */
  }
}

export function effectiveScraperKey(config: Config): string | undefined {
  return process.env.UGCSPY_SCRAPER_API_KEY ?? config.scraper_api_key;
}
