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
});
