"""Prefer the video platform's OWN caption track over Whisper.

Many TikTok / IG / YouTube videos ship a creator-authored or
platform-auto-generated subtitle track (a WebVTT file). When present, it
is almost always a better spoken-narrative source than running Whisper
over the audio bed:

  - It's the actual script the platform aligned to the video (near
    verbatim), not an ASR guess fighting background music.
  - It costs nothing — no 700MB torch + whisper model, no GPU/CPU minutes.
  - It degrades gracefully on music-heavy UGC where Whisper hallucinates
    song lyrics.

So decode.py's audio stage now follows this priority:

    1. embedded caption track (this module)  ← preferred when available
    2. Whisper transcription (scripts.transcribe)  ← fallback
    3. nothing (--no-audio, or both unavailable)

This module does two things:

  fetch_embedded_subs(url, dest_dir)
      Ask yt-dlp to download ONLY the subtitle track (no video) and
      return the path to the .vtt it wrote, or None if the platform
      serves no captions for this video.

  parse_vtt(path)
      Parse a WebVTT file into the same transcript document shape that
      scripts.transcribe emits, so decode.py can treat both sources
      interchangeably:

        {"language": "en",
         "duration_sec": 47.3,
         "segments": [{"start": 0.0, "end": 1.1, "text": "..."}, ...],
         "words":    [{"start": 0.0, "end": 1.1, "word": "..."}, ...],
         "source": "embedded_subs"}

WebVTT is the only format we parse — it's what every major platform
serves through yt-dlp, and it's a tiny well-specified grammar. SRT and
others are intentionally out of scope; if a platform only offers SRT we
fall through to Whisper.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# A VTT cue timestamp: HH:MM:SS.mmm or MM:SS.mmm
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})\.(\d{1,3})")
# A cue timing line: "00:00:01.100 --> 00:00:01.980 [optional settings]"
_TIMING_RE = re.compile(r"^(.+?)\s*-->\s*(\S+)")
# Inline VTT tags we strip from cue text: <c>, </c>, <00:00:01.100>, <v Name>
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_ts(token: str) -> float | None:
    """Parse a single VTT timestamp token into seconds. Returns None if
    the token isn't a timestamp (so caller can skip malformed lines)."""
    m = _TS_RE.match(token.strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    millis_str = m.group(4)
    # Normalize fractional part to milliseconds (".1" → 100ms, ".12" → 120ms)
    millis = int(millis_str.ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _clean_cue_text(lines: list[str]) -> str:
    """Join a cue's text lines, strip inline VTT tags, collapse whitespace."""
    joined = " ".join(lines)
    joined = _TAG_RE.sub("", joined)
    # VTT escapes: &amp; &lt; &gt; &nbsp;
    joined = (
        joined.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
    )
    return " ".join(joined.split())


def parse_vtt(path: Path, *, language: str | None = None) -> dict[str, Any]:
    """Parse a WebVTT file into a transcript document.

    Each cue becomes one segment. We also emit a flat `words` list: for a
    caption track we don't have true per-word timestamps, so each cue's
    full text is recorded as a single "word" entry spanning the cue
    window. Downstream pairing (transcribe.pair_words_to_cuts) buckets by
    [start, end) so cue-granularity is sufficient for shot-list alignment;
    it's coarser than Whisper's per-word stamps but the text is more
    accurate, which is the better tradeoff for most UGC.

    `language` lets the caller pass the language code yt-dlp reported
    (e.g. "eng-US" → we store "en"); WebVTT itself rarely encodes it.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Normalize newlines, drop the leading "WEBVTT" header + any NOTE blocks.
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    segments: list[dict[str, Any]] = []
    duration = 0.0

    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # Skip the file header and metadata blocks.
        if lines[0].upper().startswith("WEBVTT"):
            continue
        if lines[0].upper().startswith(("NOTE", "STYLE", "REGION")):
            continue
        # A cue may start with an optional numeric/string identifier line,
        # then the timing line. Find the timing line within the block.
        timing_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                timing_idx = i
                break
        if timing_idx is None:
            continue
        m = _TIMING_RE.match(lines[timing_idx])
        if not m:
            continue
        start = _parse_ts(m.group(1))
        end = _parse_ts(m.group(2))
        if start is None or end is None:
            continue
        text = _clean_cue_text(lines[timing_idx + 1 :])
        if not text:
            continue
        if end > duration:
            duration = end
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})

    # De-dupe consecutive identical cues (TikTok auto-captions sometimes
    # repeat a line across two overlapping cues) and merge them.
    merged: list[dict[str, Any]] = []
    for seg in segments:
        if merged and seg["text"] == merged[-1]["text"]:
            merged[-1]["end"] = seg["end"]
            continue
        merged.append(seg)

    words = [
        {"start": s["start"], "end": s["end"], "word": s["text"]} for s in merged
    ]
    norm_lang = _normalize_lang(language)
    return {
        "language": norm_lang,
        "duration_sec": round(duration, 3),
        "segments": merged,
        "words": words,
        "source": "embedded_subs",
    }


def _normalize_lang(code: str | None) -> str | None:
    """Reduce a yt-dlp subtitle language code to a short ISO-639-1-ish tag.
    "eng-US" → "en", "en-US" → "en", "zh-Hans" → "zh". Best-effort; returns
    the original (lowercased) when we can't map it."""
    if not code:
        return None
    base = code.lower().split("-")[0]
    three_to_two = {"eng": "en", "zho": "zh", "chi": "zh", "spa": "es", "fra": "fr", "deu": "de"}
    return three_to_two.get(base, base)


def fetch_embedded_subs(
    url: str,
    dest_dir: Path,
    *,
    preferred_langs: tuple[str, ...] = ("en", "eng", "en-US", "eng-US"),
    timeout_sec: int = 120,
) -> tuple[Path, str] | None:
    """Download ONLY the caption track for `url` via yt-dlp (no video).

    Returns (vtt_path, lang_code) for the first track that downloads, or
    None when the platform serves no usable captions. We request the
    preferred languages in order; yt-dlp picks the best available match.

    Never raises on "no subs" — that's the normal case for plenty of
    videos and the caller falls back to Whisper. Only logs+returns None.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / "embedded.%(ext)s")
    # --write-subs grabs creator/platform-authored captions; --write-auto-subs
    # grabs auto-generated ones. We take either — both beat Whisper-on-music.
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        ",".join(preferred_langs),
        "--sub-format",
        "vtt",
        "--no-warnings",
        "-o",
        out_tmpl,
        url,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[decode] embedded-subs: yt-dlp unavailable or timed out ({e}); will try Whisper.", file=sys.stderr)
        return None

    # yt-dlp names the file embedded.<lang>.vtt. Pick the first .vtt written.
    vtts = sorted(dest_dir.glob("embedded*.vtt"))
    if not vtts:
        return None
    vtt = vtts[0]
    # Derive the language from the filename suffix: embedded.eng-US.vtt → eng-US
    lang = vtt.stem.split(".", 1)[1] if "." in vtt.stem else "und"
    return vtt, lang
