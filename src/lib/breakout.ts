import type { VideoRecord } from "../types.ts";

export type WatchState = "warming_up" | "active";

const WARMUP_DAYS = 7;
const MIN_VIDEOS = 5;

export interface WatchStatus {
  state: WatchState;
  videos_in_window: number;
  days_since_added: number;
  reason?: string;
}

// Cold-start gate: alerts stay warming_up until 7 days have elapsed since the watch was created
// AND at least N=5 videos exist in the trailing 30-day window.
export function evaluateWatchState(
  watchCreatedAt: string,
  videosInTrailingWindow: number,
  now: Date = new Date(),
): WatchStatus {
  const created = new Date(watchCreatedAt).getTime();
  const ageMs = now.getTime() - created;
  const days_since_added = ageMs / 86_400_000;

  if (days_since_added < WARMUP_DAYS) {
    return {
      state: "warming_up",
      videos_in_window: videosInTrailingWindow,
      days_since_added,
      reason: `${WARMUP_DAYS - Math.floor(days_since_added)}d remaining in warmup`,
    };
  }
  if (videosInTrailingWindow < MIN_VIDEOS) {
    return {
      state: "warming_up",
      videos_in_window: videosInTrailingWindow,
      days_since_added,
      reason: `${videosInTrailingWindow}/${MIN_VIDEOS} videos collected`,
    };
  }
  return { state: "active", videos_in_window: videosInTrailingWindow, days_since_added };
}

// Median view-count over the trailing-30-day window. Used as the breakout baseline.
// Cap at 30 days; require at least MIN_VIDEOS samples or return null.
export function trailingMedianViews(videos: Pick<VideoRecord, "view_count">[]): number | null {
  if (videos.length < MIN_VIDEOS) return null;
  const sorted = [...videos].map((v) => v.view_count).sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return (sorted[mid - 1]! + sorted[mid]!) / 2;
  }
  return sorted[mid]!;
}

export interface BreakoutCandidate {
  video: VideoRecord;
  ratio: number;
  threshold: number;
}

// A video breaks out if its view_count exceeds threshold_multiplier × trailing median.
// Spec: "views-at-24h vs trailing-30-day-median views-at-24h."
// Real implementation should snapshot views at +24h after posting; mock data uses raw views.
export function detectBreakouts(
  recentVideos: VideoRecord[],
  baselineVideos: Pick<VideoRecord, "view_count">[],
  thresholdMultiplier: number,
): BreakoutCandidate[] {
  const median = trailingMedianViews(baselineVideos);
  if (median === null || median === 0) return [];
  const threshold = median * thresholdMultiplier;
  return recentVideos
    .filter((v) => v.view_count >= threshold)
    .map((video) => ({ video, ratio: video.view_count / median, threshold }));
}
