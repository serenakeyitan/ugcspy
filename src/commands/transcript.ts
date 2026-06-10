import chalk from "chalk";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { isTalking, saveTranscript, spokenHook, transcriptText } from "../db/videos.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import type { Platform, TranscriptDoc, VideoRecord } from "../types.ts";
import { parseQuery, readCachedVideos } from "./search.ts";

export interface TranscriptOptions {
  top: number;
  talking?: boolean;
  nonTalking?: boolean;
  days: number;
  platform: Platform;
  json: boolean;
}

// What the positional argument refers to.
//   url      — a TikTok video URL (transcribed ad-hoc; cached when the video is in the DB)
//   dbid     — a videos.id from `search --json` output (DB autoincrement, short)
//   external — a TikTok external video id (19-ish digits)
//   query    — a brand/#tag/@handle; resolves to the cached search's top videos
export type TranscriptTarget =
  | { kind: "url"; url: string }
  | { kind: "dbid"; id: number }
  | { kind: "external"; externalId: string }
  | { kind: "query"; raw: string };

export function classifyTranscriptTarget(arg: string): TranscriptTarget {
  const trimmed = arg.trim();
  if (/^https?:\/\//i.test(trimmed)) return { kind: "url", url: trimmed };
  // TikTok external ids are 18-19 digits; DB autoincrement ids stay far below
  // 10 digits for a local tool. The gap (10-14 digits) defaults to external —
  // an out-of-range DB id would just be a miss anyway.
  if (/^\d{1,9}$/.test(trimmed)) return { kind: "dbid", id: Number.parseInt(trimmed, 10) };
  if (/^\d{10,}$/.test(trimmed)) return { kind: "external", externalId: trimmed };
  return { kind: "query", raw: trimmed };
}

// When filtering by talking/non-talking we can't know in advance how many
// videos must be transcribed to find N matches — scan down the ranked list,
// but bound the work: whisper costs ~10-40s per video.
export function transcribeScanCap(top: number, filtering: boolean): number {
  return filtering ? Math.max(top * 4, 12) : top;
}

export interface TranscriptEntry {
  video: VideoRecord;
  doc: TranscriptDoc;
  talking: boolean;
  fromCache: boolean;
}

// Rebuild a TranscriptDoc-shaped object from the DB cache columns. The cached
// transcript is the flattened text (segments aren't persisted), so the doc
// carries one synthetic speech segment when text exists. Classification fields
// (audio_kind, lexical_word_count) round-trip exactly.
export function docFromCache(video: VideoRecord): TranscriptDoc | null {
  if (!video.transcript_kind || !video.transcribed_at) return null;
  const kind = video.transcript_kind;
  if (kind !== "speech" && kind !== "music" && kind !== "mixed") return null;
  const text = video.transcript ?? "";
  return {
    language: video.transcript_lang ?? null,
    duration_sec: video.transcript_duration_sec ?? 0,
    segments: text ? [{ start: 0, end: video.transcript_duration_sec ?? 0, text, kind: "speech" }] : [],
    audio_kind: kind,
    lexical_word_count: video.transcript_words ?? 0,
    video_url: video.video_url,
  };
}

export interface CollectDeps {
  transcribe: (videoUrl: string) => Promise<TranscriptDoc>;
  save: (videoId: number, doc: TranscriptDoc) => void;
  // Called once per candidate as work starts/ends; null text clears.
  progress?: (text: string | null) => void;
}

// Walk the ranked candidates, transcribing (or reading cache) until `top`
// entries match the filter or the scan cap is hit. Returns the matched entries
// plus how many candidates were scanned — callers surface the scan count so a
// capped run never silently reads as "covered everything".
export async function collectTranscripts(
  candidates: VideoRecord[],
  opts: Pick<TranscriptOptions, "top" | "talking" | "nonTalking">,
  deps: CollectDeps,
): Promise<{ entries: TranscriptEntry[]; scanned: number; failures: string[] }> {
  const filtering = Boolean(opts.talking || opts.nonTalking);
  const cap = transcribeScanCap(opts.top, filtering);
  const entries: TranscriptEntry[] = [];
  const failures: string[] = [];
  let scanned = 0;

  for (const video of candidates) {
    if (entries.length >= opts.top || scanned >= cap) break;
    scanned += 1;
    let doc = docFromCache(video);
    const fromCache = doc !== null;
    if (!doc) {
      if (!video.video_url) {
        failures.push(`@${video.author_handle ?? "?"} ${video.external_id}: no video_url`);
        continue;
      }
      deps.progress?.(
        `Transcribing @${video.author_handle ?? "?"} (${video.view_count.toLocaleString()} views)...`,
      );
      try {
        doc = await deps.transcribe(video.video_url);
        deps.save(video.id, doc);
      } catch (err) {
        failures.push(`@${video.author_handle ?? "?"} ${video.external_id}: ${(err as Error).message}`);
        continue;
      } finally {
        deps.progress?.(null);
      }
    }
    const talking = isTalking(doc);
    if (opts.talking && !talking) continue;
    if (opts.nonTalking && talking) continue;
    entries.push({ video, doc, talking, fromCache });
  }
  return { entries, scanned, failures };
}

export async function runTranscript(arg: string, opts: TranscriptOptions): Promise<void> {
  if (opts.talking && opts.nonTalking) {
    console.error(chalk.red("--talking and --non-talking are mutually exclusive."));
    process.exit(1);
  }
  const db = openDb();
  const config = loadConfig();
  const provider = getProvider(config);
  if (!provider.fetchTranscript) {
    console.error(
      chalk.red(
        `Provider '${provider.name}' has no transcript support. Use the tiktok-oss provider (free) for transcripts.`,
      ),
    );
    process.exit(1);
  }

  const target = classifyTranscriptTarget(arg);
  let candidates: VideoRecord[];
  if (target.kind === "dbid") {
    const row = db.prepare(`SELECT * FROM videos WHERE id = ?`).get(target.id) as
      | VideoRecord
      | undefined;
    if (!row) {
      console.error(chalk.red(`No video with id ${target.id}. Ids come from \`search --json\`.`));
      process.exit(1);
    }
    candidates = [row];
  } else if (target.kind === "external") {
    const row = db
      .prepare(`SELECT * FROM videos WHERE external_id = ? ORDER BY id LIMIT 1`)
      .get(target.externalId) as VideoRecord | undefined;
    if (!row) {
      console.error(chalk.red(`No cached video with TikTok id ${target.externalId}.`));
      process.exit(1);
    }
    candidates = [row];
  } else if (target.kind === "url") {
    const row = db
      .prepare(`SELECT * FROM videos WHERE video_url = ? ORDER BY id LIMIT 1`)
      .get(target.url) as VideoRecord | undefined;
    // An uncached URL still works — transcribe ad-hoc with a synthetic record;
    // there's just no row to persist the cache into.
    candidates = row
      ? [row]
      : [
          {
            id: -1,
            competitor_id: -1,
            platform: opts.platform,
            external_id: target.url,
            posted_at: "",
            caption: "",
            thumbnail_url: "",
            video_url: target.url,
            view_count: 0,
            like_count: 0,
            comment_count: 0,
            share_count: 0,
            fetched_at: "",
            hook_source: "none",
            hook_text: "",
            hook_confidence: 0,
            format_tag: null,
            raw_metrics_json: "{}",
            author_handle: null,
          },
        ];
  } else {
    const query = parseQuery(target.raw);
    const competitor = db
      .prepare(`SELECT id FROM competitors WHERE handle = ? AND platform = ?`)
      .get(query.key, opts.platform) as { id: number } | undefined;
    if (!competitor) {
      console.error(
        chalk.red(
          `No cached results for ${query.key} on ${opts.platform}. Run \`ugcspy search ${target.raw}\` first.`,
        ),
      );
      process.exit(1);
    }
    candidates = readCachedVideos(db, competitor.id, opts.platform, opts.days).sort(
      (a, b) => b.view_count - a.view_count,
    );
    if (candidates.length === 0) {
      console.error(
        chalk.red(`No cached videos for ${query.key} in the last ${opts.days} days.`),
      );
      process.exit(1);
    }
  }

  const spinner = opts.json ? null : ora();
  const { entries, scanned, failures } = await collectTranscripts(
    candidates,
    { top: target.kind === "query" ? opts.top : candidates.length, ...filterOf(opts) },
    {
      transcribe: (url) => provider.fetchTranscript!(url),
      save: (videoId, doc) => {
        if (videoId > 0) saveTranscript(db, videoId, doc);
      },
      progress: spinner
        ? (text) => {
            if (text) spinner.start(text);
            else spinner.stop();
          }
        : undefined,
    },
  );
  spinner?.stop();

  if (opts.json) {
    const out = entries.map(({ video, doc, talking, fromCache }) => ({
      id: video.id > 0 ? video.id : null,
      external_id: video.external_id,
      author_handle: video.author_handle,
      view_count: video.view_count,
      video_url: video.video_url,
      talking,
      audio_kind: doc.audio_kind,
      lexical_word_count: doc.lexical_word_count,
      duration_sec: doc.duration_sec,
      language: doc.language,
      hook: hookFor(video, doc),
      transcript: transcriptText(doc),
      from_cache: fromCache,
    }));
    console.log(JSON.stringify(out, null, 2));
    for (const f of failures) console.error(`transcript failed: ${f}`);
    if (failures.length > 0 && entries.length === 0) process.exit(1);
    return;
  }

  if (entries.length === 0) {
    const filterLabel = opts.talking ? "talking " : opts.nonTalking ? "non-talking " : "";
    console.log(
      chalk.yellow(
        `No ${filterLabel}videos found (scanned ${scanned} candidate${scanned === 1 ? "" : "s"}).`,
      ),
    );
  }
  entries.forEach(({ video, doc, talking, fromCache }, i) => {
    const views = video.view_count > 0 ? `${video.view_count.toLocaleString()} views — ` : "";
    const badge = talking ? chalk.green("TALKING") : chalk.magenta("NON-TALKING");
    const meta = `${doc.audio_kind}, ${doc.lexical_word_count} words, ${Math.round(doc.duration_sec)}s${fromCache ? ", cached" : ""}`;
    console.log(
      `\n${chalk.bold(`#${i + 1}`)} ${chalk.cyan(`@${video.author_handle ?? "?"}`)} — ${views}${badge} ${chalk.dim(`(${meta})`)}`,
    );
    if (video.video_url) console.log(chalk.dim(video.video_url));
    const hook = hookFor(video, doc);
    if (hook.text) console.log(`${chalk.bold("Hook")} ${chalk.dim(`(${hook.source})`)}: ${hook.text}`);
    const text = transcriptText(doc);
    console.log(
      text
        ? `${chalk.bold("Transcript")}: ${text}`
        : chalk.dim("Transcript: (no speech — music/ambience only)"),
    );
  });
  const filtering = Boolean(opts.talking || opts.nonTalking);
  if (filtering && scanned > entries.length) {
    console.log(
      chalk.dim(
        `\nScanned ${scanned} of ${candidates.length} ranked candidates to find ${entries.length} match${entries.length === 1 ? "" : "es"} (cap: ${transcribeScanCap(opts.top, true)}).`,
      ),
    );
  }
  for (const f of failures) console.error(chalk.yellow(`transcript failed: ${f}`));
  if (failures.length > 0 && entries.length === 0) process.exit(1);
}

function filterOf(opts: TranscriptOptions): { talking?: boolean; nonTalking?: boolean } {
  return { talking: opts.talking, nonTalking: opts.nonTalking };
}

// Hook preference: the spoken first line when the video talks, else the
// caption-derived hook already on the row.
export function hookFor(
  video: Pick<VideoRecord, "hook_text" | "hook_source" | "caption">,
  doc: Pick<TranscriptDoc, "segments">,
): { text: string; source: string } {
  const spoken = spokenHook(doc);
  if (spoken) return { text: spoken, source: "spoken" };
  const caption = (video.caption ?? "").trim();
  if (video.hook_text) return { text: video.hook_text, source: video.hook_source };
  if (caption) return { text: caption.slice(0, 120), source: "caption" };
  return { text: "", source: "none" };
}
