import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ScrapeCreatorsProvider, mapItems } from "../src/providers/scrapecreators.ts";
import { getProvider } from "../src/providers/index.ts";
import { effectiveScraperKey } from "../src/lib/config.ts";
import type { Config } from "../src/types.ts";

describe("ScrapeCreators mapItems — response → RawVideo", () => {
  test("maps a reel with all fields (prefers play_count, owner.username, ISO taken_at)", () => {
    const out = mapItems([
      {
        shortcode: "ABC123",
        caption: "love @befreed for studying",
        like_count: 5000,
        comment_count: 42,
        video_view_count: 100000,
        video_play_count: 250000,
        owner: { username: "studytok.jane" },
        taken_at: "2026-06-10T12:00:00.000Z",
        video_url: "https://scontent.cdninstagram.com/x.mp4",
        thumbnail_src: "https://scontent.cdninstagram.com/x.jpg",
      },
    ]);
    expect(out).toHaveLength(1);
    const v = out[0]!;
    expect(v.platform).toBe("instagram");
    expect(v.external_id).toBe("ABC123");
    expect(v.view_count).toBe(250000); // play_count preferred over view_count
    expect(v.like_count).toBe(5000);
    expect(v.author_handle).toBe("studytok.jane");
    expect(v.posted_at).toBe("2026-06-10T12:00:00.000Z");
    expect(v.caption).toContain("@befreed");
    expect(v.video_url).toContain(".mp4");
  });

  test("falls back to view_count when no play_count, and to reel URL when no video_url", () => {
    const v = mapItems([{ shortcode: "X", video_view_count: 99, owner: { username: "a" } }])[0]!;
    expect(v.view_count).toBe(99);
    expect(v.video_url).toBe("https://www.instagram.com/reel/X/");
  });

  test("accepts `code` as the shortcode alias and a caption object {text}", () => {
    const v = mapItems([{ code: "Y", caption: { text: "hi" }, owner: { username: "b" } }])[0]!;
    expect(v.external_id).toBe("Y");
    expect(v.caption).toBe("hi");
  });

  test("numeric epoch taken_at normalizes to ISO; missing → epoch", () => {
    const sec = mapItems([{ shortcode: "S", taken_at: 1700000000, owner: { username: "c" } }])[0]!;
    expect(sec.posted_at.startsWith("2023-")).toBe(true);
    const none = mapItems([{ shortcode: "N", owner: { username: "d" } }])[0]!;
    expect(none.posted_at).toBe(new Date(0).toISOString());
  });

  test("drops malformed rows (null, no shortcode, non-object) without throwing", () => {
    const out = mapItems([null, 42, "x", { caption: "no shortcode" }, { shortcode: "OK" }]);
    expect(out.map((v) => v.external_id)).toEqual(["OK"]);
  });

  test("non-array input → empty array", () => {
    expect(mapItems(undefined)).toEqual([]);
    expect(mapItems({})).toEqual([]);
  });
});

describe("ScrapeCreators provider guards", () => {
  test("missing key → clear ProviderError on any fetch", async () => {
    const p = new ScrapeCreatorsProvider("");
    await expect(p.fetchKeywordVideos("befreed", "instagram", 30)).rejects.toThrow(/API key missing/);
  });

  test("rejects non-instagram platform", async () => {
    const p = new ScrapeCreatorsProvider("k");
    await expect(p.fetchHashtagVideos("x", "tiktok", 30)).rejects.toThrow(/only instagram/);
  });
});

describe("getProvider IG routing: ScrapeCreators when key present, free fallback otherwise", () => {
  test("tiktok-oss + instagram + key → ScrapeCreators (the keyword-search upgrade)", () => {
    const cfg = { scraper_provider: "tiktok-oss", scraper_api_key: "sk-test" } as Config;
    expect(getProvider(cfg, "instagram").name).toBe("scrapecreators");
  });

  test("tiktok-oss + instagram + NO key → free instagram-oss fallback", () => {
    // Isolate HOME so a real ~/.ugcspy/scrapecreators.key on the dev machine
    // can't leak in — assert the genuine keyless fallback deterministically.
    const savedHome = process.env.UGCSPY_HOME;
    const savedKey = process.env.UGCSPY_SCRAPER_API_KEY;
    process.env.UGCSPY_HOME = mkdtempSync(join(tmpdir(), "ugcspy-sc-nokey-"));
    delete process.env.UGCSPY_SCRAPER_API_KEY;
    try {
      const cfg = { scraper_provider: "tiktok-oss" } as Config;
      expect(getProvider(cfg, "instagram").name).toBe("instagram-oss");
    } finally {
      if (savedHome === undefined) delete process.env.UGCSPY_HOME;
      else process.env.UGCSPY_HOME = savedHome;
      if (savedKey !== undefined) process.env.UGCSPY_SCRAPER_API_KEY = savedKey;
    }
  });

  test("tiktok-oss + tiktok → tiktok-oss regardless of key", () => {
    const cfg = { scraper_provider: "tiktok-oss", scraper_api_key: "sk-test" } as Config;
    expect(getProvider(cfg, "tiktok").name).toBe("tiktok-oss");
  });

  test("a whitespace-only config key is NOT used as the key (codex P2 — blank x-api-key avoided)", () => {
    // effectiveScraperKey must trim and reject a whitespace-only config/env key,
    // so it never gets sent as a blank x-api-key. (Routing then depends on the
    // key FILE, tested separately — here we assert the blank string isn't the key.)
    const k = effectiveScraperKey({ scraper_provider: "tiktok-oss", scraper_api_key: "   " } as Config);
    expect(k).not.toBe("   ");
    expect(k === undefined || k.trim().length > 0).toBe(true);
  });
});
