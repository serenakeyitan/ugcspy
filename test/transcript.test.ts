import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { migrate } from "../src/db/schema.ts";
import {
  isTalking,
  MIN_TALKING_WORDS,
  saveTranscript,
  spokenHook,
  transcriptText,
  upsertVideos,
} from "../src/db/videos.ts";
import {
  classifyTranscriptTarget,
  collectTranscripts,
  docFromCache,
  externalIdFromUrl,
  MAX_WAVE_SIZE,
  hookFor,
  transcribeScanCap,
} from "../src/commands/transcript.ts";
import { parseTranscriptBatchOutput, parseTranscriptOutput } from "../src/providers/tiktok-oss.ts";
import type { RawVideo, TranscriptDoc, VideoRecord } from "../src/types.ts";

function speechDoc(overrides: Partial<TranscriptDoc> = {}): TranscriptDoc {
  return {
    language: "en",
    duration_sec: 30,
    segments: [
      { start: 0, end: 4, text: "Here is the spoken hook line.", kind: "speech" },
      { start: 4, end: 8, text: "And more narration after it.", kind: "speech" },
    ],
    audio_kind: "speech",
    lexical_word_count: 11,
    ...overrides,
  };
}

function musicDoc(): TranscriptDoc {
  return {
    language: null,
    duration_sec: 22,
    segments: [{ start: 0, end: 22, text: "", kind: "non_speech", no_speech_prob: 0.93 }],
    audio_kind: "music",
    lexical_word_count: 0,
  };
}

describe("classifyTranscriptTarget", () => {
  test("http(s) URL → url", () => {
    expect(classifyTranscriptTarget("https://www.tiktok.com/@x/video/76450798753589")).toEqual({
      kind: "url",
      url: "https://www.tiktok.com/@x/video/76450798753589",
    });
  });
  test("short digits → DB id", () => {
    expect(classifyTranscriptTarget("42")).toEqual({ kind: "dbid", id: 42 });
  });
  test("19-digit TikTok id → external", () => {
    expect(classifyTranscriptTarget("7645079875358919966")).toEqual({
      kind: "external",
      externalId: "7645079875358919966",
    });
  });
  test("brand word / #tag / @handle → query", () => {
    expect(classifyTranscriptTarget("befreed").kind).toBe("query");
    expect(classifyTranscriptTarget("#befreed").kind).toBe("query");
    expect(classifyTranscriptTarget("@jacob.befreed").kind).toBe("query");
  });
});

describe("isTalking", () => {
  test("speech with enough words is talking", () => {
    expect(isTalking({ audio_kind: "speech", lexical_word_count: MIN_TALKING_WORDS })).toBe(true);
  });
  test("word boundary: one below the minimum is not talking", () => {
    expect(isTalking({ audio_kind: "speech", lexical_word_count: MIN_TALKING_WORDS - 1 })).toBe(
      false,
    );
  });
  test("music is never talking even with hallucinated word count", () => {
    // Upstream blanks music-bed text, but defend the classifier anyway.
    expect(isTalking({ audio_kind: "music", lexical_word_count: 50 })).toBe(false);
  });
  test("mixed with real narration is talking", () => {
    expect(isTalking({ audio_kind: "mixed", lexical_word_count: 40 })).toBe(true);
  });
});

describe("spokenHook / transcriptText", () => {
  test("hook is the FIRST speech segment, skipping music and filler", () => {
    const doc = speechDoc({
      segments: [
        { start: 0, end: 2, text: "", kind: "non_speech", no_speech_prob: 0.9 },
        { start: 2, end: 3, text: "Mmm", kind: "non_lexical" },
        { start: 3, end: 6, text: "The actual first line.", kind: "speech" },
        { start: 6, end: 9, text: "Second line.", kind: "speech" },
      ],
    });
    expect(spokenHook(doc)).toBe("The actual first line.");
  });
  test("hook caps at 160 chars", () => {
    const long = "word ".repeat(60).trim();
    const doc = speechDoc({ segments: [{ start: 0, end: 5, text: long, kind: "speech" }] });
    expect(spokenHook(doc).length).toBe(160);
  });
  test("music-only doc has no hook and empty text", () => {
    expect(spokenHook(musicDoc())).toBe("");
    expect(transcriptText(musicDoc())).toBe("");
  });
  test("transcriptText joins speech + non-lexical, drops blanked music segments", () => {
    const doc = speechDoc({
      segments: [
        { start: 0, end: 2, text: "Hello there.", kind: "speech" },
        { start: 2, end: 4, text: "", kind: "non_speech", no_speech_prob: 0.95 },
        { start: 4, end: 5, text: "(sighs)", kind: "non_lexical" },
        { start: 5, end: 8, text: "Back to talking.", kind: "speech" },
      ],
    });
    expect(transcriptText(doc)).toBe("Hello there. (sighs) Back to talking.");
  });
});

describe("transcribeScanCap", () => {
  test("no filter: exactly top", () => {
    expect(transcribeScanCap(3, false)).toBe(3);
  });
  test("filtering: 4x top with a floor of 12", () => {
    expect(transcribeScanCap(3, true)).toBe(12);
    expect(transcribeScanCap(5, true)).toBe(20);
  });
});

function makeVideo(id: number, overrides: Partial<VideoRecord> = {}): VideoRecord {
  return {
    id,
    competitor_id: 1,
    platform: "tiktok",
    external_id: String(7_000_000_000_000_000_000 + id),
    posted_at: "2026-06-01T00:00:00.000Z",
    caption: `caption ${id} #brand`,
    thumbnail_url: "",
    video_url: `https://www.tiktok.com/@c${id}/video/${id}`,
    view_count: 1000 - id,
    like_count: 0,
    comment_count: 0,
    share_count: 0,
    fetched_at: "",
    hook_source: "caption",
    hook_text: `caption ${id}`,
    hook_confidence: 1,
    format_tag: null,
    raw_metrics_json: "{}",
    author_handle: `c${id}`,
    ...overrides,
  };
}

describe("collectTranscripts (wave batching — one model load per wave)", () => {
  // Batch dep helper: serves docs by url, records every batch call.
  function batcher(docs: Record<string, TranscriptDoc | { error: string }>) {
    const calls: string[][] = [];
    const transcribeBatch = async (urls: string[]) => {
      calls.push(urls);
      return urls.map((u) => docs[u] ?? speechDoc());
    };
    return { calls, transcribeBatch };
  }

  test("unfiltered: ONE batch call covering exactly the top videos in rank order", async () => {
    const b = batcher({});
    const { entries, scanned } = await collectTranscripts(
      [makeVideo(1), makeVideo(2), makeVideo(3)],
      { top: 2 },
      { transcribeBatch: b.transcribeBatch, save: () => {} },
    );
    expect(entries.length).toBe(2);
    expect(scanned).toBe(2);
    expect(b.calls).toEqual([[makeVideo(1).video_url, makeVideo(2).video_url]]);
  });

  test("cache hit never reaches the batch and is marked fromCache", async () => {
    const cached = makeVideo(1, {
      transcript: "cached words here",
      transcript_kind: "speech",
      transcript_words: 20,
      transcript_duration_sec: 30,
      transcribed_at: "2026-06-10 00:00:00",
    });
    const b = batcher({});
    const { entries } = await collectTranscripts([cached], { top: 1 }, {
      transcribeBatch: b.transcribeBatch,
      save: () => {},
    });
    expect(b.calls.length).toBe(0);
    expect(entries[0]!.fromCache).toBe(true);
    expect(entries[0]!.talking).toBe(true);
  });

  test("mixed cached/uncached: waves stop at cached rows, order is preserved", async () => {
    const cached2 = makeVideo(2, {
      transcript: "cached",
      transcript_kind: "speech",
      transcript_words: 20,
      transcript_duration_sec: 10,
      transcribed_at: "2026-06-10 00:00:00",
    });
    const b = batcher({});
    const { entries } = await collectTranscripts(
      [makeVideo(1), cached2, makeVideo(3)],
      { top: 3 },
      { transcribeBatch: b.transcribeBatch, save: () => {} },
    );
    expect(entries.map((e) => e.video.id)).toEqual([1, 2, 3]);
    // Two waves: [1] (stopped at cached row 2), then [3].
    expect(b.calls).toEqual([[makeVideo(1).video_url], [makeVideo(3).video_url]]);
  });

  test("--talking filter: a wave transcribes ahead, extras are saved as cache", async () => {
    const saved: number[] = [];
    const b = batcher({ [makeVideo(1).video_url]: musicDoc() });
    const { entries, scanned } = await collectTranscripts(
      [makeVideo(1), makeVideo(2), makeVideo(3)],
      { top: 1, talking: true },
      {
        transcribeBatch: b.transcribeBatch,
        save: (v) => saved.push(v.id),
      },
    );
    expect(entries.length).toBe(1);
    expect(entries[0]!.video.id).toBe(2);
    // One wave of all 3 (≤ FILTER_WAVE_SIZE): overshoot past the match is
    // SAVED (cache for next time), and every transcription counts as scanned.
    expect(b.calls.length).toBe(1);
    expect(scanned).toBe(3);
    expect(saved).toEqual([1, 2, 3]);
  });

  test("scan cap bounds the waves even when nothing matches", async () => {
    const videos = Array.from({ length: 40 }, (_, i) => makeVideo(i + 1));
    const b = batcher({}); // everything talks → non-talking never matches
    const { entries, scanned } = await collectTranscripts(videos, { top: 1, nonTalking: true }, {
      transcribeBatch: b.transcribeBatch,
      save: () => {},
    });
    expect(entries.length).toBe(0);
    expect(scanned).toBe(transcribeScanCap(1, true));
    // cap 12 / wave size 6 = exactly 2 waves
    expect(b.calls.length).toBe(2);
    expect(b.calls.flat().length).toBe(12);
  });

  test("a per-item {error} is recorded and the rest of the wave survives", async () => {
    const b = batcher({ [makeVideo(1).video_url]: { error: "yt-dlp died" } });
    const { entries, failures } = await collectTranscripts(
      [makeVideo(1), makeVideo(2)],
      { top: 2 },
      { transcribeBatch: b.transcribeBatch, save: () => {} },
    );
    expect(entries.length).toBe(1);
    expect(entries[0]!.video.id).toBe(2);
    expect(failures.length).toBe(1);
    expect(failures[0]).toContain("yt-dlp died");
  });

  test("a no-url row consumes a scan slot — the cap can't be overrun", async () => {
    // Regression: no-url rows incremented `scanned` without shrinking the
    // wave budget, so an unfiltered --top 1 whose #1 lacked a URL silently
    // transcribed #2 — scanning deeper than the user asked.
    const noUrl = makeVideo(1, { video_url: "" });
    const b = batcher({});
    const { entries, scanned, failures } = await collectTranscripts(
      [noUrl, makeVideo(2)],
      { top: 1 },
      { transcribeBatch: b.transcribeBatch, save: () => {} },
    );
    expect(scanned).toBe(transcribeScanCap(1, false)); // exactly the cap
    expect(entries.length).toBe(0);
    expect(failures.length).toBe(1);
    expect(b.calls.length).toBe(0); // #2 was never transcribed
  });

  test("unfiltered waves are capped at MAX_WAVE_SIZE so one bridge call can't blow the deadline", async () => {
    const videos = Array.from({ length: 20 }, (_, i) => makeVideo(i + 1));
    const b = batcher({});
    const { entries } = await collectTranscripts(videos, { top: 20 }, {
      transcribeBatch: b.transcribeBatch,
      save: () => {},
    });
    expect(entries.length).toBe(20);
    expect(Math.max(...b.calls.map((c) => c.length))).toBeLessThanOrEqual(MAX_WAVE_SIZE);
    expect(b.calls.length).toBe(Math.ceil(20 / MAX_WAVE_SIZE));
  });

  test("a batch-LEVEL throw (no whisper) fails the wave and stops scanning", async () => {
    const { entries, failures } = await collectTranscripts(
      [makeVideo(1), makeVideo(2), makeVideo(3)],
      { top: 3 },
      {
        transcribeBatch: async () => {
          throw new Error("no transcript support");
        },
        save: () => {},
      },
    );
    expect(entries.length).toBe(0);
    expect(failures.length).toBe(3); // whole wave marked, then stop — no retry loop
    expect(failures[0]).toContain("no transcript support");
  });
});

describe("parseTranscriptBatchOutput", () => {
  const okDoc = JSON.parse(JSON.stringify(speechDoc({ video_url: "https://t/v/1" })));

  test("aligned array of docs and error envelopes", () => {
    const out = parseTranscriptBatchOutput(
      0,
      JSON.stringify([okDoc, { error: "Video unavailable" }]),
      "",
      2,
    );
    expect(out.length).toBe(2);
    expect((out[0] as TranscriptDoc).audio_kind).toBe("speech");
    expect((out[1] as { error: string }).error).toBe("Video unavailable");
  });

  test("all-failed batch (exit 1) still parses the aligned array", () => {
    const out = parseTranscriptBatchOutput(
      1,
      JSON.stringify([{ error: "a" }, { error: "b" }]),
      "",
      2,
    );
    expect(out.every((r) => "error" in r)).toBe(true);
  });

  test("count mismatch is rejected (alignment is the contract)", () => {
    expect(() => parseTranscriptBatchOutput(0, JSON.stringify([okDoc]), "", 2)).toThrow(
      /1 results for 2 urls/,
    );
  });

  test("top-level failure (no whisper) surfaces the error envelope", () => {
    expect(() =>
      parseTranscriptBatchOutput(1, JSON.stringify({ error: "whisper not installed" }), "", 2),
    ).toThrow(/whisper not installed/);
  });

  test("a malformed doc inside the array degrades to a per-item error, not a crash", () => {
    const out = parseTranscriptBatchOutput(
      0,
      JSON.stringify([{ audio_kind: "podcast" }, okDoc]),
      "",
      2,
    );
    expect("error" in out[0]!).toBe(true);
    expect((out[1] as TranscriptDoc).audio_kind).toBe("speech");
  });
});

describe("docFromCache", () => {
  test("round-trips the classification fields", () => {
    const video = makeVideo(1, {
      transcript: "some words",
      transcript_kind: "mixed",
      transcript_lang: "en",
      transcript_words: 25,
      transcript_duration_sec: 41.5,
      transcribed_at: "2026-06-10 00:00:00",
    });
    const doc = docFromCache(video)!;
    expect(doc.audio_kind).toBe("mixed");
    expect(doc.lexical_word_count).toBe(25);
    expect(doc.duration_sec).toBe(41.5);
    expect(transcriptText(doc)).toBe("some words");
  });
  test("untranscribed or invalid kind → null (forces a real transcription)", () => {
    expect(docFromCache(makeVideo(1))).toBeNull();
    expect(
      docFromCache(
        makeVideo(1, { transcript_kind: "garbage" as never, transcribed_at: "2026-06-10" }),
      ),
    ).toBeNull();
  });
});

describe("hookFor", () => {
  test("spoken hook wins over the caption hook", () => {
    const hook = hookFor(makeVideo(1), speechDoc());
    expect(hook.source).toBe("spoken");
    expect(hook.text).toBe("Here is the spoken hook line.");
  });
  test("music video falls back to the row's caption hook", () => {
    const hook = hookFor(makeVideo(1), musicDoc());
    expect(hook.source).toBe("caption");
    expect(hook.text).toBe("caption 1");
  });
  test("no hook anywhere → none", () => {
    const bare = makeVideo(1, { hook_text: "", caption: "" });
    expect(hookFor(bare, musicDoc()).source).toBe("none");
  });
});

describe("parseTranscriptOutput", () => {
  const valid = JSON.stringify(speechDoc({ video_url: "https://t/v/1" }));

  test("valid doc parses with all fields", () => {
    const doc = parseTranscriptOutput(0, valid, "");
    expect(doc.audio_kind).toBe("speech");
    expect(doc.lexical_word_count).toBe(11);
    expect(doc.segments.length).toBe(2);
    expect(doc.video_url).toBe("https://t/v/1");
  });
  test("nonzero exit surfaces the bridge's JSON error envelope", () => {
    expect(() =>
      parseTranscriptOutput(1, JSON.stringify({ error: "whisper not installed" }), ""),
    ).toThrow(/whisper not installed/);
  });
  test("non-JSON stdout fails loudly", () => {
    expect(() => parseTranscriptOutput(0, "Traceback ...", "")).toThrow(/non-JSON/);
  });
  test("array output (search-mode shape) is rejected", () => {
    expect(() => parseTranscriptOutput(0, "[]", "")).toThrow(/non-object/);
  });
  test("invalid audio_kind is rejected", () => {
    const bad = JSON.stringify({ ...speechDoc(), audio_kind: "podcast" });
    expect(() => parseTranscriptOutput(0, bad, "")).toThrow(/invalid audio_kind/);
  });
  test("garbage segment entries are filtered, not crashed on", () => {
    const messy = JSON.stringify({
      ...speechDoc(),
      segments: [null, 42, { text: "ok", kind: "speech", start: 0, end: 1 }],
    });
    expect(parseTranscriptOutput(0, messy, "").segments.length).toBe(1);
  });
});

describe("saveTranscript (real schema, in-memory db)", () => {
  function freshDb(): Database {
    const db = new Database(":memory:");
    db.exec("PRAGMA foreign_keys = ON;");
    migrate(db);
    db.prepare(`INSERT INTO competitors (handle, platform) VALUES ('#brand', 'tiktok')`).run();
    return db;
  }
  const RAW: RawVideo = {
    platform: "tiktok",
    external_id: "7000000000000000001",
    posted_at: "2026-06-01T00:00:00.000Z",
    caption: "the caption #brand",
    thumbnail_url: "",
    video_url: "https://www.tiktok.com/@c/video/1",
    view_count: 10,
    like_count: 0,
    comment_count: 0,
    share_count: 0,
    author_handle: "c",
  };
  function insertOne(db: Database): number {
    upsertVideos(db, 1, [RAW]);
    return (db.prepare(`SELECT id FROM videos LIMIT 1`).get() as { id: number }).id;
  }

  test("persists transcript columns and upgrades the hook to whisper", () => {
    const db = freshDb();
    const id = insertOne(db);
    saveTranscript(db, RAW, speechDoc());
    const row = db.prepare(`SELECT * FROM videos WHERE id = ?`).get(id) as VideoRecord;
    expect(row.transcript).toContain("spoken hook line");
    expect(row.transcript_kind).toBe("speech");
    expect(row.transcript_words).toBe(11);
    expect(row.transcribed_at).toBeTruthy();
    expect(row.hook_source).toBe("whisper");
    expect(row.hook_text).toBe("Here is the spoken hook line.");
    expect(row.hook_confidence).toBe(0.9);
  });

  test("music doc caches the classification but leaves the caption hook alone", () => {
    const db = freshDb();
    const id = insertOne(db);
    saveTranscript(db, RAW, musicDoc());
    const row = db.prepare(`SELECT * FROM videos WHERE id = ?`).get(id) as VideoRecord;
    expect(row.transcript_kind).toBe("music");
    expect(row.transcript).toBe("");
    expect(row.hook_source).toBe("caption"); // untouched
  });

  test("propagates the cache to EVERY competitor's copy of the same video", () => {
    // The same TikTok video legitimately exists under multiple competitors
    // (#brand search + @creator pull). One transcription must fill all copies
    // or the one-shot cache promise breaks across queries.
    const db = freshDb();
    db.prepare(`INSERT INTO competitors (handle, platform) VALUES ('@c', 'tiktok')`).run();
    upsertVideos(db, 1, [RAW]);
    upsertVideos(db, 2, [RAW]);
    saveTranscript(db, RAW, speechDoc());
    const rows = db
      .prepare(`SELECT transcript_kind, hook_source FROM videos WHERE external_id = ?`)
      .all(RAW.external_id) as Array<{ transcript_kind: string; hook_source: string }>;
    expect(rows.length).toBe(2);
    for (const r of rows) {
      expect(r.transcript_kind).toBe("speech");
      expect(r.hook_source).toBe("whisper");
    }
  });

  test("a later search refresh does NOT clobber the whisper hook", () => {
    const db = freshDb();
    const id = insertOne(db);
    saveTranscript(db, RAW, speechDoc());
    // Simulate a refresh: same video comes back with a (new) non-empty caption.
    upsertVideos(db, 1, [{ ...RAW, caption: "refreshed caption #brand", view_count: 99 }]);
    const row = db.prepare(`SELECT * FROM videos WHERE id = ?`).get(id) as VideoRecord;
    expect(row.view_count).toBe(99); // metrics refreshed
    expect(row.caption).toBe("refreshed caption #brand"); // caption refreshed
    expect(row.hook_source).toBe("whisper"); // spoken hook survived
    expect(row.hook_text).toBe("Here is the spoken hook line.");
  });
});

describe("externalIdFromUrl (share-link cache matching)", () => {
  test("canonical, query-param, and mobile URLs all yield the id", () => {
    const id = "7645079875358919966";
    expect(externalIdFromUrl(`https://www.tiktok.com/@x/video/${id}`)).toBe(id);
    expect(externalIdFromUrl(`https://www.tiktok.com/@x/video/${id}?is_from_webapp=1&q=share`)).toBe(id);
    expect(externalIdFromUrl(`https://m.tiktok.com/v/whatever/video/${id}/`)).toBe(id);
  });
  test("short-links without /video/<id> yield null (fall back to exact match)", () => {
    expect(externalIdFromUrl("https://vm.tiktok.com/ZMabc123/")).toBeNull();
  });
});

describe("cached music rows must not promote non-lexical cues to hooks", () => {
  test("docFromCache tags music-row text as non_lexical, so the hook falls back to caption", () => {
    // transcriptText keeps "(sighs)"-style cues for display, so a cached music
    // row CAN carry text. On the first run hookFor skipped it (kind
    // non_lexical); the cached doc must preserve that, not re-tag it speech.
    const cachedMusic = makeVideo(1, {
      transcript: "(sighs)",
      transcript_kind: "music",
      transcript_words: 0,
      transcript_duration_sec: 20,
      transcribed_at: "2026-06-10 00:00:00",
    });
    const doc = docFromCache(cachedMusic)!;
    expect(doc.segments[0]!.kind).toBe("non_lexical");
    expect(transcriptText(doc)).toBe("(sighs)"); // display unchanged
    const hook = hookFor(cachedMusic, doc);
    expect(hook.source).toBe("caption"); // NOT "(sighs)" as a spoken hook
  });
});

describe("hookFor stability on cache hits", () => {
  test("a persisted whisper hook beats re-deriving from the flattened cached doc", () => {
    // Cache hits rebuild the doc as ONE segment containing the whole
    // transcript; without the stored-hook preference the 'hook' would be the
    // first 160 chars of everything — different from the original first line.
    const video = makeVideo(1, {
      hook_source: "whisper",
      hook_text: "The original first spoken line.",
    });
    const flattened = speechDoc({
      segments: [
        {
          start: 0,
          end: 30,
          text: "The original first spoken line. Plus every later sentence flattened together into one block.",
          kind: "speech",
        },
      ],
    });
    const hook = hookFor(video, flattened);
    expect(hook.text).toBe("The original first spoken line.");
    expect(hook.source).toBe("spoken");
  });
});
