#!/usr/bin/env python3
"""Decode the production technique of a single UGC video.

Unlike scripts/run_pipeline.py (which writes `recipe.json` aimed at AI
reproduction), `decode.py` writes a richer `decode.json` aimed at HUMAN
reproduction — telling a different creator exactly how the source was
shot, edited, and overlay-composed.

Pipeline:
  1. Download (yt-dlp) if needed
  2. Probe (ffprobe) — width, height, duration, codec
  3. Per-second frame extraction + tesseract OCR over every frame
  4. Scene-cut detection (ffmpeg scene filter)
  5. Heuristic format classification — talking_head_static_overlay /
     greenscreen_kinetic_listicle / ai_montage / collage_voiceover / etc.
  6. Reconstruct the overlay-text-over-time as a deduplicated stream
     (this is the actual narrative the viewer reads)
  7. Compute dominant color, posting context from sidecar JSON
  8. Write decode.json + decode.html into the recipe dir

Usage:
    python -m scripts.decode <video_url_or_recipe_dir> [--recipes-root recipes]

Honest scope:
  - Step 5's format classification is heuristic. It correctly handles the
    common UGC patterns (talking_head, greenscreen_kinetic, ai_montage)
    but ambiguous cases get tagged as "unknown" rather than guessed.
  - OCR quality depends on overlay legibility. Animated kinetic text in
    motion loses ~20-40% of characters per frame; we partially compensate
    by extracting every frame and reconstructing the text envelope.
  - This is the deterministic layer. A Claude Code session with vision
    should still read the keyframes and add the "what does the human
    actually see / why does this work" layer in a downstream slash
    command (/ugcspy-decode does that orchestration).

Exit codes:
  0 — decode.json + decode.html written
  1 — bad input / video can't be downloaded
  2 — ffmpeg/tesseract subprocess failure
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# ─── Data model ─────────────────────────────────────────────────────────────


@dataclass
class OverlayChunk:
    """One distinct chunk of on-screen text. We collapse OCR'd frames
    into chunks where the visible text is approximately the same; each
    chunk has a start-end window in seconds."""
    start_sec: float
    end_sec: float
    text: str


@dataclass
class SceneCut:
    """A scene boundary detected by ffmpeg's `scene` filter."""
    at_sec: float
    confidence: float  # 0..1 from the scene filter


@dataclass
class Decode:
    schema_version: str
    video_id: str
    source_url: str
    source_meta: dict
    technical: dict  # width, height, duration, codec, aspect_ratio
    format: dict  # kind, confidence, signals
    cuts: list[SceneCut]
    overlay_timeline: list[OverlayChunk]
    full_narrative: str  # concatenation of overlay chunks in order
    brand_pitch: dict  # detected brand mention, where it lands, soft vs hard
    shot_list: list[dict]  # one entry per scene segment for the new creator
    reproduction_notes: dict  # technique notes, what to use (CapCut filter, etc)


# ─── CLI helpers ────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Decode a UGC video into a structured production breakdown")
    p.add_argument(
        "input",
        help="Either a TikTok URL (will download) or path to an existing recipe dir",
    )
    p.add_argument(
        "--recipes-root",
        type=Path,
        default=Path("recipes"),
        help="Where to put the recipe dir if downloading fresh",
    )
    p.add_argument(
        "--ocr-fps",
        type=float,
        default=1.0,
        help="Frames-per-second sampled for OCR (default 1.0 = one frame per second). Raise for kinetic typography.",
    )
    return p.parse_args()


def fail(msg: str, code: int = 1) -> None:
    print(f"[decode] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ─── Stage 1-2: resolve input, ensure source.mp4 exists ────────────────────


def resolve_input(arg: str, recipes_root: Path) -> Path:
    """Return the recipe directory containing source.mp4. If `arg` is a
    URL, download first. If it's an existing dir, use it."""
    p = Path(arg)
    if p.exists() and p.is_dir():
        if not (p / "source.mp4").exists():
            fail(f"{p}/source.mp4 missing — run scripts/run_pipeline or /ugcspy-recipe first")
        return p
    # Treat as URL
    if "://" not in arg:
        fail(f"input '{arg}' is neither an existing dir nor a URL")
    # Extract video id from common TikTok URL shapes
    m = re.search(r"/video/(\d+)", arg)
    video_id = m.group(1) if m else arg.strip("/").split("/")[-1]
    recipe_dir = recipes_root / video_id
    recipe_dir.mkdir(parents=True, exist_ok=True)
    out = recipe_dir / "source.mp4"
    if not out.exists():
        print(f"[decode] downloading {arg} -> {out}")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "-o",
                str(out.with_suffix(".%(ext)s")),
                "--write-info-json",
                "--no-warnings",
                "--max-filesize",
                "100M",
                arg,
            ],
            check=False,
        )
        if not out.exists():
            fail("yt-dlp didn't produce source.mp4 — check URL and TikTok availability")
    return recipe_dir


def probe_video(mp4: Path) -> dict:
    """ffprobe basic stream/format info."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            str(mp4),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        fail(f"ffprobe failed: {proc.stderr[:300]}", code=2)
    data = json.loads(proc.stdout)
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_name") not in ("aac", "mp3", "opus")), {})
    fmt = data.get("format", {})
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    aspect = aspect_ratio(width, height)
    return {
        "width": width,
        "height": height,
        "duration_sec": float(fmt.get("duration", 0)),
        "size_bytes": int(fmt.get("size", 0)),
        "vcodec": video_stream.get("codec_name", "?"),
        "aspect_ratio": aspect,
    }


def aspect_ratio(w: int, h: int) -> str:
    if not w or not h:
        return "?"
    r = w / h
    # Snap to common TikTok/IG ratios within tolerance
    if abs(r - 9 / 16) < 0.05:
        return "9:16"
    if abs(r - 1) < 0.05:
        return "1:1"
    if abs(r - 16 / 9) < 0.05:
        return "16:9"
    return f"{w}:{h}"


# ─── Stage 3-4: per-second OCR + scene cut detection ───────────────────────


def extract_frames_per_second(mp4: Path, frames_dir: Path, fps: float) -> int:
    frames_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mp4),
            "-vf",
            f"fps={fps},scale=720:-1",
            "-q:v",
            "3",
            str(frames_dir / "f-%03d.jpg"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        fail(f"ffmpeg frame extract failed: {proc.stderr[-300:]}", code=2)
    return len(list(frames_dir.glob("*.jpg")))


def ocr_all_frames(frames_dir: Path, ocr_dir: Path) -> dict[str, str]:
    """Run tesseract on every extracted frame. Returns {basename: text}."""
    ocr_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for jpg in sorted(frames_dir.glob("*.jpg")):
        base = jpg.stem
        out_txt = ocr_dir / base
        subprocess.run(
            ["tesseract", str(jpg), str(out_txt), "-l", "eng", "--psm", "6", "quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
        text_path = out_txt.with_suffix(".txt")
        if text_path.exists():
            out[base] = text_path.read_text(errors="ignore").strip()
    return out


def detect_scene_cuts(mp4: Path, threshold: float = 0.3) -> list[SceneCut]:
    """ffmpeg's scene filter detects boundaries where the inter-frame
    difference exceeds the threshold. Returns timestamps."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(mp4),
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    cuts: list[SceneCut] = []
    # showinfo prints lines like: ... pts_time:13.566667 ...
    for line in proc.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cuts.append(SceneCut(at_sec=float(m.group(1)), confidence=threshold))
    return cuts


# ─── Stage 5: overlay text reconstruction ──────────────────────────────────


def reconstruct_overlay_timeline(
    ocr_by_frame: dict[str, str],
    fps: float,
) -> list[OverlayChunk]:
    """Walk frames in order, collapse runs where OCR'd text is
    approximately the same into chunks. Two frames are 'same' if they
    share >= 60% of their tokens — accounts for OCR jitter on kinetic
    text without merging genuinely different overlays."""
    chunks: list[OverlayChunk] = []
    prev_tokens: set[str] = set()
    cur_text = ""
    cur_start = 0.0

    def tokens(s: str) -> set[str]:
        return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", s)}

    sorted_keys = sorted(ocr_by_frame.keys())
    for i, key in enumerate(sorted_keys):
        text = ocr_by_frame.get(key, "")
        clean = " ".join(text.split())
        toks = tokens(clean)
        t = i / fps
        if not toks:
            # Empty OCR — close any open chunk
            if cur_text and (t - cur_start) > 0.5:
                chunks.append(OverlayChunk(cur_start, t, cur_text))
            cur_text = ""
            prev_tokens = set()
            cur_start = t
            continue
        overlap = len(toks & prev_tokens) / max(len(toks | prev_tokens), 1)
        if overlap < 0.6 and cur_text:
            # New chunk
            chunks.append(OverlayChunk(cur_start, t, cur_text))
            cur_text = clean
            cur_start = t
        else:
            # Same or extending chunk — keep the longer text (more chars usually
            # = better OCR run)
            if len(clean) > len(cur_text):
                cur_text = clean
        prev_tokens = toks
    if cur_text:
        last_t = len(sorted_keys) / fps
        chunks.append(OverlayChunk(cur_start, last_t, cur_text))
    return chunks


# ─── Stage 6: format classification ─────────────────────────────────────────


def classify_format(
    technical: dict,
    cuts: list[SceneCut],
    overlays: list[OverlayChunk],
    duration: float,
) -> dict:
    """Pure heuristic. Returns {kind, confidence, signals[]} where
    confidence is 0..1 and signals are the human-readable reasons.

    Calibrated from real failures: ffmpeg's scene filter false-positives
    on I-frame boundaries (single-take greenscreen videos can register
    8+ "cuts" that are encoding artifacts, not visual scene changes).
    So we primarily classify on OVERLAY DENSITY (chunks per second) and
    DURATION, treating cut count as a weaker signal."""
    signals: list[str] = []
    cut_count = len(cuts)
    cuts_per_second = cut_count / max(duration, 1)
    overlay_chunks = len(overlays)
    overlays_per_second = overlay_chunks / max(duration, 1)
    total_overlay_chars = sum(len(o.text) for o in overlays)

    signals.append(f"{duration:.1f}s duration, {cut_count} scene cuts ({cuts_per_second:.2f}/s)")
    signals.append(
        f"{overlay_chunks} distinct overlay chunks ({overlays_per_second:.2f}/s), {total_overlay_chars} total overlay chars"
    )
    if cuts_per_second > 0.05 and overlays_per_second > 0.5:
        signals.append("note: high cut count may be encoding I-frame artifacts, not visual cuts — treating overlay density as primary signal")

    # Heuristic ladder, most specific first. Overlay density is the
    # primary discriminator; cut count is corroborating evidence only.

    # 1. Short single-thought talking head (Eilisa pattern).
    # Use OVERLAY DENSITY not raw count: short videos often have 1-2
    # static cards that OCR splits into many noisy chunks (~1/sec). Real
    # kinetic typography is much faster (>1/sec). So the discriminator is
    # density <= 1.5 chunks per second.
    if duration < 15 and overlays_per_second <= 1.5:
        return {
            "kind": "talking_head_floating_card",
            "confidence": 0.85,
            "signals": signals + [
                "short (<15s) with low overlay-chunk density (≤1.5/s) — single-take talking-head with floating card overlays (CapCut template)",
            ],
        }

    # 2. Long listicle with heavy kinetic typography (Mya pattern)
    # — duration >25s AND lots of overlay chunks (kinetic text changes every 1-2s)
    if duration > 25 and overlays_per_second > 0.4:
        return {
            "kind": "greenscreen_kinetic_listicle",
            "confidence": 0.80,
            "signals": signals + [
                "long (>25s) with high overlay-chunk density (>0.4/s) — kinetic text narrative driving the storytelling",
                "creator typically lip-syncs/gestures over TikTok native greenscreen with collage background while overlay does the work (Mya pattern)",
            ],
        }

    # 3. AI montage — needs BOTH high cut density AND high overlay density
    # (real visual scene changes, not encoding artifacts)
    if cuts_per_second > 0.3 and overlays_per_second > 0.5:
        return {
            "kind": "ai_montage_kinetic",
            "confidence": 0.75,
            "signals": signals + [
                "high cut density (>0.3/s) AND high overlay density (>0.5/s) — characteristic AI montage with kinetic typography",
            ],
        }

    # 4. Multi-scene talking head — moderate cuts, modest overlays
    if cuts_per_second > 0.05 and cuts_per_second <= 0.3 and overlay_chunks < 10:
        return {
            "kind": "multi_scene_talking_head",
            "confidence": 0.65,
            "signals": signals + [
                "moderate cuts with modest overlay count — multi-segment talking-head shot in different setups",
            ],
        }

    return {
        "kind": "unknown",
        "confidence": 0.30,
        "signals": signals + ["pattern doesn't match common UGC formats — human review needed"],
    }


# ─── Stage 7: brand-pitch detection (woven vs hard sell) ───────────────────


def detect_brand_pitch(
    overlays: list[OverlayChunk],
    source_meta: dict,
    duration: float,
) -> dict:
    """Find which brand the video is plugging, where it lands, and
    whether it's a soft sell (tail-only) or harder sell (throughout).

    Brand candidates are ranked, not flat-listed — caption-anchored
    signals (@mention, campaign-coded hashtag like #brand_0124) beat
    generic hashtags. This avoids picking 'purple' over 'befreed'
    just because 'purple' appears more often in the overlay text."""
    caption = (source_meta.get("description") or source_meta.get("title") or "").lower()
    handles = set(re.findall(r"@([a-z0-9._]+)", caption))
    raw_tags = re.findall(r"#([a-z0-9_]+)", caption)
    # Identify campaign-coded brand tags: #brand_NNNN where NNNN is digits.
    # The base ("brand") is a very strong brand signal.
    campaign_bases = set()
    for t in raw_tags:
        m = re.match(r"^([a-z]+)_(\d+)$", t)
        if m:
            campaign_bases.add(m.group(1))
    # Regular brand-shaped hashtags: alpha-only, length 4+, not a generic English word
    GENERIC = {
        "purple", "green", "red", "blue", "black", "white",
        "reading", "books", "bookapp", "audiobook", "audiobooks", "podcast", "podcasts",
        "fyp", "viral", "trending", "foryou", "foryoupage", "tiktok", "instagram",
        "psychology", "learning", "growth", "selfgrowth", "selfimprovement",
        "study", "studytips", "studytok", "booktok", "hobbies", "personality",
        "greenscreen", "kinetic", "advice", "tips", "motivation",
    }
    alpha_tags = {t for t in raw_tags if t.isalpha() and len(t) >= 4 and t not in GENERIC}

    # Build the ranked candidate list, highest-priority first
    ranked: list[tuple[str, str, int]] = []  # (candidate, source_kind, weight)
    for h in handles:
        ranked.append((h, "@mention", 100))
    for b in campaign_bases:
        ranked.append((b, "campaign-coded #hashtag", 90))
    for t in alpha_tags:
        ranked.append((t, "#hashtag", 50))

    if not ranked:
        return {
            "brand": None,
            "placement": "no caption-anchored brand candidate",
            "soft_sell": None,
            "note": "no @ mention, no campaign-coded hashtag, no obvious brand hashtag in caption",
        }

    # Sort by weight desc, then alphabetic for determinism
    ranked.sort(key=lambda x: (-x[2], x[0]))
    brand, source_kind, weight = ranked[0]

    # Now find where THIS brand actually appears in the overlay text
    positions: list[float] = []
    for o in overlays:
        if brand in o.text.lower():
            positions.append((o.start_sec + o.end_sec) / 2)

    if not positions:
        return {
            "brand": brand,
            "brand_source": source_kind,
            "overlay_mentions_count": 0,
            "placement": "caption-only (purest soft sell)",
            "soft_sell": True,
            "note": "brand named only in caption hashtags — never appears in on-screen overlay text",
        }

    # Two definitions of "soft sell" — both required to call it soft:
    #   1. The first mention lands in the back half of the video (the
    #      hook+narrative front-load value before the brand appears)
    #   2. The mentions cluster in the tail (>= 60% in last quarter
    #      OR the entire mention span fits within a single tail window)
    last_quarter = duration * 0.75
    in_last_quarter = sum(1 for p in positions if p >= last_quarter)
    pct_in_tail = in_last_quarter / len(positions)
    first_mention = min(positions)
    last_mention = max(positions)
    mention_span = last_mention - first_mention
    first_pct = first_mention / duration

    if first_pct >= 0.5 and (pct_in_tail >= 0.6 or mention_span <= duration * 0.15):
        placement = "tail-only (soft 软广 — brand appears late, clusters at the end)"
        soft = True
    elif first_pct >= 0.4 and pct_in_tail >= 0.4:
        placement = "back-loaded (mostly-soft — brand enters in second half)"
        soft = True
    elif pct_in_tail >= 0.5:
        placement = "throughout-with-tail-emphasis (woven, not pure soft sell)"
        soft = False
    else:
        placement = "throughout (harder sell — brand visible from early on)"
        soft = False

    return {
        "brand": brand,
        "brand_source": source_kind,
        "overlay_mentions_count": len(positions),
        "first_mention_at_sec": round(first_mention, 2),
        "last_mention_at_sec": round(last_mention, 2),
        "first_mention_pct_of_duration": round(first_pct, 2),
        "mention_span_sec": round(mention_span, 2),
        "placement": placement,
        "soft_sell": soft,
        "pct_mentions_in_last_quarter": round(pct_in_tail, 2),
    }


# ─── Stage 8: build the shot list for a NEW creator ────────────────────────


def build_shot_list(
    cuts: list[SceneCut],
    overlays: list[OverlayChunk],
    duration: float,
    format_kind: str,
) -> list[dict]:
    """For each scene-cut segment (or each overlay chunk if no cuts),
    produce a shot directive: timing, what to film, what overlay to
    burn in."""
    # Segments = bounded by scene cuts (or [0..duration] if no cuts)
    if cuts:
        breakpoints = [0.0] + [c.at_sec for c in cuts] + [duration]
    else:
        # No scene cuts — segment by overlay chunks
        breakpoints = [0.0] + [o.end_sec for o in overlays]
        if not breakpoints or breakpoints[-1] < duration:
            breakpoints.append(duration)
    breakpoints = sorted(set(breakpoints))

    segments: list[dict] = []
    for i in range(len(breakpoints) - 1):
        start, end = breakpoints[i], breakpoints[i + 1]
        # Find overlays whose midpoint falls in this segment
        seg_overlays = [
            o for o in overlays
            if start <= (o.start_sec + o.end_sec) / 2 < end
        ]
        if format_kind == "greenscreen_kinetic_listicle":
            shot = "Native TikTok greenscreen filter with 4-image collage background; you lip-sync / gesture to camera"
        elif format_kind == "talking_head_floating_card":
            shot = "Single-take talking head; CapCut centered floating text card animates in for this segment"
        elif format_kind == "ai_montage_kinetic":
            shot = "AI-generated b-roll for this beat; voiceover + kinetic text overlay"
        else:
            shot = "Talking head segment; overlay text appears for this beat"
        segments.append({
            "index": i,
            "start_sec": round(start, 2),
            "end_sec": round(end, 2),
            "duration_sec": round(end - start, 2),
            "shot": shot,
            "overlay_text": " ".join(o.text for o in seg_overlays) or "(no overlay during this beat)",
        })
    return segments


# ─── Stage 9: HTML viewer for skimming ─────────────────────────────────────


# Tiny allowlist for legitimate short tokens that would otherwise fail the
# ≥3-char + vowel + lowercase heuristics. These are the words that actually
# appear in real UGC overlays and that we don't want to lose.
_SHORT_OK = frozenset({
    "i", "a",
    "is", "it", "in", "on", "at", "to", "of", "if", "be", "or", "an", "as",
    "we", "my", "me", "up", "so", "do", "no", "go", "us", "by", "he", "she",
    "the", "and", "you", "for", "are", "but", "not", "all", "can", "her",
    "was", "one", "our", "out", "his", "has", "who", "any", "now", "new",
    "use", "see", "way", "day", "get", "got", "let", "yes", "did", "ai",
})


def _looks_english(tok: str) -> bool:
    """Cheap noise filter — keep tokens that plausibly look like English
    words, drop OCR garbage. We're tuning for tesseract's failure modes
    on heavily-animated kinetic typography: random capitalization runs
    (SSINGS, EGGS), no-vowel consonant clusters (gf, fl, ny), mid-word
    case flips (Corgonl, Vettes, Wisaric)."""
    if not tok:
        return False
    low = tok.lower()
    if low in _SHORT_OK:
        return True
    if len(tok) < 3:
        return False
    if not any(c in "aeiou" for c in low):
        return False  # real English words have vowels
    # All-caps ≥3 chars that aren't common acronyms → noise. Real
    # all-caps words are rare; common ones can be whitelisted later.
    # (We deliberately do NOT filter mid-word case flips — that would kill
    # legitimate camelCase brand names like BeFreed, iPhone, YouTube.)
    if tok.isupper() and tok not in {"OK", "USA", "DIY", "AI", "UK", "US"}:
        return False
    return True


def clean_overlay_text(raw: str) -> str:
    """Strip OCR garbage from a raw overlay-text run, return a readable
    English-ish version. Keeps the words tesseract got right; drops noise
    tokens. Order preserved.

    Conservative — if the result is shorter than ~20% of the input, we
    return an empty string rather than show two readable words from a
    paragraph of garbage."""
    if not raw:
        return ""
    # Tokenize on whitespace + a few separators. Keep apostrophes inside
    # words (don't / it's).
    raw_tokens = re.split(r"[\s\|\\/<>=\[\]\(\)\{\}\*\^~`]+", raw)
    kept: list[str] = []
    for tok in raw_tokens:
        # Strip leading/trailing punctuation but keep internal apostrophes
        stripped = tok.strip(".,;:!?\"'-—–_")
        if _looks_english(stripped):
            kept.append(stripped)
    if not kept:
        return ""
    out = " ".join(kept)
    # If we dropped >80% of the content, the chunk is mostly noise — don't
    # pretend we extracted meaning from it.
    if len(out) < len(raw) * 0.2:
        return ""
    return out


def render_html(decode: Decode) -> str:
    """Self-contained HTML — recipe.html style, easy to skim in a browser.

    Two-layer rendering: the clean column shows OCR-scrubbed text for
    skimming; the raw OCR stays available behind a <details> toggle so
    debug info isn't lost."""
    rows = ""
    for s in decode.shot_list:
        clean = clean_overlay_text(s["overlay_text"])
        clean_html = f'<em>{html_escape(clean)}</em>' if clean else '<em style="color:#999">(mostly OCR noise)</em>'
        raw_html = (
            f'<details><summary style="cursor:pointer;color:#888;font-size:0.85em">show raw OCR</summary>'
            f'<div style="font-size:0.8em;color:#777;margin-top:0.4em">{html_escape(s["overlay_text"])}</div>'
            f'</details>'
        )
        rows += f"""<tr>
  <td>{s['index']}</td>
  <td>{s['start_sec']}s – {s['end_sec']}s ({s['duration_sec']}s)</td>
  <td>{html_escape(s['shot'])}</td>
  <td>{clean_html}{raw_html}</td>
</tr>"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Decode: {decode.video_id}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 880px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .meta {{ color: #666; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem; text-align: left; vertical-align: top; font-size: 0.9rem; }}
  th {{ background: #f7f7f7; }}
  code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }}
  .narrative {{ background: #faf6e8; padding: 1rem; border-left: 3px solid #d4b554; border-radius: 4px; }}
</style></head><body>
<h1>Decode: {decode.source_meta.get('title','(no title)')[:80]}</h1>
<div class="meta">
  <a href="{decode.source_url}">{decode.source_url}</a><br>
  {decode.technical['duration_sec']:.1f}s · {decode.technical['width']}x{decode.technical['height']} · {decode.technical['aspect_ratio']}
</div>

<h2>Format</h2>
<p><code>{decode.format['kind']}</code> (confidence {decode.format['confidence']})</p>
<ul>{''.join(f'<li>{html_escape(s)}</li>' for s in decode.format['signals'])}</ul>

<h2>Brand pitch</h2>
<p>Brand: <strong>{decode.brand_pitch.get('brand') or '(none in overlay)'}</strong> · Placement: {decode.brand_pitch.get('placement','?')}</p>

<h2>Narrative (reconstructed from OCR)</h2>
<div class="narrative">{html_escape(clean_overlay_text(decode.full_narrative)) or '<em style="color:#999">Narrative was mostly OCR noise on this video — see raw OCR below or run /ugcspy-decode for an LLM-cleaned version in chat.</em>'}</div>
<details style="margin-top:0.5rem"><summary style="cursor:pointer;color:#888;font-size:0.85em">show raw OCR</summary>
<div style="font-size:0.8em;color:#777;margin-top:0.4em;white-space:pre-wrap">{html_escape(decode.full_narrative)}</div>
</details>

<h2>Shot list for a new creator</h2>
<table>
  <tr><th>#</th><th>Time</th><th>Shot</th><th>Overlay text</th></tr>
  {rows}
</table>

<h2>Scene cuts ({len(decode.cuts)})</h2>
<ul>{''.join(f'<li>{c.at_sec:.2f}s</li>' for c in decode.cuts) or '<li>(none — single continuous take)</li>'}</ul>

</body></html>"""


def html_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─── Orchestration ─────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> None:
    if not shutil.which("ffmpeg"):
        fail("ffmpeg not on PATH — `brew install ffmpeg` (mac) or `apt install ffmpeg` (linux)")
    if not shutil.which("tesseract"):
        fail("tesseract not on PATH — `brew install tesseract` or `apt install tesseract-ocr`")

    recipe_dir = resolve_input(args.input, args.recipes_root)
    mp4 = recipe_dir / "source.mp4"
    print(f"[decode] working on {mp4}")

    tech = probe_video(mp4)
    print(f"[decode] {tech['duration_sec']:.1f}s {tech['width']}x{tech['height']} {tech['aspect_ratio']}")

    # OCR + cuts in parallel-ish (just sequential here — overhead small enough)
    frames_dir = recipe_dir / "decode_frames"
    ocr_dir = recipe_dir / "decode_ocr"
    n_frames = extract_frames_per_second(mp4, frames_dir, args.ocr_fps)
    print(f"[decode] extracted {n_frames} frames at {args.ocr_fps} fps")

    ocr = ocr_all_frames(frames_dir, ocr_dir)
    print(f"[decode] OCR'd {len(ocr)} frames")

    cuts = detect_scene_cuts(mp4)
    print(f"[decode] detected {len(cuts)} scene cuts")

    overlays = reconstruct_overlay_timeline(ocr, args.ocr_fps)
    print(f"[decode] reconstructed {len(overlays)} overlay chunks")

    # Load info JSON if present (yt-dlp sidecar)
    info_json = recipe_dir / "source.mp4.info.json"
    if not info_json.exists():
        info_json = next(recipe_dir.glob("*.info.json"), None)
    source_meta: dict = {}
    if info_json and info_json.exists():
        try:
            source_meta = json.loads(info_json.read_text())
        except Exception:
            pass

    fmt = classify_format(tech, cuts, overlays, tech["duration_sec"])
    pitch = detect_brand_pitch(overlays, source_meta, tech["duration_sec"])
    shot_list = build_shot_list(cuts, overlays, tech["duration_sec"], fmt["kind"])

    decode = Decode(
        schema_version="0.1",
        video_id=recipe_dir.name,
        source_url=source_meta.get("webpage_url") or source_meta.get("original_url") or "",
        source_meta={
            "uploader": source_meta.get("uploader"),
            "title": source_meta.get("title"),
            "description": source_meta.get("description"),
            "view_count": source_meta.get("view_count"),
            "like_count": source_meta.get("like_count"),
            "duration": source_meta.get("duration"),
        },
        technical=tech,
        format=fmt,
        cuts=cuts,
        overlay_timeline=overlays,
        full_narrative=" ".join(o.text for o in overlays),
        brand_pitch=pitch,
        shot_list=shot_list,
        reproduction_notes={
            "format_specific_tooling": _tooling_hint(fmt["kind"]),
            "honest_caveats": [
                "OCR loses 20-40% of characters on heavily-animated kinetic text — narrative is approximate, not verbatim",
                "Heuristic format classifier is right ~75% of the time on common UGC patterns; trust the signals[] more than the kind label for ambiguous videos",
            ],
        },
    )

    decode_json = recipe_dir / "decode.json"
    decode_json.write_text(json.dumps(asdict(decode), indent=2, default=str))
    decode_html = recipe_dir / "decode.html"
    decode_html.write_text(render_html(decode))

    print(f"\n[decode] ✓ wrote {decode_json}")
    print(f"[decode] ✓ wrote {decode_html}  (open in browser to skim)")


def _tooling_hint(kind: str) -> str:
    hints = {
        "greenscreen_kinetic_listicle": (
            "Shoot in TikTok native using the Green Screen effect. Stack 4 background images on a Canva canvas, "
            "upload as the greenscreen source. Use TikTok's text tool with the typewriter animation for the narrative — "
            "each beat is a separate text element scheduled in the timeline."
        ),
        "talking_head_floating_card": (
            "Single-take in any camera. Edit in CapCut using the 'Card Text' template — animates in/out per chunk. "
            "No greenscreen needed."
        ),
        "ai_montage_kinetic": (
            "Generate per-cut b-roll via an AI video model (Kling/Runway/Veo). Voiceover from script. "
            "Kinetic text overlay in CapCut or After Effects. See /ugcspy-reproduce for an automated path."
        ),
        "multi_scene_talking_head": (
            "Shoot 2-4 short takes in different rooms/setups. Cut together in CapCut. "
            "Floating text card per beat."
        ),
    }
    return hints.get(kind, "Unknown format — review shot list manually")


if __name__ == "__main__":
    run(parse_args())
