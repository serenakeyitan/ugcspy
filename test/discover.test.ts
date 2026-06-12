import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { migrate } from "../src/db/schema.ts";
import { upsertVideos } from "../src/db/videos.ts";
import {
  cachedCorpusTags,
  mineBrandCandidates,
  topCreators,
} from "../src/commands/discover.ts";
import type { RawVideo } from "../src/types.ts";

function vid(n: number, caption: string, author = `creator${n}`, views = 1000): RawVideo {
  return {
    platform: "tiktok",
    // Stay under Number.MAX_SAFE_INTEGER — 8e18 + n collapses distinct ids
    // into one float and the upsert silently merges the fixtures.
    external_id: String(8_600_000_000_000 + n),
    posted_at: "2026-06-01T00:00:00.000Z",
    caption,
    thumbnail_url: "",
    video_url: `https://www.tiktok.com/@${author}/video/${n}`,
    view_count: views,
    like_count: 0,
    comment_count: 0,
    share_count: 0,
    author_handle: author,
  };
}

describe("mineBrandCandidates (structural brand signals, no word lists)", () => {
  test("campaign codes are the dominant signal and collapse into the base tag", () => {
    const out = mineBrandCandidates([
      vid(1, "love this #acmeco_0124 #hobbies", "a"),
      vid(2, "try it #acmeco_0007", "b"),
      vid(3, "#hobbies #selfimprovement", "c"),
      vid(4, "#hobbies again", "d"),
    ]);
    expect(out[0]!.tag).toBe("acmeco");
    expect(out[0]!.campaignCodes).toBe(2);
    // generic #hobbies has breadth but no brand signal — must rank below
    const hobbies = out.find((b) => b.tag === "hobbies")!;
    expect(out[0]!.score).toBeGreaterThan(hobbies.score);
  });

  test("#xapp variant folds into x as evidence", () => {
    const out = mineBrandCandidates([
      vid(1, "#zentaskapp is great", "a"),
      vid(2, "#zentask changed my life", "b"),
    ]);
    const z = out.find((b) => b.tag === "zentask")!;
    expect(z.appVariant).toBe(true);
    expect(z.videos).toBe(2);
  });

  test("an in-corpus account handle matching the tag is a brand signal", () => {
    const out = mineBrandCandidates([
      vid(1, "#pingoai lesson today", "pingoai.korean"),
      vid(2, "more #pingoai", "someoneelse"),
    ]);
    const p = out.find((b) => b.tag === "pingoai")!;
    expect(p.authorMatch).toBe(true);
  });

  test("single-author tags without any brand signal are dropped (personal taglines)", () => {
    const out = mineBrandCandidates([
      vid(1, "#mydailyvlog episode 9", "onecreator"),
      vid(2, "#mydailyvlog episode 10", "onecreator"),
    ]);
    expect(out.find((b) => b.tag === "mydailyvlog")).toBeUndefined();
  });

  test("background (trending-corpus) tags are crushed unless they carry campaign codes", () => {
    const corpus = [
      vid(1, "#fyp #acmeco_0124", "a"),
      vid(2, "#fyp #funny", "b"),
      vid(3, "#fyp #funny", "c"),
      vid(4, "#fyp", "d"),
    ];
    const background = new Set(["fyp", "funny"]);
    const out = mineBrandCandidates(corpus, { backgroundTags: background });
    const fyp = out.find((b) => b.tag === "fyp")!;
    const acme = out.find((b) => b.tag === "acmeco")!;
    expect(fyp.background).toBe(true);
    expect(acme.score).toBeGreaterThan(fyp.score);
  });

  test("malformed/empty captions never crash the miner", () => {
    const weird = [
      vid(1, "", "a"),
      vid(2, "no tags at all", "b"),
      { ...vid(3, "#ok #fine", "c"), caption: undefined as unknown as string },
    ];
    expect(() => mineBrandCandidates(weird)).not.toThrow();
  });
});

describe("topCreators", () => {
  test("requires recurrence and ranks by top video", () => {
    const out = topCreators([
      vid(1, "x", "recurring", 100),
      vid(2, "y", "recurring", 9000),
      vid(3, "z", "oneoff", 50000),
    ]);
    expect(out.length).toBe(1);
    expect(out[0]!.handle).toBe("recurring");
    expect(out[0]!.maxViews).toBe(9000);
  });
});

describe("cachedCorpusTags", () => {
  test("reads the tag set of a cached synthetic corpus by handle prefix", () => {
    const db = new Database(":memory:");
    db.exec("PRAGMA foreign_keys = ON;");
    migrate(db);
    db.prepare(`INSERT INTO competitors (handle, platform) VALUES ('trend:US', 'tiktok')`).run();
    upsertVideos(db, 1, [vid(1, "#fyp #worldcup heat", "a"), vid(2, "#funny clip", "b")]);
    const tags = cachedCorpusTags(db, "trend:");
    expect(tags.has("fyp")).toBe(true);
    expect(tags.has("funny")).toBe(true);
    expect(tags.has("nonexistent")).toBe(false);
    // unrelated prefix → empty set
    expect(cachedCorpusTags(db, "kw:").size).toBe(0);
  });
});
