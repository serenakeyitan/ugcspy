"""Unit tests for scripts/instagram_fetch.py (the IG fetch bridge).

Covers the pure mappers (RawVideo contract, date normalization, metric
preference) and the in-band error/JSON wire contract. No network, no browser
session: the gallery-dl / instaloader / cookie boundaries are not exercised here
(those are proven by the live E2E). Run:
    python3 -m pytest test/test_instagram_fetch.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# scripts/ is not a package; load by path (same as the tiktok bridge tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import instagram_fetch as ig  # noqa: E402


def test_to_raw_video_emits_the_full_contract():
    post = {
        "shortcode": "DZjR_dosb9x",
        "likes": 634000,
        "comments": 6897,
        "caption": "Sleep well, NY.",
        "video_url": "https://scontent.cdninstagram.com/clip.mp4",
        "thumbnail_url": "https://scontent.cdninstagram.com/cover.jpg",
        "username": "nike",
        "date": "2026-06-14 03:31:05",
        "view_count": 12700000,
        "is_video": True,
    }
    rv = ig.to_raw_video(post)
    # Every RawVideo field the TS layer expects must be present.
    for key in (
        "platform", "external_id", "posted_at", "caption", "thumbnail_url",
        "video_url", "view_count", "like_count", "comment_count", "share_count",
        "author_handle",
    ):
        assert key in rv, f"missing {key}"
    assert rv["platform"] == "instagram"
    assert rv["external_id"] == "DZjR_dosb9x"
    assert rv["view_count"] == 12700000
    assert rv["like_count"] == 634000
    assert rv["comment_count"] == 6897
    assert rv["share_count"] == 0  # IG never exposes shares
    assert rv["author_handle"] == "nike"


def test_to_raw_video_falls_back_to_reel_url_when_no_media_url():
    rv = ig.to_raw_video({"shortcode": "ABC123", "is_video": True})
    assert rv["video_url"] == "https://www.instagram.com/reel/ABC123/"
    assert rv["view_count"] == 0  # un-enriched → 0, not crash
    assert rv["author_handle"] is None  # empty username → None, not ""


def test_iso_normalizes_gallery_dl_datetime():
    out = ig._iso("2026-06-14 03:31:05")
    assert out.startswith("2026-06-14T03:31:05")


def test_iso_handles_epoch_and_missing():
    epoch = ig._iso(1700000000)
    assert epoch.startswith("2023-")  # a real date, not a crash
    # missing date → a valid current ISO timestamp
    now = ig._iso(None)
    datetime.fromisoformat(now.replace("Z", "+00:00"))  # parses without raising


def test_author_handle_strips_leading_at():
    rv = ig.to_raw_video({"shortcode": "X", "username": "@nike", "is_video": True})
    assert rv["author_handle"] == "nike"


def test_fail_emits_inband_error_and_exits_zero(capsys):
    # The bridge reports errors as in-band JSON {error,code} and exits 0 so the
    # TS layer parses them (never a nonzero crash the parser can't read).
    import pytest
    with pytest.raises(SystemExit) as ex:
        ig._fail("nope", "re_login_required")
    assert ex.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "nope"
    assert out["code"] == "re_login_required"


def test_unsupported_mode_is_rejected(monkeypatch, capsys):
    import pytest
    monkeypatch.setattr("sys.stdin", _FakeStdin('{"mode":"trending"}'))
    with pytest.raises(SystemExit):
        ig.main()
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "bad_request"
    assert "trending" in out["error"]


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
