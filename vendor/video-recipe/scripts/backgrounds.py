"""Per-cut background imagery: search the web (Pinterest first) for a photo
that matches each cut's topic, then composite it behind the cut as a blurred
backdrop via ffmpeg.

Why: greenscreen-kinetic / collage UGC (the Mya pattern) rides on real
reference imagery — a 4-image Canva collage behind the creator. When we
reproduce that format, a flat AI clip with no background context looks
emptier than the source. This module finds "as close as possible" imagery
for each cut's subject and lays it in as an aesthetic backdrop.

Design (per the project owner's call):

  - **Pluggable source.** `ImageSource` is a small protocol. We ship a
    Pinterest backend (the requested source) AND a generic web-image
    fallback, because Pinterest has no public search API and blocks
    scrapers aggressively — when Pinterest returns nothing, the feature
    still works via the fallback instead of silently producing no
    background.

  - **ffmpeg overlay layer.** The background is composited with ffmpeg
    (no extra video-gen API cost, works on any clip). The cut's generated
    video sits in the foreground; the searched image fills the frame as a
    blurred, darkened backdrop. This keeps Kling's image2video reference
    slot free for the character face (#25).

Honest scope: web scraping for images is inherently brittle and is NOT
exercised in CI (no network, and we won't hammer Pinterest from tests).
The pure pieces — query derivation, source selection, and the ffmpeg
composite filter construction — ARE unit-tested. The live fetch is
best-effort and degrades to "no background for this cut" on any failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Protocol

# Generic stopwords we strip when turning overlay/scene text into a tight
# image-search query. Image search does better with concrete nouns than with
# filler — "purple introspective personality" beats "people who tend to be".
_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "is", "are", "was", "were", "be", "been", "being", "that", "this", "these",
    "those", "your", "you", "they", "their", "them", "it", "its", "we", "our",
    "who", "which", "what", "when", "very", "much", "also", "tend", "tends",
    "literally", "really", "people", "person", "like", "likes", "value",
    "proves", "shows", "show", "reflection",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{1,}")


def derive_query(
    overlay_text: str = "",
    scene_description: str = "",
    *,
    max_terms: int = 5,
) -> str:
    """Turn a cut's overlay text + scene description into a compact image
    search query. Prefers concrete content words, drops stopwords + OCR
    noise, dedupes, and caps length so the search stays focused.

    Returns "" when there's nothing meaningful to search for (caller then
    skips the background for that cut)."""
    text = f"{scene_description} {overlay_text}".strip()
    if not text:
        return ""
    seen: set[str] = set()
    terms: list[str] = []
    for m in _WORD_RE.finditer(text):
        w = m.group(0).lower()
        if w in _QUERY_STOPWORDS or w in seen or len(w) < 3:
            continue
        seen.add(w)
        terms.append(w)
        if len(terms) >= max_terms:
            break
    if not terms:
        return ""
    # Add an aesthetic qualifier so we bias toward backdrop-suitable imagery
    # (mood/texture shots) rather than busy infographics — but don't repeat
    # words already present in the extracted terms.
    base = " ".join(terms)
    qualifier = " ".join(w for w in ("aesthetic", "background") if w not in seen)
    return f"{base} {qualifier}".strip() if qualifier else base


# ─── Pluggable image source ─────────────────────────────────────────────────


class ImageSource(Protocol):
    """A source that, given a query, returns candidate image URLs (best
    first). Implementations must NEVER raise on a normal "no results" or
    network blip — return [] instead so the caller degrades gracefully."""

    name: str

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        ...


class PinterestSource:
    """Pinterest image search.

    Pinterest has no public search API for this and blocks scrapers
    aggressively, so this is a best-effort HTML/JSON scrape that WILL
    break periodically. It returns [] on any failure (block, layout
    change, network) so the pipeline falls through to the web fallback
    rather than crashing. This is the requested primary source; treat its
    output as a bonus, not a guarantee."""

    name = "pinterest"

    def __init__(self, timeout_sec: int = 15) -> None:
        self.timeout_sec = timeout_sec

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        if not query:
            return []
        try:
            import requests
        except ImportError:
            print("[backgrounds] pinterest: requests not installed; skipping.", file=sys.stderr)
            return []
        # Pinterest's resource endpoint used by its own search page. Shape
        # drifts; we parse defensively and bail to [] on anything unexpected.
        url = "https://www.pinterest.com/resource/BaseSearchResource/get/"
        params = {
            "source_url": f"/search/pins/?q={query}",
            "data": f'{{"options":{{"query":"{query}","scope":"pins"}},"context":{{}}}}',
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout_sec)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:
            return []
        return _extract_pinterest_image_urls(data, limit)


def _extract_pinterest_image_urls(data: object, limit: int) -> list[str]:
    """Walk Pinterest's resource JSON for the largest image URL per pin.
    Defensive: returns whatever it can find, [] on unexpected shapes.
    Factored out so it's unit-testable against a captured fixture without
    any network."""
    urls: list[str] = []
    try:
        results = (
            data.get("resource_response", {})  # type: ignore[union-attr]
            .get("data", {})
            .get("results", [])
        )
    except AttributeError:
        return []
    for pin in results or []:
        if not isinstance(pin, dict):
            continue
        images = pin.get("images") or {}
        # Pinterest keys images by size string ("orig", "736x", ...). Prefer
        # the original / largest.
        best = None
        for key in ("orig", "736x", "600x315", "474x"):
            if key in images and isinstance(images[key], dict) and images[key].get("url"):
                best = images[key]["url"]
                break
        if not best:
            for v in images.values():
                if isinstance(v, dict) and v.get("url"):
                    best = v["url"]
                    break
        if best:
            urls.append(best)
        if len(urls) >= limit:
            break
    return urls


class WebImageSource:
    """Generic web-image fallback via DuckDuckGo's image endpoint — no API
    key, more tolerant than Pinterest. Used when Pinterest returns nothing
    so the feature still produces a background. Same never-raise contract."""

    name = "web"

    def __init__(self, timeout_sec: int = 15) -> None:
        self.timeout_sec = timeout_sec

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        if not query:
            return []
        try:
            import requests
        except ImportError:
            return []
        try:
            # DuckDuckGo requires a vqd token from the HTML page first.
            token_resp = requests.get(
                "https://duckduckgo.com/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self.timeout_sec,
            )
            m = re.search(r"vqd=([\d-]+)", token_resp.text) or re.search(
                r'vqd="([\d-]+)"', token_resp.text
            )
            if not m:
                return []
            vqd = m.group(1)
            resp = requests.get(
                "https://duckduckgo.com/i.js",
                params={"q": query, "vqd": vqd, "o": "json"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://duckduckgo.com/"},
                timeout=self.timeout_sec,
            )
            data = resp.json()
        except Exception:
            return []
        return [r["image"] for r in (data.get("results") or [])[:limit] if r.get("image")]


_SOURCES: dict[str, type] = {
    "pinterest": PinterestSource,
    "web": WebImageSource,
}


def pick_sources(name: str) -> list[ImageSource]:
    """Return the ordered source chain for a --backgrounds value.

    "pinterest" → [Pinterest, Web]  (Pinterest first, web as fallback)
    "web"       → [Web]
    Unknown     → [Web]  (safe default)"""
    if name == "pinterest":
        return [PinterestSource(), WebImageSource()]
    if name == "web":
        return [WebImageSource()]
    return [WebImageSource()]


def fetch_background(
    query: str,
    dest: Path,
    sources: list[ImageSource],
    *,
    timeout_sec: int = 20,
) -> Path | None:
    """Search `sources` in order for `query`, download the first image that
    fetches successfully to `dest`, return its path. None when nothing was
    found/downloadable (caller skips the background for this cut).

    Best-effort by contract — never raises on network/source failure."""
    if not query:
        return None
    try:
        import requests
    except ImportError:
        return None
    for src in sources:
        candidates = src.search(query, limit=5)
        for img_url in candidates:
            try:
                r = requests.get(img_url, timeout=timeout_sec, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200 or not r.content:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                if dest.stat().st_size > 0:
                    print(f"[backgrounds] {src.name}: '{query}' -> {dest.name}")
                    return dest
            except Exception:
                continue
    print(f"[backgrounds] no image found for '{query}' (tried {', '.join(s.name for s in sources)})", file=sys.stderr)
    return None


# ─── ffmpeg composite ────────────────────────────────────────────────────────


def build_background_filter(width: int, height: int) -> str:
    """filter_complex graph: blurred/darkened full-frame background image
    with the foreground clip scaled to ~78% and centered on top.

    Inputs (caller wires): [0:v] = foreground clip, [1:v] = background image.
    Output label: [out].

      - background: scale to cover the frame, crop to exact WxH, gaussian
        blur, darken so the foreground reads clearly.
      - foreground: scale to fit within ~78% of the frame (preserves the
        AI clip's aspect), centered.

    Returned as a single -filter_complex string."""
    fg_w = int(width * 0.78)
    return (
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2,eq=brightness=-0.18[bg];"
        f"[0:v]scale={fg_w}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
    )


def composite_background(
    clip: Path,
    background_image: Path,
    out_path: Path,
    width: int,
    height: int,
) -> bool:
    """Composite `background_image` behind `clip` via ffmpeg, writing
    `out_path`. Returns True on success, False on ffmpeg failure (caller
    keeps the un-composited clip). Audio is stream-copied from the clip."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(clip),
            "-i",
            str(background_image),
            "-filter_complex",
            build_background_filter(width, height),
            "-map",
            "[out]",
            "-map",
            "0:a?",  # copy clip audio if present
            "-c:a",
            "copy",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            str(out_path),
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        print(
            f"[backgrounds] composite failed (rc={proc.returncode}); keeping un-composited clip.",
            file=sys.stderr,
        )
        return False
    return True
