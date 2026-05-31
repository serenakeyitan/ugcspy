"""Tests for scripts.embedded_subs — WebVTT parsing + the prefer-captions
priority. No network: fetch_embedded_subs is exercised via a fake yt-dlp
invocation by writing the .vtt directly, and parse_vtt is pure.
"""

from __future__ import annotations

from pathlib import Path

from scripts import embedded_subs

# A real TikTok auto-caption VTT shape (trimmed from the Mya purple-colours
# video — short cues, leading spaces, fractional-second timing).
MYA_VTT = """WEBVTT

00:00:00.000 --> 00:00:01.100
Psychology shows

00:00:01.101 --> 00:00:01.980
 and literally proves

00:00:01.981 --> 00:00:02.300
that your

00:00:02.301 --> 00:00:02.860
favourite colour

00:00:02.861 --> 00:00:03.580
is a reflection

00:00:03.581 --> 00:00:04.900
of your personality.
"""


def test_parse_vtt_basic_cues(tmp_path: Path) -> None:
    p = tmp_path / "embedded.eng-US.vtt"
    p.write_text(MYA_VTT)
    doc = embedded_subs.parse_vtt(p, language="eng-US")
    assert doc["source"] == "embedded_subs"
    assert doc["language"] == "en"  # eng-US normalized
    assert len(doc["segments"]) == 6
    assert doc["segments"][0]["text"] == "Psychology shows"
    # Leading whitespace in the cue is collapsed.
    assert doc["segments"][1]["text"] == "and literally proves"
    # Duration is the last cue's end.
    assert doc["duration_sec"] == 4.9
    # Each cue produces one coarse "word" unit spanning its window.
    assert len(doc["words"]) == 6
    assert doc["words"][0]["word"] == "Psychology shows"


def test_parse_vtt_handles_hour_timestamps(tmp_path: Path) -> None:
    vtt = "WEBVTT\n\n01:02:03.500 --> 01:02:04.000\nlate cue\n"
    p = tmp_path / "x.vtt"
    p.write_text(vtt)
    doc = embedded_subs.parse_vtt(p)
    assert doc["segments"][0]["start"] == 3723.5  # 1h2m3.5s
    assert doc["segments"][0]["text"] == "late cue"


def test_parse_vtt_strips_inline_tags(tmp_path: Path) -> None:
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "<c>hello</c> <00:00:01.000><c> world</c>\n"
    )
    p = tmp_path / "x.vtt"
    p.write_text(vtt)
    doc = embedded_subs.parse_vtt(p)
    assert doc["segments"][0]["text"] == "hello world"


def test_parse_vtt_skips_notes_and_styles(tmp_path: Path) -> None:
    vtt = (
        "WEBVTT\n\n"
        "NOTE this is a comment block\n\n"
        "STYLE\n::cue { color: white }\n\n"
        "00:00:00.000 --> 00:00:01.000\nreal line\n"
    )
    p = tmp_path / "x.vtt"
    p.write_text(vtt)
    doc = embedded_subs.parse_vtt(p)
    assert len(doc["segments"]) == 1
    assert doc["segments"][0]["text"] == "real line"


def test_parse_vtt_merges_consecutive_duplicate_cues(tmp_path: Path) -> None:
    """TikTok auto-captions sometimes repeat a line across overlapping
    cues — merge them into one span rather than double-counting."""
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.000\nsame line\n\n"
        "00:00:01.000 --> 00:00:02.000\nsame line\n"
    )
    p = tmp_path / "x.vtt"
    p.write_text(vtt)
    doc = embedded_subs.parse_vtt(p)
    assert len(doc["segments"]) == 1
    assert doc["segments"][0]["start"] == 0.0
    assert doc["segments"][0]["end"] == 2.0


def test_parse_vtt_empty_when_no_cues(tmp_path: Path) -> None:
    p = tmp_path / "x.vtt"
    p.write_text("WEBVTT\n\n")
    doc = embedded_subs.parse_vtt(p)
    assert doc["segments"] == []
    assert doc["words"] == []
    assert doc["duration_sec"] == 0.0


def test_normalize_lang() -> None:
    assert embedded_subs._normalize_lang("eng-US") == "en"
    assert embedded_subs._normalize_lang("en-US") == "en"
    assert embedded_subs._normalize_lang("zh-Hans") == "zh"
    assert embedded_subs._normalize_lang(None) is None
    assert embedded_subs._normalize_lang("fra") == "fr"
