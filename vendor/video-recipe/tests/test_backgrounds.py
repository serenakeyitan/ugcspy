"""Tests for scripts.backgrounds — per-cut background search + composite.

No network: the live search/download is best-effort and not exercised
here. We test the pure pieces (query derivation, source-chain selection,
Pinterest JSON extraction against a captured-shape fixture, the ffmpeg
composite filter, and the real ffmpeg composite on a synthetic clip).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scripts import backgrounds

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ─── derive_query ────────────────────────────────────────────────────────────


def test_derive_query_extracts_content_words():
    q = backgrounds.derive_query(
        overlay_text="People who like the colour purple",
        scene_description="moody purple lighting",
    )
    # Stopwords (people, who, the) dropped; content words kept; aesthetic
    # qualifier appended.
    assert "purple" in q
    assert "colour" in q
    assert "people" not in q
    assert q.endswith("aesthetic background")


def test_derive_query_dedupes_and_caps():
    q = backgrounds.derive_query(
        overlay_text="ocean ocean ocean waves beach sand sky clouds horizon",
        max_terms=3,
    )
    terms = q.replace(" aesthetic background", "").split()
    assert len(terms) == 3
    assert terms.count("ocean") == 1  # deduped


def test_derive_query_empty_when_no_content():
    assert backgrounds.derive_query("", "") == ""
    # Pure stopwords yield nothing searchable.
    assert backgrounds.derive_query("the and of to is", "") == ""


# ─── pick_sources ────────────────────────────────────────────────────────────


def test_pick_sources_pinterest_falls_back_to_web():
    chain = backgrounds.pick_sources("pinterest")
    assert [s.name for s in chain] == ["pinterest", "web"]


def test_pick_sources_web_only():
    chain = backgrounds.pick_sources("web")
    assert [s.name for s in chain] == ["web"]


def test_pick_sources_unknown_defaults_to_web():
    chain = backgrounds.pick_sources("nonsense")
    assert [s.name for s in chain] == ["web"]


# ─── Pinterest JSON extraction (no network — fixture shape) ─────────────────


def test_extract_pinterest_image_urls_prefers_orig():
    data = {
        "resource_response": {
            "data": {
                "results": [
                    {
                        "images": {
                            "474x": {"url": "https://i.pinimg.com/474x/a.jpg"},
                            "orig": {"url": "https://i.pinimg.com/orig/a.jpg"},
                        }
                    },
                    {"images": {"736x": {"url": "https://i.pinimg.com/736x/b.jpg"}}},
                ]
            }
        }
    }
    urls = backgrounds._extract_pinterest_image_urls(data, limit=5)
    assert urls == [
        "https://i.pinimg.com/orig/a.jpg",  # orig preferred over 474x
        "https://i.pinimg.com/736x/b.jpg",
    ]


def test_extract_pinterest_image_urls_handles_bad_shape():
    assert backgrounds._extract_pinterest_image_urls({}, limit=5) == []
    assert backgrounds._extract_pinterest_image_urls({"resource_response": "nope"}, limit=5) == []
    assert backgrounds._extract_pinterest_image_urls([1, 2, 3], limit=5) == []


def test_extract_pinterest_image_urls_respects_limit():
    results = [{"images": {"orig": {"url": f"https://x/{i}.jpg"}}} for i in range(10)]
    data = {"resource_response": {"data": {"results": results}}}
    assert len(backgrounds._extract_pinterest_image_urls(data, limit=3)) == 3


# ─── source contract: never raises on failure ──────────────────────────────


def test_sources_return_empty_on_blank_query():
    assert backgrounds.PinterestSource().search("") == []
    assert backgrounds.WebImageSource().search("") == []


def test_fetch_background_none_on_empty_query(tmp_path):
    assert backgrounds.fetch_background("", tmp_path / "bg.jpg", backgrounds.pick_sources("web")) is None


def test_fetch_background_none_when_all_sources_empty(tmp_path):
    """A source chain that finds nothing yields None, not an exception."""

    class EmptySource:
        name = "empty"

        def search(self, query, *, limit=5):
            return []

    out = backgrounds.fetch_background("waves", tmp_path / "bg.jpg", [EmptySource()])
    assert out is None
    assert not (tmp_path / "bg.jpg").exists()


# ─── ffmpeg composite ────────────────────────────────────────────────────────


def test_build_background_filter_shape():
    f = backgrounds.build_background_filter(1080, 1920)
    # Background scaled+cropped to frame, blurred, darkened; fg overlaid centered.
    assert "scale=1080:1920" in f
    assert "crop=1080:1920" in f
    assert "boxblur" in f
    assert "overlay=(W-w)/2:(H-h)/2[out]" in f
    # Foreground scaled to 78% width (1080 * 0.78 = 842, even).
    assert "scale=842:-2[fg]" in f


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not on PATH")
def test_composite_background_produces_video(tmp_path):
    # Synthetic foreground clip + synthetic background image.
    clip = tmp_path / "cut.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=teal:320x568:duration=2:rate=24", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    bg = tmp_path / "bg.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=orange:640x640:duration=1", "-frames:v", "1", str(bg)],
        check=True,
    )
    out = tmp_path / "composited.mp4"
    ok = backgrounds.composite_background(clip, bg, out, 320, 568)
    assert ok is True
    assert out.exists() and out.stat().st_size > 0
    # Output is a valid 320x568 video.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip().replace(" ", "") == "320,568"


def test_composite_background_returns_false_on_bad_input(tmp_path):
    ok = backgrounds.composite_background(
        tmp_path / "nope.mp4", tmp_path / "nope.jpg", tmp_path / "out.mp4", 320, 568
    )
    assert ok is False
