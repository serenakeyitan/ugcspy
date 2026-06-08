import type { Platform, RawVideo } from "../types.ts";
import type { DataProvider } from "./types.ts";

// Deterministic mock: same handle → same videos. Lets the CLI run end-to-end without API keys.
export class MockProvider implements DataProvider {
  readonly name = "mock";

  async fetchHashtagVideos(tag: string, platform: Platform, days: number): Promise<RawVideo[]> {
    // Reuse the same generator but stamp a different author per row, so the
    // result looks like third-party UGC. Deterministic per (tag, platform).
    const baseline = await this.fetchRecentVideos(`#${tag}`, platform, days);
    const creators = ["growthwithmya7", "apluslisa", "yapswithalicia", "diegodayy", "ask_julia"];
    return baseline.map((v, i) => ({
      ...v,
      author_handle: creators[i % creators.length],
      video_url: `https://www.tiktok.com/@${creators[i % creators.length]}/video/${v.external_id}`,
    }));
  }

  async fetchKeywordVideos(
    keyword: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    // Niche/keyword discovery: third-party UGC matching a topic, with captions
    // that deliberately do NOT carry any brand hashtag (that's the corpus the
    // brand-hashtag filter used to drop). Deterministic per (keyword, platform).
    const baseline = await this.fetchRecentVideos(`kw:${keyword}`, platform, days);
    const creators = ["nichecreator1", "topicqueen", "viralskincare", "routinegirl", "dermtalks"];
    return baseline.map((v, i) => ({
      ...v,
      // Topic caption with NO brand tag/mention — pure niche content.
      caption: `${keyword} tips that actually work ✨ #fyp #${keyword.replace(/\s+/g, "")}`,
      author_handle: creators[i % creators.length],
      video_url: `https://www.tiktok.com/@${creators[i % creators.length]}/video/${v.external_id}`,
    }));
  }

  async fetchRecentVideos(handle: string, platform: Platform, days: number): Promise<RawVideo[]> {
    const seed = hashString(`${handle}:${platform}`);
    const count = Math.min(30, 8 + (seed % 23));
    const videos: RawVideo[] = [];
    // Anchor "now" to a fixed reference so output is deterministic per (handle, platform).
    // Real provider uses live time; this is dev-only.
    const now = Date.UTC(2026, 4, 8, 0, 0, 0); // 2026-05-08
    const dayMs = 86_400_000;
    const cleanHandle = handle.replace(/^@/, "");

    const captionPool = [
      `POV: you just tried ${cleanHandle} for the first time`,
      `My honest review of ${cleanHandle}`,
      `GRWM using only ${cleanHandle} products`,
      `Why everyone is talking about ${cleanHandle}`,
      `${cleanHandle} unboxing — is it worth the hype?`,
      `3 ways to use ${cleanHandle} that nobody tells you`,
      `Before & after ${cleanHandle}`,
      `Stitch with @creator: ${cleanHandle} edition`,
      `${cleanHandle} viral product test`,
      `If you're on the fence about ${cleanHandle}, watch this`,
    ];

    for (let i = 0; i < count; i++) {
      const offsetDays = (i * days) / count + ((seed + i) % 100) / 100;
      const postedAt = new Date(now - offsetDays * dayMs);
      const baseViews = 5_000 + ((seed + i * 7) % 95_000);
      // Inject a few breakouts so warmup + alert logic has signal in dev.
      const breakout = i === 2 || i === 11 ? 8 : 1;
      const views = baseViews * breakout;

      videos.push({
        platform,
        external_id: `${platform}-${cleanHandle}-${seed}-${i}`,
        posted_at: postedAt.toISOString(),
        caption: captionPool[(seed + i) % captionPool.length]!,
        thumbnail_url: `https://example.invalid/thumb/${cleanHandle}/${i}.jpg`,
        video_url:
          platform === "tiktok"
            ? `https://www.tiktok.com/@${cleanHandle}/video/${seed}${i}`
            : `https://www.instagram.com/reel/${cleanHandle}-${i}/`,
        view_count: views,
        like_count: Math.floor(views * 0.06),
        comment_count: Math.floor(views * 0.005),
        share_count: Math.floor(views * 0.012),
      });
    }

    return videos;
  }
}

function hashString(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
