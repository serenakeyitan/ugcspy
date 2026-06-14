import { fileURLToPath } from "node:url";
import { platform } from "node:os";
import { dirname, resolve } from "node:path";
import type {
  Platform,
  RawVideo,
  SeedResult,
  SimilarCreator,
  SimilarResult,
  TranscriptDoc,
  TranscriptSegment,
} from "../types.ts";
import { type DataProvider, ProviderError } from "./types.ts";
import { venvExists, venvPython } from "../lib/venv.ts";

// Bridge to davidteather/TikTok-Api (Python). Free, OSS, actively maintained
// (v7.3.3 shipped April 2026). Instagram is intentionally NOT supported by this
// provider — no production-grade free IG scraper exists right now (see Open Q
// in DESIGN.md). Use ScrapeCreators for IG when you need it.
//
// Setup (one-time): `ugcspy install-deps` creates a managed venv at
// ~/.ugcspy/venv and installs TikTokApi + Chromium there. This provider
// invokes that venv's python so a system-Python upgrade can't silently
// invalidate the deps.
//
// Two modes via the Python bridge:
//   - user mode:    fetch a handle's own posts
//   - hashtag mode: fetch posts tagged with #X by ANY creator (third-party UGC)
export class TikTokOssProvider implements DataProvider {
  readonly name = "tiktok-oss";

  async fetchRecentVideos(
    handle: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok. For Instagram Reels use 'scrapecreators'.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "user", handle, days });
  }

  async fetchHashtagVideos(
    tag: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok. For Instagram Reels use 'scrapecreators'.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "hashtag", tag, days });
  }

  // Keyword / niche discovery. Served by the tikwm relay (free, no key) inside
  // the Python bridge — this is the broad-corpus path that the brand-hashtag
  // model structurally cannot reach. The bridge uses stdlib urllib (no
  // TikTokApi/Chromium session needed for this mode), but still runs in the
  // managed venv for one code path + the shared RawVideo contract.
  async fetchKeywordVideos(
    keyword: string,
    platform: Platform,
    days: number,
  ): Promise<RawVideo[]> {
    if (platform !== "tiktok") {
      throw new ProviderError(
        `Provider 'tiktok-oss' only supports tiktok keyword search.`,
        this.name,
      );
    }
    return this.runBridge({ mode: "keyword", keyword, days });
  }

  // Network-wide trending feed for a region (the 蹭热度 discovery lane) — the
  // relay's rotating feed, deduped bridge-side. Pure HTTP, no venv extras.
  async fetchTrendingVideos(region: string, days: number): Promise<RawVideo[]> {
    return this.runBridge({ mode: "trending", region, days });
  }

  // Follow-graph similarity (the creator-centric "find more like these" pass):
  // walk who the seed creators FOLLOW and rank the result by how many seeds
  // follow each candidate. Pure HTTP, no venv. NOTE: tikwm's /user/following is
  // private/blocked for a large fraction of creators (~60% in measurement), so
  // a thin or empty result is normal, not an error — the caller pairs this with
  // corpus style-matching and reports the graph hit-rate.
  async fetchSimilarCreators(seeds: string[]): Promise<SimilarResult> {
    const raw = await this.spawnBridge({ mode: "snowball", seeds });
    return parseSimilarOutput(raw.exit, raw.stdout, raw.stderr);
  }

  // Transcribe one video's audio via the bridge's transcript mode (whisper +
  // yt-dlp in the managed venv). Same deadline contract as the search modes.
  async fetchTranscript(videoUrl: string): Promise<TranscriptDoc> {
    const raw = await this.spawnBridge({ mode: "transcript", url: videoUrl });
    return parseTranscriptOutput(raw.exit, raw.stdout, raw.stderr);
  }

  // Batch transcription: ONE bridge spawn + ONE whisper model load for the
  // whole wave (the load costs ~3-5s; per-video spawns dominated wall time on
  // multi-video runs). Results align with the input order; a bad video yields
  // an {error} element instead of sinking the batch.
  async fetchTranscriptBatch(
    videoUrls: string[],
  ): Promise<Array<TranscriptDoc | { error: string }>> {
    const raw = await this.spawnBridge({ mode: "transcript", urls: videoUrls });
    return parseTranscriptBatchOutput(raw.exit, raw.stdout, raw.stderr, videoUrls.length);
  }

  private async runBridge(payload: Record<string, unknown>): Promise<RawVideo[]> {
    const raw = await this.spawnBridge(payload);
    return parseBridgeOutput(raw.exit, raw.stdout, raw.stderr);
  }

  private async spawnBridge(
    payload: Record<string, unknown>,
  ): Promise<{ exit: number; stdout: string; stderr: string }> {
    const pythonBin = resolveBridgePython(venvExists(), payload.mode);
    const scriptPath = resolveScript();
    const proc = Bun.spawn([pythonBin, scriptPath], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });

    proc.stdin.write(JSON.stringify(payload));
    await proc.stdin.end();

    // Overall deadline so a hung bridge (stuck network read, throttle loop)
    // can't wedge a search or a daemon tick forever. Tune via
    // UGCSPY_BRIDGE_TIMEOUT_MS; the default 30min leaves room for a full
    // Stage-2 creator walk on an active brand.
    const timeoutMs = bridgeTimeoutMs();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill();
    }, timeoutMs);

    const [stdout, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    const exit = await proc.exited;
    clearTimeout(timer);

    if (timedOut) {
      throw new ProviderError(
        `tiktok-oss: bridge timed out after ${timeoutMs}ms — raise UGCSPY_BRIDGE_TIMEOUT_MS if the walk legitimately needs longer.`,
        this.name,
      );
    }
    return { exit, stdout, stderr };
  }
}

// Decide which python runs the bridge. Keyword/niche discovery and the
// trending feed are pure HTTP (tikwm + stdlib urllib) — they need NO venv, so
// they can fall back to a system interpreter rather than forcing an install.
// user/hashtag modes still require
// the venv (yt-dlp walk; TikTokApi fallbacks). On Windows the system binary is
// `python` (python.org installs no `python3`, and the WindowsApps `python3.exe`
// is a Store stub) — match venv.ts / install-deps' platform handling.
// Exported for tests.
export function resolveBridgePython(hasVenv: boolean, mode: unknown): string {
  if (hasVenv) return venvPython();
  if (mode === "keyword" || mode === "trending" || mode === "snowball") {
    return platform() === "win32" ? "python" : "python3"; // stdlib-only paths; resolved on PATH
  }
  throw new ProviderError(
    `tiktok-oss venv not found at ${venvPython()}. Run \`ugcspy install-deps\` to set it up (one-time, ~30-60s; browser-free). ` +
      `(Tip: keyword/niche search — \`--mode keyword\` — works without the venv.)`,
    "tiktok-oss",
  );
}

// Bridge deadline in ms (default 30min). Invalid/absent env values fall back
// to the default. The whole value must be a positive integer — parseInt would
// accept the prefix of "30000ms" or "1h" (1h → 1ms, killing the bridge
// instantly on a typo). Exported for tests.
export function bridgeTimeoutMs(
  env: Record<string, string | undefined> = process.env,
): number {
  const raw = env.UGCSPY_BRIDGE_TIMEOUT_MS?.trim();
  const n = raw && /^\d+$/.test(raw) ? Number.parseInt(raw, 10) : NaN;
  return Number.isFinite(n) && n > 0 ? n : 1_800_000;
}

// Pure parse of the bridge's (exit, stdout, stderr) — exported for tests.
//
// Maps the Python bridge's `_author` field onto our typed `author_handle`.
// Fallback: when the bridge couldn't supply an author (e.g. the tikwm feed
// item had no author.unique_id), parse the handle out of the video_url —
// every TikTok URL is `https://www.tiktok.com/@<handle>/video/<id>`, so the
// author is ALREADY present in data we hold. This is free (no extra fetch /
// no /user/info lookup) and recovers the rows that previously rendered as
// "(unknown)". Prefer the explicit field; only derive from the URL when it's
// missing, so a real author is never overwritten by a URL parse.
export function parseBridgeOutput(exit: number, stdout: string, stderr: string): RawVideo[] {
  const name = "tiktok-oss";
  if (exit !== 0) {
    // NOTE: `||` (not `??`) for the stderr fallback — trim() always returns a
    // string, so a bridge that died with empty output (OOM-kill, SIGKILL)
    // must still produce a non-blank error naming the exit code.
    const errBody =
      parseErrorBody(stdout) ?? (stderr.trim() || `bridge exited ${exit} with no output`);
    throw new ProviderError(`tiktok-oss: ${errBody}`, name);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-JSON output: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  if (!Array.isArray(parsed)) {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-array: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  return (parsed as Array<RawVideo & { _author?: string }>).map((v) => {
    const out: RawVideo = { ...v };
    delete (out as { _author?: string })._author;
    // _author crosses an untrusted JSON boundary — a non-string (numeric id,
    // object) must degrade to the URL parse, not crash the whole result set.
    const author =
      (typeof v._author === "string" ? v._author.trim() : "") || authorFromUrl(out.video_url);
    if (author) out.author_handle = author;
    return out;
  });
}

// Parse the snowball bridge's {creators, seedResults} envelope. Same exit/JSON
// guards as parseBridgeOutput, and every element is hardened across the
// untrusted JSON boundary: rows missing `handle` or with a non-numeric count are
// dropped, not crashed on. EMPTY `creators` is a VALID result (most following
// lists are private/blocked) — never an error; that's exactly why seedResults
// exists, so the caller can tell a blocked graph from one with no overlap.
export function parseSimilarOutput(exit: number, stdout: string, stderr: string): SimilarResult {
  const name = "tiktok-oss";
  if (exit !== 0) {
    const errBody =
      parseErrorBody(stdout) ?? (stderr.trim() || `bridge exited ${exit} with no output`);
    throw new ProviderError(`tiktok-oss: ${errBody}`, name);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-JSON output: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ProviderError(
      `tiktok-oss: bridge returned non-envelope: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  const env = parsed as { creators?: unknown; seedResults?: unknown };
  const creators: SimilarCreator[] = [];
  if (Array.isArray(env.creators)) {
    for (const row of env.creators as Array<{ handle?: unknown; seedsFollowing?: unknown }>) {
      const handle = typeof row?.handle === "string" ? row.handle.trim() : "";
      const n = typeof row?.seedsFollowing === "number" ? row.seedsFollowing : NaN;
      if (handle && Number.isFinite(n)) creators.push({ handle, seedsFollowing: n });
    }
  }
  const seedResults: SeedResult[] = [];
  if (Array.isArray(env.seedResults)) {
    for (const row of env.seedResults as Array<{ handle?: unknown; status?: unknown }>) {
      const handle = typeof row?.handle === "string" ? row.handle.trim() : "";
      const status = typeof row?.status === "number" ? row.status : NaN;
      if (handle && Number.isFinite(status)) seedResults.push({ handle, status });
    }
  }
  return { creators, seedResults };
}

// Extract the @handle from a TikTok video URL. Returns "" when the URL has no
// /@handle/ segment (e.g. a bare `tiktok.com/video/<id>` the relay sometimes
// returns). Strips a leading @ and lower-cases for consistency with the rest of
// the pipeline (handles are case-insensitive on TikTok).
export function authorFromUrl(url: string | undefined): string {
  if (!url) return "";
  const m = url.match(/tiktok\.com\/@([^/?#]+)/i);
  return m ? m[1]!.replace(/^@/, "").toLowerCase() : "";
}

function parseErrorBody(stdout: string): string | null {
  try {
    const obj = JSON.parse(stdout);
    if (obj && typeof obj === "object" && typeof obj.error === "string") return obj.error;
  } catch {
    /* ignore */
  }
  return null;
}

function resolveScript(): string {
  // The Python bridge lives in scripts/tiktok_fetch.py at the repo root.
  // This file may be running from one of three locations depending on how
  // ugcspy was invoked, so we walk up from import.meta.url and try each
  // candidate path until we find the script:
  //
  //   - dev:   src/providers/tiktok-oss.ts -> ../../scripts/
  //   - dist:  dist/cli.js                  -> ../scripts/
  //   - npm:   node_modules/ugcspy/dist/cli.js -> ../scripts/
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "scripts", "tiktok_fetch.py"),
    resolve(here, "..", "scripts", "tiktok_fetch.py"),
  ];
  for (const path of candidates) {
    try {
      // Bun.file is sync metadata; existsSync would be cheaper but Bun.file
      // works without an extra import.
      const f = Bun.file(path);
      if (f.size > 0) return path;
    } catch {
      // file doesn't exist; try next
    }
  }
  // Fall back to the dev path so the error is human-readable.
  return candidates[0]!;
}

// Validate one untrusted transcript-doc shape from the bridge. Throws on a
// shape that would crash the renderer downstream.
function validateTranscriptDoc(parsed: unknown, raw: string): TranscriptDoc {
  const name = "tiktok-oss";
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ProviderError(
      `tiktok-oss: transcript bridge returned a non-object: ${raw.slice(0, 300)}`,
      name,
    );
  }
  const doc = parsed as Record<string, unknown>;
  const kind = doc.audio_kind;
  if (kind !== "speech" && kind !== "music" && kind !== "mixed") {
    throw new ProviderError(
      `tiktok-oss: transcript doc has invalid audio_kind: ${String(kind).slice(0, 50)}`,
      name,
    );
  }
  const segments = Array.isArray(doc.segments)
    ? (doc.segments as TranscriptSegment[]).filter(
        (s) => s !== null && typeof s === "object" && typeof s.text === "string",
      )
    : [];
  return {
    language: typeof doc.language === "string" ? doc.language : null,
    duration_sec: typeof doc.duration_sec === "number" ? doc.duration_sec : 0,
    segments,
    audio_kind: kind,
    lexical_word_count:
      typeof doc.lexical_word_count === "number" && Number.isFinite(doc.lexical_word_count)
        ? doc.lexical_word_count
        : 0,
    video_url: typeof doc.video_url === "string" ? doc.video_url : undefined,
    whisper_model: typeof doc.whisper_model === "string" ? doc.whisper_model : undefined,
  };
}

// Pure parse of the bridge's transcript-mode (exit, stdout, stderr) — exported
// for tests. The transcript payload is an OBJECT (one doc), unlike the search
// modes' array, and an untrusted-shape doc must fail loudly here rather than
// crash the renderer downstream.
export function parseTranscriptOutput(
  exit: number,
  stdout: string,
  stderr: string,
): TranscriptDoc {
  const name = "tiktok-oss";
  if (exit !== 0) {
    const errBody =
      parseErrorBody(stdout) ?? (stderr.trim() || `bridge exited ${exit} with no output`);
    throw new ProviderError(`tiktok-oss: ${errBody}`, name);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new ProviderError(
      `tiktok-oss: transcript bridge returned non-JSON output: ${stdout.slice(0, 300)}`,
      name,
    );
  }
  return validateTranscriptDoc(parsed, stdout);
}

// Pure parse of the BATCH transcript output — a JSON array aligned with the
// input urls, each element either a doc or an {"error": ...} envelope. The
// bridge exits 1 only when EVERY item failed or a top-level failure (missing
// whisper) prevented the run; in the all-failed case stdout still carries the
// aligned array, so prefer parsing it over throwing. Exported for tests.
export function parseTranscriptBatchOutput(
  exit: number,
  stdout: string,
  stderr: string,
  expectedCount: number,
): Array<TranscriptDoc | { error: string }> {
  const name = "tiktok-oss";
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    parsed = undefined;
  }
  if (!Array.isArray(parsed)) {
    // Top-level failure (no whisper, bad payload) — same envelope as single.
    const errBody =
      parseErrorBody(stdout) ?? (stderr.trim() || `bridge exited ${exit} with no output`);
    throw new ProviderError(`tiktok-oss: ${errBody}`, name);
  }
  if (parsed.length !== expectedCount) {
    throw new ProviderError(
      `tiktok-oss: transcript batch returned ${parsed.length} results for ${expectedCount} urls`,
      name,
    );
  }
  return parsed.map((item) => {
    if (item !== null && typeof item === "object" && "error" in (item as object)) {
      return { error: String((item as { error: unknown }).error) };
    }
    try {
      return validateTranscriptDoc(item, JSON.stringify(item) ?? "");
    } catch (err) {
      return { error: (err as Error).message };
    }
  });
}
