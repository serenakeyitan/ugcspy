import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import {
  applyHashtagPrecision,
  fetchByMode,
  isHashtagMatch,
  normalizeSearchOptions,
  parseQuery,
  readCachedVideos,
} from "../src/commands/search.ts";
import { migrate } from "../src/db/schema.ts";
import { normalizePostedAt, reconcileVideosWindow, upsertVideos } from "../src/db/videos.ts";
import type { DataProvider } from "../src/providers/index.ts";
import type { RawVideo } from "../src/types.ts";

describe("parseQuery", () => {
  test("@handle → user mode", () => {
    const q = parseQuery("@befreed");
    expect(q.mode).toBe("user");
    expect(q.key).toBe("@befreed");
    expect(q.value).toBe("befreed");
  });

  test("plain word → hashtag mode (UGC discovery default)", () => {
    const q = parseQuery("befreed");
    expect(q.mode).toBe("hashtag");
    expect(q.key).toBe("#befreed");
    expect(q.value).toBe("befreed");
  });

  test("#tag → hashtag mode", () => {
    const q = parseQuery("#befreed");
    expect(q.mode).toBe("hashtag");
    expect(q.key).toBe("#befreed");
    expect(q.value).toBe("befreed");
  });

  test("override to user mode", () => {
    const q = parseQuery("befreed", "user");
    expect(q.mode).toBe("user");
    expect(q.key).toBe("@befreed");
    expect(q.value).toBe("befreed");
  });

  test("override to hashtag mode strips @", () => {
    const q = parseQuery("@befreed", "hashtag");
    expect(q.mode).toBe("hashtag");
    expect(q.key).toBe("#befreed");
    expect(q.value).toBe("befreed");
  });

  test("whitespace is trimmed", () => {
    const q = parseQuery("  @befreed  ");
    expect(q.value).toBe("befreed");
  });

  // ─── keyword / niche discovery mode (competitor-UGC coverage fix) ───

  test("override to keyword mode keeps the full phrase, kw: key", () => {
    const q = parseQuery("skincare routine", "keyword");
    expect(q.mode).toBe("keyword");
    expect(q.key).toBe("kw:skincare routine");
    expect(q.value).toBe("skincare routine"); // spaces preserved — it's a search phrase
  });

  test("keyword mode strips a leading # but keeps the rest verbatim", () => {
    const q = parseQuery("#skincare tips", "keyword");
    expect(q.mode).toBe("keyword");
    expect(q.value).toBe("skincare tips");
  });

  test("plain multi-word still defaults to hashtag (keyword is opt-in)", () => {
    // Behavior preserved: keyword discovery must be explicitly requested via
    // --mode keyword so existing brand-hashtag scripts don't silently change.
    const q = parseQuery("skincare routine");
    expect(q.mode).toBe("hashtag");
  });
});

describe("isHashtagMatch (precision filter for hashtag results)", () => {
  // Real BEFREED data captured during E2E testing. Comments mark which we want
  // to keep (true positives) vs reject (false positives where TikTok's hashtag
  // endpoint over-matched).
  const TRUE_POSITIVES = [
    "Start getting comfortable with small talk☕️ #befreed_0111 #growth #microlearning", // campaign code
    "You can be a genius too! #befreed #geniuslevel #neuroscience #befreed_0128", // explicit hashtag
    "my top 5 tips to master flirting #flirting #befreed #learning", // explicit hashtag
    "Dark Psychology Tricks #BeFreed taught me as a personal coach", // case-insensitive
    "If you want to be disgustingly educated? DO THIS🫣 #befreed_0117 #growth", // campaign code
    "like are you kidding??? #learning #booktok #befreed_0067", // campaign code
    // Regression (signal #5): plain-text brand mention, no # or @. These were
    // the DROPPED top performers — 776K & 360K views — the bug this fix closes.
    "Learning with befreed bc not everyone has time to read 300 pages", // 776K, was dropped
    "Use these to level up bro, reading with befreed is so clutch", // 360K, was dropped
    "The BeFreed app has taken over my whole life 🤷🏻‍♀️📚 #booktok", // plain-text, case-insensitive
    "Bruh befreed is so addictive #reading #learning", // plain-text token
  ];

  const FALSE_POSITIVES = [
    "speaking my truth #lookamess #chopped", // unrelated, no befreed tag
    "🎵: Time to be free - Kodak Black (I'm happy…). #lyrics #song", // "be free" phrase, no tag
    "", // missing caption — unverifiable
    "Our sister will be freed", // "be freed" phrase — no "befreed" token
    "я надеюсь это гениально #befree #одежда #мода", // #befree (Russian clothing brand) ≠ befreed
    "#FREEDOM || I GOT AWAYYYYY #relatable #inspiration", // #freedom ≠ befreed
    "Horse Breeding Life – Power, Passion, and the Beauty", // pure noise the raw feed matched
    "Just #befreedish learning vibes", // boundary: befreedish ≠ befreed
  ];

  test.each(TRUE_POSITIVES)("keeps real UGC: %s", (caption) => {
    expect(isHashtagMatch(caption, "befreed")).toBe(true);
  });

  test.each(FALSE_POSITIVES)("rejects noise: %s", (caption) => {
    expect(isHashtagMatch(caption, "befreed")).toBe(false);
  });

  test("hashtag boundary: #befreedish is NOT a match for befreed", () => {
    expect(isHashtagMatch("Just #befreedish learning vibes", "befreed")).toBe(false);
  });

  test("@brand mention also counts", () => {
    expect(isHashtagMatch("This app @befreed changed my life", "befreed")).toBe(true);
  });

  test("strips @ and # from query before matching", () => {
    expect(isHashtagMatch("#befreed is great", "@befreed")).toBe(true);
    expect(isHashtagMatch("#befreed is great", "#befreed")).toBe(true);
  });

  test("#brandapp variant — keeps real UGC that uses the app-suffix tag", () => {
    // Real false-negative we caught in production: @apluslisa's 53.8K-view
    // post used #BeFreedApp instead of #befreed. Was being dropped before fix.
    expect(
      isHashtagMatch(
        "The easiest way to get more knowledge 🤭 #BeFreedApp #studytok #booktok",
        "befreed",
      ),
    ).toBe(true);
  });

  test("#brandapp boundary — #brandapp2 is NOT a match (same boundary discipline)", () => {
    expect(isHashtagMatch("Look at this #befreedapp2 thing", "befreed")).toBe(false);
  });

  test("@brandfoo is NOT a mention match (boundary applies to mentions too)", () => {
    expect(isHashtagMatch("Talking about @befreedom", "befreed")).toBe(false);
  });
});

describe("readCachedVideos trailing-window filter", () => {
  // The DB accumulates every video ever fetched for a competitor, so a prior
  // `--days 365` run leaves year-old rows behind. A later `--days 30` query must
  // NOT resurface those stale rows from cache (the bug: a 31-day-old clip
  // appearing in a "last 30 days" view). And it must NOT collapse to one row per
  // creator — top videos rank by views, multiple per creator allowed.
  function seed(): { db: Database; competitorId: number } {
    const db = new Database(":memory:");
    migrate(db);
    const competitorId = (
      db
        .prepare(`INSERT INTO competitors (handle, platform) VALUES ('#befreed','tiktok') RETURNING id`)
        .get() as { id: number }
    ).id;
    const ins = db.prepare(
      `INSERT INTO videos (competitor_id, platform, external_id, posted_at, view_count, author_handle)
       VALUES (?, 'tiktok', ?, ?, ?, ?)`,
    );
    const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
    // mya: 3 videos — one 31 days old (outside 30d), two inside
    ins.run(competitorId, "mya-31d", daysAgo(31), 2_600_000, "growthwithmya7");
    ins.run(competitorId, "mya-10d", daysAgo(10), 500_000, "growthwithmya7");
    ins.run(competitorId, "mya-5d", daysAgo(5), 70_000, "growthwithmya7");
    // jacob: 2 videos, both inside 30d
    ins.run(competitorId, "jacob-8d", daysAgo(8), 790_000, "jacob.befreed");
    ins.run(competitorId, "jacob-3d", daysAgo(3), 215_000, "jacob.befreed");
    return { db, competitorId };
  }

  test("30-day window excludes the 31-day-old video", () => {
    const { db, competitorId } = seed();
    const rows = readCachedVideos(db, competitorId, "tiktok", 30);
    const ids = rows.map((r) => r.external_id);
    expect(ids).not.toContain("mya-31d"); // 31d old → excluded
    expect(ids).toContain("mya-10d");
    expect(ids).toContain("jacob-8d");
    expect(rows.length).toBe(4); // 5 seeded, 1 outside the window
  });

  test("no window (undefined) returns everything, including the 31-day video", () => {
    const { db, competitorId } = seed();
    const rows = readCachedVideos(db, competitorId, "tiktok");
    expect(rows.length).toBe(5);
    expect(rows.map((r) => r.external_id)).toContain("mya-31d");
  });

  test("ranking keeps MULTIPLE videos per creator (no one-per-creator collapse)", () => {
    const { db, competitorId } = seed();
    const rows = readCachedVideos(db, competitorId, "tiktok", 30);
    // sort by views desc the way runSearch does
    rows.sort((a, b) => b.view_count - a.view_count);
    const myaRows = rows.filter((r) => r.author_handle === "growthwithmya7");
    const jacobRows = rows.filter((r) => r.author_handle === "jacob.befreed");
    expect(myaRows.length).toBe(2); // both in-window mya videos present
    expect(jacobRows.length).toBe(2); // both jacob videos present
    // top of the in-window ranking is jacob's 790k (mya's 2.6M is out of window)
    expect(rows[0]!.external_id).toBe("jacob-8d");
  });

  test("365-day window keeps the 31-day video and it ranks #1 by views", () => {
    const { db, competitorId } = seed();
    const rows = readCachedVideos(db, competitorId, "tiktok", 365);
    rows.sort((a, b) => b.view_count - a.view_count);
    expect(rows[0]!.external_id).toBe("mya-31d"); // 2.6M tops the year view
    expect(rows.length).toBe(5);
  });

  test("window compare handles legacy '+00:00'-offset posted_at rows", () => {
    const { db, competitorId } = seed();
    // Legacy row written before UTC normalization: 10 days ago, offset form.
    const tenDaysAgo = new Date(Date.now() - 10 * 86_400_000)
      .toISOString()
      .replace("Z", "+00:00");
    db.prepare(
      `INSERT INTO videos (competitor_id, platform, external_id, posted_at, view_count)
       VALUES (?, 'tiktok', 'legacy-offset', ?, 1)`,
    ).run(competitorId, tenDaysAgo);
    const rows = readCachedVideos(db, competitorId, "tiktok", 30);
    expect(rows.map((r) => r.external_id)).toContain("legacy-offset");
  });
});

describe("normalizeSearchOptions (CLI raw options → SearchOptions)", () => {
  const base = { limit: 20, sort: "views", days: 30 };

  test("legacy 'engagement' sort aliases to 'views' (existing scripts contract)", () => {
    expect(normalizeSearchOptions({ ...base, sort: "engagement" }).sort).toBe("views");
  });

  test("'recency' sort passes through", () => {
    expect(normalizeSearchOptions({ ...base, sort: "recency" }).sort).toBe("recency");
  });

  test("mode whitelist: valid modes pass, anything else → undefined (auto-detect)", () => {
    expect(normalizeSearchOptions({ ...base, mode: "user" }).mode).toBe("user");
    expect(normalizeSearchOptions({ ...base, mode: "hashtag" }).mode).toBe("hashtag");
    expect(normalizeSearchOptions({ ...base, mode: "keyword" }).mode).toBe("keyword");
    expect(normalizeSearchOptions({ ...base, mode: "bogus" }).mode).toBeUndefined();
    expect(normalizeSearchOptions({ ...base }).mode).toBeUndefined();
  });

  test("json/refresh coerce to booleans; limit/days pass through", () => {
    const o = normalizeSearchOptions({ ...base, json: undefined, refresh: 1 });
    expect(o.json).toBe(false);
    expect(o.refresh).toBe(true);
    expect(o.limit).toBe(20);
    expect(o.days).toBe(30);
  });
});

function raw(over: Partial<RawVideo> = {}): RawVideo {
  return {
    platform: "tiktok",
    external_id: "x1",
    posted_at: "2026-06-01T00:00:00.000Z",
    caption: "loved this!! #befreed",
    thumbnail_url: "https://t/x1.jpg",
    video_url: "https://www.tiktok.com/@creator1/video/1",
    view_count: 100,
    like_count: 10,
    comment_count: 5,
    share_count: 1,
    author_handle: "creator1",
    ...over,
  };
}

describe("applyHashtagPrecision (mode-aware brand filter wiring)", () => {
  const videos: RawVideo[] = [
    raw({ external_id: "match", caption: "reading with befreed is so clutch" }),
    raw({ external_id: "noise", caption: "our sister will be freed" }),
    raw({ external_id: "own-account", caption: "#befreed from us!", author_handle: "befreed" }),
  ];

  test("hashtag mode drops non-matching captions AND the brand's own account", () => {
    const out = applyHashtagPrecision(videos, "hashtag", "befreed");
    expect(out.map((v) => v.external_id)).toEqual(["match"]);
  });

  test("keyword mode returns input unchanged (captions carry no brand tag by design)", () => {
    expect(applyHashtagPrecision(videos, "keyword", "befreed")).toEqual(videos);
  });

  test("user mode returns input unchanged (it's the brand's own catalog)", () => {
    expect(applyHashtagPrecision(videos, "user", "befreed")).toEqual(videos);
  });
});

describe("fetchByMode (provider capability errors)", () => {
  const stub: DataProvider = {
    name: "stub",
    fetchRecentVideos: async () => [raw()],
  };

  test("keyword mode on a provider without fetchKeywordVideos throws actionably", async () => {
    await expect(
      fetchByMode(stub, { mode: "keyword", value: "skincare routine" }, "tiktok", 30),
    ).rejects.toThrow(/does not support keyword/);
  });

  test("hashtag mode on a provider without fetchHashtagVideos throws actionably", async () => {
    await expect(
      fetchByMode(stub, { mode: "hashtag", value: "befreed" }, "tiktok", 30),
    ).rejects.toThrow(/does not support hashtag/);
  });

  test("user mode dispatches to fetchRecentVideos", async () => {
    const out = await fetchByMode(stub, { mode: "user", value: "befreed" }, "tiktok", 30);
    expect(out).toHaveLength(1);
  });
});

describe("upsertVideos refresh semantics (shared search/daemon helper)", () => {
  function freshCompetitor(): { db: Database; competitorId: number } {
    const db = new Database(":memory:");
    migrate(db);
    const competitorId = (
      db
        .prepare(`INSERT INTO competitors (handle, platform) VALUES ('#befreed','tiktok') RETURNING id`)
        .get() as { id: number }
    ).id;
    return { db, competitorId };
  }

  function getRow(db: Database, externalId: string) {
    return db
      .prepare(`SELECT * FROM videos WHERE external_id = ?`)
      .get(externalId) as Record<string, unknown>;
  }

  test("refresh persists an upgraded caption AND recomputes hook_text from it", () => {
    // Regression: ON CONFLICT omitted caption, so the truncation-rescue's
    // full caption was never persisted for cached rows — while hook_text WAS
    // recomputed from the fresh caption, leaving the two out of sync.
    const { db, competitorId } = freshCompetitor();
    upsertVideos(db, competitorId, [raw({ caption: "purple colours with #befree" })]);
    upsertVideos(db, competitorId, [
      raw({ caption: "purple colours with #befreed full rescued caption" }),
    ]);
    const row = getRow(db, "x1");
    expect(row.caption).toBe("purple colours with #befreed full rescued caption");
    expect(row.hook_text).toBe("purple colours with #befreed full rescued caption");
  });

  test("a blank caption never clobbers a known caption (keep-on-throttle)", () => {
    const { db, competitorId } = freshCompetitor();
    upsertVideos(db, competitorId, [raw({ caption: "real caption #befreed" })]);
    upsertVideos(db, competitorId, [raw({ caption: "" })]);
    const row = getRow(db, "x1");
    expect(row.caption).toBe("real caption #befreed");
    expect(row.hook_text).toBe("real caption #befreed");
  });

  test("a zero metric never overwrites a known nonzero metric (prefer_metrics)", () => {
    const { db, competitorId } = freshCompetitor();
    upsertVideos(db, competitorId, [raw({ view_count: 2_600_000, like_count: 9000 })]);
    upsertVideos(db, competitorId, [raw({ view_count: 0, like_count: 9500 })]);
    const row = getRow(db, "x1");
    expect(row.view_count).toBe(2_600_000); // throttled 0 ignored
    expect(row.like_count).toBe(9500); // real nonzero update applied
  });

  test("a NULL author_handle never clobbers a known author", () => {
    const { db, competitorId } = freshCompetitor();
    upsertVideos(db, competitorId, [raw({ author_handle: "growthwithmya7" })]);
    upsertVideos(db, competitorId, [raw({ author_handle: null })]);
    expect(getRow(db, "x1").author_handle).toBe("growthwithmya7");
  });

  test("video/thumbnail URLs refresh but blanks are ignored", () => {
    const { db, competitorId } = freshCompetitor();
    upsertVideos(db, competitorId, [raw()]);
    upsertVideos(db, competitorId, [
      raw({ video_url: "https://www.tiktok.com/@creator1/video/1?new", thumbnail_url: "" }),
    ]);
    const row = getRow(db, "x1");
    expect(row.video_url).toBe("https://www.tiktok.com/@creator1/video/1?new");
    expect(row.thumbnail_url).toBe("https://t/x1.jpg");
  });
});

describe("normalizePostedAt (UTC normalization at the persistence boundary)", () => {
  test("offset form converts to Z form", () => {
    expect(normalizePostedAt("2026-06-01T08:00:00+00:00")).toBe("2026-06-01T08:00:00.000Z");
    expect(normalizePostedAt("2026-06-01T08:00:00+02:00")).toBe("2026-06-01T06:00:00.000Z");
  });

  test("bare 'YYYY-MM-DD HH:MM:SS' is treated as UTC, not local", () => {
    expect(normalizePostedAt("2026-06-01 08:00:00")).toBe("2026-06-01T08:00:00.000Z");
  });

  test("Z form is preserved; garbage passes through untouched", () => {
    expect(normalizePostedAt("2026-06-01T08:00:00.000Z")).toBe("2026-06-01T08:00:00.000Z");
    expect(normalizePostedAt("not-a-date")).toBe("not-a-date");
    expect(normalizePostedAt("")).toBe("");
  });
});

describe("reconcileVideosWindow (--refresh drops in-window rows the provider no longer returns)", () => {
  function seedReconcile(): { db: Database; competitorId: number } {
    const db = new Database(":memory:");
    migrate(db);
    const competitorId = (
      db
        .prepare(`INSERT INTO competitors (handle, platform) VALUES ('#befreed','tiktok') RETURNING id`)
        .get() as { id: number }
    ).id;
    const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
    upsertVideos(db, competitorId, [
      raw({ external_id: "kept", posted_at: daysAgo(5) }),
      raw({ external_id: "stale-in-window", posted_at: daysAgo(10) }),
      raw({ external_id: "old-history", posted_at: daysAgo(45) }),
    ]);
    return { db, competitorId };
  }

  test("deletes in-window rows missing from the fresh set; older history survives", () => {
    const { db, competitorId } = seedReconcile();
    const removed = reconcileVideosWindow(db, competitorId, "tiktok", 30, ["kept"]);
    expect(removed).toBe(1);
    const ids = (db.prepare(`SELECT external_id FROM videos`).all() as { external_id: string }[])
      .map((r) => r.external_id)
      .sort();
    expect(ids).toEqual(["kept", "old-history"]);
  });

  test("an empty fresh set is a no-op (never wipe the window on a dud fetch)", () => {
    const { db, competitorId } = seedReconcile();
    expect(reconcileVideosWindow(db, competitorId, "tiktok", 30, [])).toBe(0);
    expect((db.prepare(`SELECT COUNT(*) n FROM videos`).get() as { n: number }).n).toBe(3);
  });

  test("invalid window is a no-op", () => {
    const { db, competitorId } = seedReconcile();
    expect(reconcileVideosWindow(db, competitorId, "tiktok", Number.NaN, ["kept"])).toBe(0);
  });
});
