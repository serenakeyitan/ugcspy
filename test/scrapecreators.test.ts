import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ScrapeCreatorsProvider,
  mapItems,
  mapUserReelItems,
} from "../src/providers/scrapecreators.ts";
import { getProvider } from "../src/providers/index.ts";
import { expandCreators } from "../src/commands/search.ts";
import { effectiveScraperKey } from "../src/lib/config.ts";
import type { Config, RawVideo } from "../src/types.ts";

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

describe("ScrapeCreators mapUserReelItems — nested /user/reels shape", () => {
  // The user-reels endpoint wraps each row in `.media` with DIFFERENT field
  // names than keyword search: code (not shortcode), play_count (not
  // video_play_count), user.username (not owner.username), numeric epoch
  // taken_at (not ISO). This is the exact bug that made fetchRecentVideos
  // return 0 for every creator.
  test("unwraps .media and maps play_count/code/user.username/epoch", () => {
    const out = mapUserReelItems([
      {
        media: {
          code: "DZG66Hxs9Yr",
          caption: { text: "Whats your favourite colour?" },
          like_count: 9209,
          comment_count: 106,
          play_count: 388152,
          ig_play_count: 388152,
          user: { username: "growwith.nomes" },
          taken_at: 1780456254, // numeric epoch seconds
          display_uri: "https://scontent.cdninstagram.com/x.jpg",
          video_versions: [{ url: "https://scontent.cdninstagram.com/x.mp4" }],
        },
      },
    ]);
    expect(out).toHaveLength(1);
    const v = out[0]!;
    expect(v.platform).toBe("instagram");
    expect(v.external_id).toBe("DZG66Hxs9Yr");
    expect(v.view_count).toBe(388152); // play_count
    expect(v.like_count).toBe(9209);
    expect(v.author_handle).toBe("growwith.nomes");
    expect(v.caption).toBe("Whats your favourite colour?");
    expect(v.video_url).toContain(".mp4");
    expect(v.posted_at.startsWith("2026-")).toBe(true); // epoch → ISO
  });

  test("prefers play_count, falls back to ig_play_count then view_count", () => {
    expect(mapUserReelItems([{ media: { code: "A", ig_play_count: 5, view_count: 9 } }])[0]!.view_count).toBe(5);
    expect(mapUserReelItems([{ media: { code: "B", view_count: 9 } }])[0]!.view_count).toBe(9);
  });

  test("accepts an already-unwrapped flat row (older API shape)", () => {
    const v = mapUserReelItems([{ code: "F", play_count: 7, user: { username: "x" } }])[0]!;
    expect(v.external_id).toBe("F");
    expect(v.view_count).toBe(7);
    expect(v.author_handle).toBe("x");
  });

  test("a flat-shape row keeps its video_url and video_play/view_count aliases", () => {
    // codex P2: a flat row routed through this mapper must not lose its direct
    // URL or metrics just because it uses the keyword-search field names.
    const v = mapUserReelItems([
      { code: "G", video_url: "https://x.cdn/g.mp4", video_play_count: 321, like_count: 4 },
    ])[0]!;
    expect(v.video_url).toBe("https://x.cdn/g.mp4");
    expect(v.view_count).toBe(321);
    const w = mapUserReelItems([{ code: "H", video_view_count: 88 }])[0]!;
    expect(w.view_count).toBe(88);
  });

  test("drops rows with no usable code; non-array → []", () => {
    expect(mapUserReelItems([{ media: { caption: "no code" } }, null, 3])).toEqual([]);
    expect(mapUserReelItems(undefined)).toEqual([]);
  });

  test("falls back to reel URL when video_versions missing", () => {
    const v = mapUserReelItems([{ media: { code: "Z" } }])[0]!;
    expect(v.video_url).toBe("https://www.instagram.com/reel/Z/");
  });
});

describe("expandCreators — keyword/hashtag fan-out to full reels", () => {
  const now = Date.now();
  const mk = (id: string, handle: string, daysAgo: number, views: number): RawVideo => ({
    platform: "instagram",
    external_id: id,
    posted_at: new Date(now - daysAgo * 86_400_000).toISOString(),
    caption: "",
    thumbnail_url: "",
    video_url: "",
    view_count: views,
    like_count: 0,
    comment_count: 0,
    share_count: 0,
    author_handle: handle,
  });

  test("pulls each creator's full roster, deduped, roster row wins; in-window only", async () => {
    const discovered = [mk("disc_A", "alice", 5, 100), mk("disc_B", "bob", 10, 50)];
    const provider = {
      name: "fake",
      async fetchRecentVideos(handle: string): Promise<RawVideo[]> {
        if (handle === "alice")
          return [mk("disc_A", "alice", 5, 999), mk("ros_A2", "alice", 3, 400)];
        if (handle === "bob") return [mk("ros_B_stale", "bob", 40, 1)];
        return [];
      },
    } as unknown as Parameters<typeof expandCreators>[0];
    const out = await expandCreators(provider, discovered, "instagram", 30);
    const ids = out.map((v) => v.external_id).sort();
    expect(ids).toEqual(["disc_A", "disc_B", "ros_A2"]); // ros_B_stale (40d) dropped
    // roster row overwrote the discovered view count
    expect(out.find((v) => v.external_id === "disc_A")!.view_count).toBe(999);
  });

  test("a creator whose roster fetch throws keeps their (in-window) discovered video", async () => {
    const discovered = [mk("disc_A", "alice", 5, 100)];
    const provider = {
      name: "fake",
      async fetchRecentVideos(): Promise<RawVideo[]> {
        throw new Error("private/throttled");
      },
    } as unknown as Parameters<typeof expandCreators>[0];
    const out = await expandCreators(provider, discovered, "instagram", 30);
    expect(out.map((v) => v.external_id)).toEqual(["disc_A"]);
  });

  test("out-of-window discovered rows are dropped (window applies to both, codex P2)", async () => {
    // discovery's coarse `last-month` window can exceed --days 30; the function
    // contract is in-window only, so a 40-day-old discovered row must drop.
    const discovered = [mk("fresh", "alice", 5, 100), mk("stale", "alice", 40, 9999)];
    const provider = {
      name: "fake",
      async fetchRecentVideos(): Promise<RawVideo[]> {
        return [];
      },
    } as unknown as Parameters<typeof expandCreators>[0];
    const out = await expandCreators(provider, discovered, "instagram", 30);
    expect(out.map((v) => v.external_id)).toEqual(["fresh"]);
  });

  test("a malformed posted_at is dropped, not admitted via NaN comparison (codex P2)", async () => {
    const bad: RawVideo = { ...mk("bad", "alice", 0, 1), posted_at: "not-a-date" };
    const provider = {
      name: "fake",
      async fetchRecentVideos(): Promise<RawVideo[]> {
        return [{ ...mk("badroster", "alice", 0, 1), posted_at: "also-bad" }];
      },
    } as unknown as Parameters<typeof expandCreators>[0];
    const out = await expandCreators(provider, [bad], "instagram", 30);
    expect(out).toEqual([]); // neither the bad discovered nor the bad roster row survives
  });

  test("empty discovery → empty result, no roster calls", async () => {
    let calls = 0;
    const provider = {
      name: "fake",
      async fetchRecentVideos(): Promise<RawVideo[]> {
        calls++;
        return [];
      },
    } as unknown as Parameters<typeof expandCreators>[0];
    expect(await expandCreators(provider, [], "instagram", 30)).toEqual([]);
    expect(calls).toBe(0);
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
