import { describe, expect, test } from "bun:test";
import { ProviderError } from "../src/providers/types.ts";
import {
  InstagramOssProvider,
  parseIgJson,
  parseIgVideosResponse,
} from "../src/providers/instagram-oss.ts";

describe("instagram-oss provider identity + platform guard", () => {
  test("name is instagram-oss", () => {
    expect(new InstagramOssProvider().name).toBe("instagram-oss");
  });

  test("rejects a non-instagram platform loudly (never silent-empty)", async () => {
    const p = new InstagramOssProvider();
    await expect(p.fetchRecentVideos("@x", "tiktok", 30)).rejects.toThrow(/only supports instagram/);
  });
});

describe("parseIgJson", () => {
  test("parses a JSON object", () => {
    expect(parseIgJson('{"logged_in":true}', "")).toEqual({ logged_in: true });
  });

  test("empty stdout → clear error (surfaces stderr)", () => {
    expect(() => parseIgJson("", "Traceback: boom")).toThrow(/no output.*boom/s);
  });

  test("non-JSON stdout → clear error", () => {
    expect(() => parseIgJson("not json at all", "")).toThrow(/not JSON/);
  });
});

describe("parseIgVideosResponse — the bridge {videos}/{error} contract", () => {
  test("returns the videos array on success", () => {
    const body = JSON.stringify({
      videos: [
        {
          platform: "instagram",
          external_id: "DZjR_dosb9x",
          posted_at: "2026-06-10T00:00:00.000Z",
          view_count: 12_700_000,
          like_count: 634_000,
          author_handle: "nike",
        },
      ],
      enriched_views: 1,
    });
    const out = parseIgVideosResponse(body, "");
    expect(out).toHaveLength(1);
    expect(out[0]!.external_id).toBe("DZjR_dosb9x");
    expect(out[0]!.view_count).toBe(12_700_000);
    expect(out[0]!.platform).toBe("instagram");
  });

  test("re_login_required error → actionable ProviderError with the cookie-browser hint", () => {
    const body = JSON.stringify({
      error: "No logged-in Instagram session in safari",
      code: "re_login_required",
    });
    try {
      parseIgVideosResponse(body, "");
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ProviderError);
      expect((e as Error).message).toContain("No logged-in Instagram session");
      expect((e as Error).message).toContain("UGCSPY_IG_COOKIE_BROWSER"); // the actionable hint
    }
  });

  test("a generic bridge error surfaces without the re-login hint", () => {
    const body = JSON.stringify({ error: "Instagram returned no posts", code: "empty_or_blocked" });
    expect(() => parseIgVideosResponse(body, "")).toThrow(/Instagram returned no posts/);
    expect(() => parseIgVideosResponse(body, "")).not.toThrow(/UGCSPY_IG_COOKIE_BROWSER/);
  });

  test("missing videos array → clear error (no silent empty)", () => {
    expect(() => parseIgVideosResponse('{"unexpected":1}', "")).toThrow(/no videos array/);
  });

  // codex P2: malformed rows must be dropped, not blindly cast — a null row,
  // wrong-platform row, or missing-field row would otherwise crash ingestion
  // (null caption rolls back the upsert) or persist bad cross-platform data.
  const DATE = "2026-06-10T00:00:00.000Z";

  test("drops null / wrong-platform / no-external-id rows; keeps + coerces valid ones", () => {
    const body = JSON.stringify({
      videos: [
        null, // JSON null — must not TypeError
        { platform: "tiktok", external_id: "x", posted_at: DATE }, // wrong platform — drop
        { platform: "instagram", posted_at: DATE }, // no external_id — drop
        { platform: "instagram", external_id: "OK1", posted_at: DATE }, // valid but partial — keep+coerce
        {
          platform: "instagram",
          external_id: "OK2",
          posted_at: DATE,
          caption: "hi",
          view_count: 100,
          like_count: 5,
        },
      ],
    });
    const out = parseIgVideosResponse(body, "");
    expect(out.map((v) => v.external_id)).toEqual(["OK1", "OK2"]);
    // partial row coerced to safe defaults (no null caption that crashes the DB)
    expect(out[0]!.caption).toBe("");
    expect(out[0]!.view_count).toBe(0);
    expect(out[0]!.platform).toBe("instagram");
    // full row preserved
    expect(out[1]!.view_count).toBe(100);
    expect(out[1]!.caption).toBe("hi");
  });

  // codex P2 (round 2): a row with NO real date (the bridge stamps missing dates
  // as the epoch) must be DROPPED, not persisted — otherwise on a re-fetch the
  // upsert overwrites a previously-good date with epoch, yanking the video out of
  // relative-breakout windows.
  test("drops rows with a missing or epoch posted_at (no date-corruption on upsert)", () => {
    const body = JSON.stringify({
      videos: [
        { platform: "instagram", external_id: "NODATE" }, // missing posted_at — drop
        // The PYTHON bridge stamps a missing date as the epoch in OFFSET form,
        // NOT JS's ".000Z" form — the parser must drop BOTH (codex P2 round 3).
        { platform: "instagram", external_id: "EPOCHZ", posted_at: new Date(0).toISOString() },
        { platform: "instagram", external_id: "EPOCHOFF", posted_at: "1970-01-01T00:00:00+00:00" },
        { platform: "instagram", external_id: "EMPTY", posted_at: "" }, // empty — drop
        { platform: "instagram", external_id: "GARBAGE", posted_at: "not-a-date" }, // unparseable — drop
        { platform: "instagram", external_id: "GOOD", posted_at: DATE }, // real date — keep
      ],
    });
    const out = parseIgVideosResponse(body, "");
    expect(out.map((v) => v.external_id)).toEqual(["GOOD"]);
  });

  test("a videos array of entirely malformed rows yields an empty array, not a throw", () => {
    const body = JSON.stringify({ videos: [null, { platform: "tiktok" }, 42, "nope"] });
    expect(parseIgVideosResponse(body, "")).toEqual([]);
  });
});
