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


# ─── multi-image collage ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, (1, 1)),
        (2, (2, 1)),
        (3, (3, 1)),
        (4, (2, 2)),
        (5, (3, 2)),
        (6, (3, 2)),
        (9, (3, 3)),
    ],
)
def test_grid_dimensions(n, expected):
    assert backgrounds.grid_dimensions(n) == expected


def test_grid_dimensions_covers_n():
    # cols*rows must always be >= n so every image gets a cell.
    for n in range(1, 17):
        cols, rows = backgrounds.grid_dimensions(n)
        assert cols * rows >= n


def test_build_collage_filter_single_image_has_no_xstack():
    f = backgrounds.build_collage_filter(1080, 1920, 1)
    assert "xstack" not in f  # 1 tile degrades to a plain backdrop
    assert "[0:v]scale=842:-2[fg]" in f
    assert "overlay=(W-w)/2:(H-h)/2[out]" in f


def test_build_collage_filter_four_images_makes_2x2_grid():
    f = backgrounds.build_collage_filter(1080, 1920, 4)
    # 4 image inputs scaled to 540x960 cells (1080/2 x 1920/2).
    assert "[1:v]scale=540:960" in f
    assert "[4:v]scale=540:960" in f
    # xstack assembles 4 inputs in a 2x2 layout (row-major cell offsets).
    assert "xstack=inputs=4:layout=0_0|540_0|0_960|540_960" in f
    assert "[out]" in f


def test_build_collage_filter_two_images_side_by_side():
    f = backgrounds.build_collage_filter(1080, 1920, 2)
    assert "[1:v]scale=540:1920" in f  # 2 cols, 1 row -> 540 wide, full height
    assert "xstack=inputs=2:layout=0_0|540_0" in f


def test_build_collage_filter_rejects_zero():
    with pytest.raises(ValueError):
        backgrounds.build_collage_filter(1080, 1920, 0)


def test_fetch_backgrounds_dedupes_and_caps(tmp_path, monkeypatch):
    """fetch_backgrounds should collect up to `count` DISTINCT images across
    the source chain, deduping repeated URLs."""

    class FakeSource:
        name = "fake"

        def __init__(self, urls):
            self._urls = urls

        def search(self, query, *, limit=5):
            return self._urls

    # Two sources; second repeats one URL from the first (should dedupe).
    sources = [
        FakeSource(["https://x/a.jpg", "https://x/b.jpg"]),
        FakeSource(["https://x/b.jpg", "https://x/c.jpg", "https://x/d.jpg"]),
    ]

    class FakeResp:
        status_code = 200
        content = b"\xff\xd8\xff\xe0fakejpeg"

    # backgrounds.py does a lazy `import requests` inside the function, so we
    # patch the real requests module's get (what the lazy import resolves to).
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    out = backgrounds.fetch_backgrounds("waves", tmp_path, sources, count=4, prefix="bg")
    # a, b, c, d — 4 distinct (the repeated b.jpg deduped, so we reach across
    # both sources to fill the grid).
    assert len(out) == 4
    names = sorted(p.name for p in out)
    assert names == ["bg-0.jpg", "bg-1.jpg", "bg-2.jpg", "bg-3.jpg"]


def test_fetch_backgrounds_empty_on_blank_query(tmp_path):
    assert backgrounds.fetch_backgrounds("", tmp_path, backgrounds.pick_sources("web")) == []


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not on PATH")
def test_composite_collage_background_4up(tmp_path):
    """Real ffmpeg: 4 distinct color tiles composite into a 2x2 grid behind
    the clip, output is a valid frame-sized video."""
    clip = tmp_path / "cut.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=teal:320x568:duration=2:rate=24", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    colors = ["red", "green", "blue", "yellow"]
    imgs = []
    for i, c in enumerate(colors):
        p = tmp_path / f"bg-{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"color={c}:400x400:duration=1", "-frames:v", "1", str(p)],
            check=True,
        )
        imgs.append(p)
    out = tmp_path / "collage.mp4"
    ok = backgrounds.composite_collage_background(clip, imgs, out, 320, 568)
    assert ok is True
    assert out.exists() and out.stat().st_size > 0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip().replace(" ", "") == "320,568"


def test_composite_collage_background_empty_list_returns_false(tmp_path):
    assert backgrounds.composite_collage_background(
        tmp_path / "clip.mp4", [], tmp_path / "out.mp4", 320, 568
    ) is False
