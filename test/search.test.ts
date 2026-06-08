import { describe, expect, test } from "bun:test";
import { isHashtagMatch, parseQuery } from "../src/commands/search.ts";

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
