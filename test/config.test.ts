import { afterEach, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { loadConfig, saveConfig } from "../src/lib/config.ts";

// All tests run against throwaway temp dirs — never ~/.ugcspy.
const dirs: string[] = [];
function tempDir(): string {
  const d = mkdtempSync(join(tmpdir(), "ugcspy-config-test-"));
  dirs.push(d);
  return d;
}
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

describe("loadConfig", () => {
  test("missing file returns defaults (mock provider)", () => {
    const p = join(tempDir(), "config.json");
    expect(loadConfig(p).scraper_provider).toBe("mock");
  });

  test("corrupted JSON throws an actionable error, not a raw SyntaxError", () => {
    // Regression: a truncated config.json (crash/^C mid-write) bricked EVERY
    // command — including `ugcspy init` — with a raw JSON.parse stack trace.
    const p = join(tempDir(), "config.json");
    writeFileSync(p, '{"scraper_provider": "tikt'); // truncated mid-write
    expect(() => loadConfig(p)).toThrow(/ugcspy init/);
    expect(() => loadConfig(p)).toThrow(/corrupted/);
  });

  test("partial config merges over defaults", () => {
    const p = join(tempDir(), "config.json");
    writeFileSync(p, JSON.stringify({ default_slack_webhook: "https://hooks.example/x" }));
    const c = loadConfig(p);
    expect(c.scraper_provider).toBe("mock"); // default preserved
    expect(c.default_slack_webhook).toBe("https://hooks.example/x");
  });
});

describe("saveConfig (atomic, owner-only)", () => {
  test("roundtrips through loadConfig and creates parent dirs", () => {
    const p = join(tempDir(), "nested", "config.json");
    saveConfig({ scraper_provider: "tiktok-oss", scraper_api_key: "sekrit" }, p);
    const c = loadConfig(p);
    expect(c.scraper_provider).toBe("tiktok-oss");
    expect(c.scraper_api_key).toBe("sekrit");
  });

  test("file is 0600 and its dir 0700 from the moment of creation; no tmp file left", () => {
    const p = join(tempDir(), "nested", "config.json");
    saveConfig({ scraper_provider: "mock", default_slack_webhook: "https://h/x" }, p);
    expect(statSync(p).mode & 0o777).toBe(0o600);
    expect(statSync(dirname(p)).mode & 0o777).toBe(0o700);
    expect(existsSync(`${p}.tmp`)).toBe(false); // renamed over, not left behind
  });

  test("overwriting an existing config keeps it readable (atomic rename)", () => {
    const p = join(tempDir(), "config.json");
    saveConfig({ scraper_provider: "mock" }, p);
    saveConfig({ scraper_provider: "tiktok-oss" }, p);
    expect(loadConfig(p).scraper_provider).toBe("tiktok-oss");
    expect(statSync(p).mode & 0o777).toBe(0o600);
  });
});
