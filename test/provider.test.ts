import { describe, expect, test } from "bun:test";
import { MockProvider } from "../src/providers/mock.ts";
import { isHashtagMatch } from "../src/commands/search.ts";

describe("MockProvider hashtag mode", () => {
  test("returns videos with author_handle set", async () => {
    const p = new MockProvider();
    const videos = await p.fetchHashtagVideos("befreed", "tiktok", 30);
    expect(videos.length).toBeGreaterThan(0);
    expect(videos[0]?.author_handle).toBeTruthy();
    expect(videos[0]?.author_handle).not.toContain("@"); // bare handle
  });

  test("authors vary across rows (third-party UGC, not single account)", async () => {
    const p = new MockProvider();
    const videos = await p.fetchHashtagVideos("befreed", "tiktok", 30);
    const uniqueAuthors = new Set(videos.map((v) => v.author_handle));
    expect(uniqueAuthors.size).toBeGreaterThan(1);
  });

  test("video_url uses the actual creator handle, not the brand", async () => {
    const p = new MockProvider();
    const videos = await p.fetchHashtagVideos("befreed", "tiktok", 30);
    for (const v of videos) {
      expect(v.video_url).toContain(`@${v.author_handle}/`);
    }
  });
});

describe("MockProvider", () => {
  test("is deterministic per (handle, platform)", async () => {
    const p = new MockProvider();
    const a = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const b = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    expect(a).toEqual(b);
  });

  test("different handles yield different data", async () => {
    const p = new MockProvider();
    const a = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const b = await p.fetchRecentVideos("@rarebeauty", "tiktok", 30);
    expect(a[0]?.external_id).not.toBe(b[0]?.external_id);
  });

  test("strips leading @ from handle in URL", async () => {
    const p = new MockProvider();
    const videos = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    expect(videos[0]?.video_url).toContain("@glossier/");
    expect(videos[0]?.external_id).not.toContain("@");
  });

  test("returns at least one breakout-shaped video for alert dev", async () => {
    const p = new MockProvider();
    const videos = await p.fetchRecentVideos("@glossier", "tiktok", 30);
    const max = Math.max(...videos.map((v) => v.view_count));
    const median = [...videos].sort((a, b) => a.view_count - b.view_count)[
      Math.floor(videos.length / 2)
    ]?.view_count ?? 0;
    expect(max).toBeGreaterThan(median * 2);
  });
});

describe("MockProvider keyword/niche mode (competitor-UGC coverage fix)", () => {
  test("returns multi-creator videos for a topic phrase", async () => {
    const p = new MockProvider();
    const videos = await p.fetchKeywordVideos("skincare routine", "tiktok", 30);
    expect(videos.length).toBeGreaterThan(0);
    const authors = new Set(videos.map((v) => v.author_handle));
    expect(authors.size).toBeGreaterThan(1); // niche corpus, not one account
  });

  test("captions carry NO brand hashtag — the corpus the old filter dropped", () => {
    // The whole point of keyword mode: surface UGC that does NOT tag a brand.
    // Prove isHashtagMatch (the brand-tag filter) would REJECT these captions,
    // which is exactly why keyword mode must bypass that filter.
    const p = new MockProvider();
    return p.fetchKeywordVideos("skincare routine", "tiktok", 30).then((videos) => {
      for (const v of videos) {
        // A hypothetical brand "glossier" is never tagged in niche captions.
        expect(isHashtagMatch(v.caption, "glossier")).toBe(false);
      }
    });
  });

  test("is deterministic per (keyword, platform)", async () => {
    const p = new MockProvider();
    const a = await p.fetchKeywordVideos("skincare routine", "tiktok", 30);
    const b = await p.fetchKeywordVideos("skincare routine", "tiktok", 30);
    expect(a).toEqual(b);
  });
});
