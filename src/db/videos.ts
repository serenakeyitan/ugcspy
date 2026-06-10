import type { Database } from "bun:sqlite";
import type { Platform, RawVideo, TranscriptDoc } from "../types.ts";

// Hook = first sentence-ish chunk of the caption, capped at 120 chars.
// Free, deterministic, no API key. Shared by search + daemon — these were two
// drifted copies before (the daemon's dropped author_handle on insert).
export function captionHook(caption: string): { text: string; source: string } {
  const trimmed = caption.trim();
  if (!trimmed) return { text: "", source: "none" };
  const match = trimmed.match(/^[^.!?\n]{1,120}/);
  const text = match ? match[0]!.trim() : trimmed.slice(0, 120);
  return { text, source: "caption" };
}

// Normalize provider timestamps to UTC ISO-8601 Z at the persistence boundary.
// Providers emit a mix of ISO-with-offset ("+00:00"), bare "YYYY-MM-DD HH:MM:SS",
// and Z forms; SQLite compares TEXT lexicographically, so mixed forms break
// window cutoffs. Bare "date time" strings are treated as UTC (matching
// SQLite's own datetime('now')), NOT host-local time. Unparseable values pass
// through untouched rather than becoming "Invalid Date".
export function normalizePostedAt(s: string): string {
  if (!s) return s;
  const iso = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s) ? `${s.replace(" ", "T")}Z` : s;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return s;
  return new Date(t).toISOString();
}

// Single upsert used by BOTH `search` and `daemon`.
//
// Refresh semantics on conflict:
//   - caption/urls/hook DO refresh — but never clobber a known-good value with
//     a blank one. The Python bridge's keep-on-throttle path stores truncated
//     captions expecting "a later refresh re-fetches"; before this, the
//     ON CONFLICT clause silently dropped the rescued caption forever (and
//     hook_text was recomputed from the NEW caption, so the two disagreed).
//   - metrics follow the bridge's prefer_metrics semantics: a fresh 0 never
//     overwrites a known nonzero count (throttled fetches report zeros).
//   - author_handle: a NULL never overwrites a known author.
export function upsertVideos(db: Database, competitorId: number, videos: RawVideo[]): void {
  const stmt = db.prepare(`
    INSERT INTO videos (
      competitor_id, platform, external_id, posted_at, caption, thumbnail_url, video_url,
      view_count, like_count, comment_count, share_count,
      hook_source, hook_text, hook_confidence, format_tag, author_handle, raw_metrics_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(competitor_id, platform, external_id) DO UPDATE SET
      posted_at = excluded.posted_at,
      caption = CASE WHEN excluded.caption <> '' THEN excluded.caption ELSE caption END,
      video_url = CASE WHEN excluded.video_url <> '' THEN excluded.video_url ELSE video_url END,
      thumbnail_url = CASE WHEN excluded.thumbnail_url <> '' THEN excluded.thumbnail_url ELSE thumbnail_url END,
      view_count = CASE WHEN excluded.view_count = 0 AND view_count > 0 THEN view_count ELSE excluded.view_count END,
      like_count = CASE WHEN excluded.like_count = 0 AND like_count > 0 THEN like_count ELSE excluded.like_count END,
      comment_count = CASE WHEN excluded.comment_count = 0 AND comment_count > 0 THEN comment_count ELSE excluded.comment_count END,
      share_count = CASE WHEN excluded.share_count = 0 AND share_count > 0 THEN share_count ELSE excluded.share_count END,
      fetched_at = datetime('now'),
      hook_text = CASE WHEN excluded.caption <> '' THEN excluded.hook_text ELSE hook_text END,
      hook_source = CASE WHEN excluded.caption <> '' THEN excluded.hook_source ELSE hook_source END,
      hook_confidence = CASE WHEN excluded.caption <> '' THEN excluded.hook_confidence ELSE hook_confidence END,
      author_handle = COALESCE(excluded.author_handle, author_handle)
  `);
  const tx = db.transaction((rows: RawVideo[]) => {
    for (const v of rows) {
      const hook = captionHook(v.caption);
      stmt.run(
        competitorId,
        v.platform,
        v.external_id,
        normalizePostedAt(v.posted_at),
        v.caption,
        v.thumbnail_url,
        v.video_url,
        v.view_count,
        v.like_count,
        v.comment_count,
        v.share_count,
        hook.source,
        hook.text,
        hook.text ? 1.0 : 0,
        null,
        v.author_handle ?? null,
        JSON.stringify({}),
      );
    }
  });
  tx(videos);
}

// --- Transcript cache + talking classification (ugcspy transcript) ----------

// A video counts as "talking" when the track has real speech (not just a
// music bed — Whisper's hallucinated lyrics are already blanked upstream) AND
// enough words to be narration rather than a stray "let's go". 8 words ≈ one
// short spoken sentence; below that it's a sound-bite over a montage.
export const MIN_TALKING_WORDS = 8;

export function isTalking(doc: Pick<TranscriptDoc, "audio_kind" | "lexical_word_count">): boolean {
  return doc.audio_kind !== "music" && doc.lexical_word_count >= MIN_TALKING_WORDS;
}

// Spoken hook = the first real speech of the video (the 3-second retention
// line), capped at 160 chars. Walks segments in order and skips music/filler.
export function spokenHook(doc: Pick<TranscriptDoc, "segments">): string {
  for (const seg of doc.segments) {
    if (seg.kind === "speech" && seg.text.trim()) {
      return seg.text.trim().slice(0, 160);
    }
  }
  return "";
}

// Render-ready transcript text: speech + non-lexical segments joined in order
// (blanked non-speech segments are dropped — they carry no text by design).
export function transcriptText(doc: Pick<TranscriptDoc, "segments">): string {
  return doc.segments
    .filter((s) => s.kind !== "non_speech" && s.text.trim())
    .map((s) => s.text.trim())
    .join(" ")
    .trim();
}

// Persist one video's transcript into its row. The spoken hook upgrades the
// caption-derived hook (a real first-line beats a caption guess) but a
// music-only doc leaves the existing hook columns untouched.
export function saveTranscript(db: Database, videoId: number, doc: TranscriptDoc): void {
  db.prepare(
    `UPDATE videos SET
       transcript = ?, transcript_kind = ?, transcript_lang = ?,
       transcript_words = ?, transcript_duration_sec = ?, transcribed_at = datetime('now')
     WHERE id = ?`,
  ).run(
    transcriptText(doc),
    doc.audio_kind,
    doc.language,
    doc.lexical_word_count,
    doc.duration_sec,
    videoId,
  );
  const hook = spokenHook(doc);
  if (hook) {
    db.prepare(
      `UPDATE videos SET hook_text = ?, hook_source = 'whisper', hook_confidence = 0.9 WHERE id = ?`,
    ).run(hook, videoId);
  }
}

// After a successful --refresh that returned >=1 video, drop in-window rows the
// provider no longer returns (deleted/private/now-filtered videos). Scoped to
// the refresh window so older history survives. The datetime() comparison keeps
// legacy offset-format posted_at rows comparing correctly. Returns the number
// of rows removed. (A single DELETE is atomic in SQLite.)
export function reconcileVideosWindow(
  db: Database,
  competitorId: number,
  platform: Platform,
  windowDays: number,
  freshExternalIds: string[],
): number {
  if (freshExternalIds.length === 0) return 0;
  if (!Number.isFinite(windowDays) || windowDays <= 0) return 0;
  const cutoff = new Date(Date.now() - windowDays * 86_400_000).toISOString();
  const placeholders = freshExternalIds.map(() => "?").join(", ");
  const result = db
    .prepare(
      `DELETE FROM videos
       WHERE competitor_id = ? AND platform = ?
         AND datetime(posted_at) >= datetime(?)
         AND external_id NOT IN (${placeholders})`,
    )
    .run(competitorId, platform, cutoff, ...freshExternalIds);
  return result.changes;
}
