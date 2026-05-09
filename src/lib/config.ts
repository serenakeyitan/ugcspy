import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { Config } from "../types.ts";

export const CONFIG_PATH = join(homedir(), ".ugcspy", "config.json");

const DEFAULT_CONFIG: Config = {
  scraper_provider: "mock",
};

export function loadConfig(path: string = CONFIG_PATH): Config {
  if (!existsSync(path)) return { ...DEFAULT_CONFIG };
  const raw = readFileSync(path, "utf8");
  const parsed = JSON.parse(raw) as Partial<Config>;
  return { ...DEFAULT_CONFIG, ...parsed };
}

export function saveConfig(config: Config, path: string = CONFIG_PATH): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(config, null, 2));
  // Best-effort 0600 — readable only by owner. Skips silently on platforms that don't support it.
  try {
    chmodSync(path, 0o600);
  } catch {
    /* noop */
  }
}

export function effectiveAnthropicKey(config: Config): string | undefined {
  return process.env.ANTHROPIC_API_KEY ?? config.anthropic_api_key;
}

export function effectiveOpenAIKey(config: Config): string | undefined {
  return process.env.OPENAI_API_KEY ?? config.openai_api_key;
}

export function effectiveScraperKey(config: Config): string | undefined {
  return process.env.UGCSPY_SCRAPER_API_KEY ?? config.scraper_api_key;
}
