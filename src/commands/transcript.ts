import chalk from "chalk";
import ora from "ora";
import { openDb } from "../db/index.ts";
import { isTalking, saveTranscript, spokenHook, transcriptText } from "../db/videos.ts";
import { loadConfig } from "../lib/config.ts";
import { getProvider } from "../providers/index.ts";
import { authorFromUrl } from "../providers/tiktok-oss.ts";
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

// Canonical TikTok video id from any pasted URL form (share links carry query
// params, m.tiktok.com hosts, trailing junk — but all keep /video/<id>).
export function externalIdFromUrl(url: string): string | null {
  const m = url.match(/\/video\/(\d+)/);
  return m ? m[1]! : null;
}

// Display handle: the row's author when known, else parsed from the video URL
// (user-mode catalog rows can carry a NULL author_handle).
export function displayHandle(video: Pick<VideoRecord, "author_handle" | "video_url">): string {
  return video.author_handle || authorFromUrl(video.video_url) || "?";
}

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
// carries ONE synthetic segment when text exists. For a "music" row that text
// can only be non-lexical cues like "(sighs)" — tag it non_lexical so
// spokenHook never promotes a cue to a hook on cache hits. Classification
// fields (audio_kind, lexical_word_count) round-trip exactly.
export function docFromCache(video: VideoRecord): TranscriptDoc | null {
  if (!video.transcript_kind || !video.transcribed_at) return null;
  const kind = video.transcript_kind;
  if (kind !== "speech" && kind !== "music" && kind !== "mixed") return null;
  const text = video.transcript ?? "";
  return {
    language: video.transcript_lang ?? null,
    duration_sec: video.transcript_duration_sec ?? 0,
    segments: text
      ? [
          {
            start: 0,
            end: video.transcript_duration_sec ?? 0,
            text,
            kind: kind === "music" ? "non_lexical" : "speech",
          },
        ]
      : [],
    audio_kind: kind,
    lexical_word_count: video.transcript_words ?? 0,
    video_url: video.video_url,
  };
}

export interface CollectDeps {
  // Batch transcription: one bridge spawn + one whisper model load per call.
  transcribeBatch: (videoUrls: string[]) => Promise<Array<TranscriptDoc | { error: string }>>;
  save: (video: VideoRecord, doc: TranscriptDoc) => void;
  // Progress line for the user; null clears.
  progress?: (text: string | null) => void;
}

// When filtering, we don't know how many transcriptions yield a match, so
// uncached candidates go to the bridge in waves of this size: big enough to
// amortize the ~3-5s model load, small enough that an early match doesn't
// over-transcribe far past it (overshoot is cached, not wasted).
export const FILTER_WAVE_SIZE = 6;

// Hard wave ceiling for ALL runs: the bridge emits its result array only when
// every video in the call finishes, and it lives under one fixed deadline
// (UGCSPY_BRIDGE_TIMEOUT_MS, default 30min). At the documented 10-40s/video an
// unbounded `--top 100` wave could blow the deadline and lose ALL completed
// work; 8 × 40s worst-case stays comfortably inside it.
export const MAX_WAVE_SIZE = 8;

// Walk the ranked candidates, transcribing (or reading cache) until `top`
// entries match the filter or the scan cap is hit. Uncached candidates are
// batched into single bridge calls — the whisper model loads ONCE per wave
// instead of once per video. Returns the matched entries plus how many
// candidates were scanned — callers surface the scan count so a capped run
// never silently reads as "covered everything".
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
  let i = 0;

  const classify = (video: VideoRecord, doc: TranscriptDoc, fromCache: boolean) => {
    const talking = isTalking(doc);
    if (opts.talking && !talking) return;
    if (opts.nonTalking && talking) return;
    if (entries.length < opts.top) entries.push({ video, doc, talking, fromCache });
  };

  while (entries.length < opts.top && scanned < cap && i < candidates.length) {
    const video = candidates[i]!;
    const cachedDoc = docFromCache(video);
    if (cachedDoc) {
      i += 1;
      scanned += 1;
      classify(video, cachedDoc, true);
      continue;
    }

    // Collect the next contiguous run of uncached candidates into one wave.
    // The guard re-checks the cap LIVE: no-url rows consume scan slots during
    // collection, and the wave itself must fit the remaining budget — without
    // this, a no-url row let the loop drift past `cap` (scanning deeper than
    // the user asked, e.g. an unfiltered --top 1 silently inspecting #2).
    const budget = Math.min(
      filtering ? FILTER_WAVE_SIZE : opts.top - entries.length,
      MAX_WAVE_SIZE,
    );
    const wave: VideoRecord[] = [];
    while (
      i < candidates.length &&
      wave.length < budget &&
      scanned + wave.length < cap
    ) {
      const v = candidates[i]!;
      if (docFromCache(v)) break; // cached row — handle on the next loop pass
      i += 1;
      if (!v.video_url) {
        scanned += 1;
        failures.push(`@${displayHandle(v)} ${v.external_id}: no video_url`);
        continue;
      }
      wave.push(v);
    }
    if (wave.length === 0) continue;

    deps.progress?.(
      wave.length === 1
        ? `Transcribing @${displayHandle(wave[0]!)} (${wave[0]!.view_count.toLocaleString()} views)...`
        : `Transcribing ${wave.length} videos (one model load)...`,
    );
    let results: Array<TranscriptDoc | { error: string }>;
    try {
      results = await deps.transcribeBatch(wave.map((v) => v.video_url));
    } catch (err) {
      // Batch-level failure (no whisper, bridge timeout) — it would repeat on
      // every subsequent wave, so record it for the whole wave and stop.
      deps.progress?.(null);
      for (const v of wave) {
        scanned += 1;
        failures.push(`@${displayHandle(v)} ${v.external_id}: ${(err as Error).message}`);
      }
      break;
    }
    deps.progress?.(null);
    for (let k = 0; k < wave.length; k += 1) {
      const v = wave[k]!;
      const r = results[k];
      scanned += 1;
      if (!r || "error" in r) {
        failures.push(`@${displayHandle(v)} ${v.external_id}: ${r ? r.error : "no result"}`);
        continue;
      }
      deps.save(v, r); // save even past `top` — overshoot becomes cache, not waste
      classify(v, r, false);
    }
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
  // No up-front provider gate: cached transcripts must stay readable even when
  // the configured search provider can't transcribe (e.g. the user switched to
  // scrapecreators after building the cache with tiktok-oss). The check fires
  // lazily, only when a cache miss actually needs a transcription.
  const transcribeBatch = async (
    urls: string[],
  ): Promise<Array<TranscriptDoc | { error: string }>> => {
    if (provider.fetchTranscriptBatch) return provider.fetchTranscriptBatch(urls);
    if (provider.fetchTranscript) {
      // Per-url fallback for providers without a batch path — slower (one
      // model load each) but correct.
      const out: Array<TranscriptDoc | { error: string }> = [];
      for (const url of urls) {
        try {
          out.push(await provider.fetchTranscript(url));
        } catch (err) {
          out.push({ error: (err as Error).message });
        }
      }
      return out;
    }
    throw new Error(
      `provider '${provider.name}' has no transcript support — switch to the tiktok-oss provider (free) to transcribe new videos`,
    );
  };

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
    // Pasted URLs vary (share query params, m.tiktok.com, trailing slash) but
    // all canonical forms carry /video/<id> — match the cache by external_id
    // first so a known video isn't re-transcribed as "ad-hoc" just because the
    // share link didn't string-match the stored canonical URL.
    const externalId = externalIdFromUrl(target.url);
    const row = (externalId
      ? db
          .prepare(`SELECT * FROM videos WHERE external_id = ? ORDER BY id LIMIT 1`)
          .get(externalId)
      : db
          .prepare(`SELECT * FROM videos WHERE video_url = ? ORDER BY id LIMIT 1`)
          .get(target.url)) as VideoRecord | undefined;
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
      transcribeBatch,
      save: (video, doc) => {
        // Ad-hoc URLs (id <= 0) have no row to cache into. Persisting is keyed
        // by (platform, external_id) so every competitor's copy of the video
        // shares the one-shot transcript.
        if (video.id > 0) saveTranscript(db, video, doc);
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
      `\n${chalk.bold(`#${i + 1}`)} ${chalk.cyan(`@${displayHandle(video)}`)} — ${views}${badge} ${chalk.dim(`(${meta})`)}`,
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
//
// The PERSISTED whisper hook wins over re-deriving from the doc: cache hits
// rebuild the doc as ONE flattened segment (segment boundaries aren't
// persisted), so spokenHook() on a cached doc would return the first 160
// chars of the whole transcript — a different "hook" on every cached run.
// The stored value is the original first spoken segment; it's stable.
export function hookFor(
  video: Pick<VideoRecord, "hook_text" | "hook_source" | "caption">,
  doc: Pick<TranscriptDoc, "segments">,
): { text: string; source: string } {
  if (video.hook_source === "whisper" && video.hook_text) {
    return { text: video.hook_text, source: "spoken" };
  }
  const spoken = spokenHook(doc);
  if (spoken) return { text: spoken, source: "spoken" };
  const caption = (video.caption ?? "").trim();
  if (video.hook_text) return { text: video.hook_text, source: video.hook_source };
  if (caption) return { text: caption.slice(0, 120), source: "caption" };
  return { text: "", source: "none" };
}
