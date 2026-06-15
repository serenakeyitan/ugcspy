"""Unit tests for scripts/instagram_fetch.py (the IG fetch bridge).

Covers the pure mappers (RawVideo contract, date normalization, metric
preference) and the in-band error/JSON wire contract. No network, no browser
session: the gallery-dl / instaloader / cookie boundaries are not exercised here
(those are proven by the live E2E). Run:
    python3 -m pytest test/test_instagram_fetch.py
"""
import json
import sys
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


def test_iso_missing_or_unparseable_date_maps_to_epoch_not_now():
    # codex P2: a missing/unparseable date must NOT default to now() — that makes
    # an old partial row look freshly posted and can trip the 24h breakout filter.
    # It must map to the UNIX epoch (1970), which can never look fresh.
    for bad in (None, "", "not-a-date", "2026/06/15 weird"):
        out = ig._iso(bad)
        assert out.startswith("1970-01-01"), f"{bad!r} -> {out} (expected epoch)"
    # a real parseable date still works
    assert ig._iso("2026-06-14 03:31:05").startswith("2026-06-14T03:31:05")


def test_author_handle_strips_leading_at():
    rv = ig.to_raw_video({"shortcode": "X", "username": "@nike", "is_video": True})
    assert rv["author_handle"] == "nike"


def test_resolve_walk_limits_daemon_enriches_full_roster_tier_caps():
    # codex P2 (round 2): with NO explicit tier (the daemon poll), the enrich cap
    # must equal the walked roster — otherwise videos past the cap refresh likes
    # but NOT views, leaving a view-threshold watch on stale counts. An explicit
    # tier still caps for the interactive speed/depth tradeoff. Tests the REAL
    # resolve_walk_limits used by run_user (not a mirror).
    # daemon: no tier (None) → enrich == roster, so every tracked video's views refresh
    assert ig.resolve_walk_limits(None, None, 60) == (60, 60)
    # quick tier (15): roster still floored at 60, but enrich capped at 15
    assert ig.resolve_walk_limits(None, 15, 60) == (60, 15)
    # deep tier (100): roster grows to cover it, enrich 100
    assert ig.resolve_walk_limits(None, 100, 60) == (100, 100)
    # a non-positive/garbage tier is ignored (treated as no tier)
    assert ig.resolve_walk_limits(None, 0, 60) == (60, 60)
    assert ig.resolve_walk_limits(None, -5, 60) == (60, 60)


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


def test_is_throttle_recognizes_ig_rate_limit_signatures():
    # Real throttle signatures on the authenticated GraphQL endpoint.
    for msg in (
        "403 Forbidden when accessing https://www.instagram.com/graphql/query",
        "401 Unauthorized - Please wait a few minutes before you try again",
        "429 Too Many Requests",
        "HTTP 403: rate limit reached, try again later",
        # phrase-only, NO status digit — must still throttle (codex round-2):
        "Please wait a few minutes before you try again.",
        "You are temporarily blocked from doing this.",
        "rate limit exceeded",
    ):
        assert ig._is_throttle(Exception(msg)), msg


def test_is_throttle_does_not_false_positive(monkeypatch):
    # codex P2: precise matching — these must NOT be treated as throttle:
    #  - a dead post whose shortcode merely CONTAINS '401'
    #  - a bare expired-session 401 (that's a re-login problem, not a rate-limit)
    #  - unrelated errors
    assert not ig._is_throttle(Exception("Post ABC401xyz does not exist"))
    assert not ig._is_throttle(Exception("401 Unauthorized"))  # bare 401 = session, not throttle
    assert not ig._is_throttle(Exception("403 Forbidden"))  # bare 403 w/o context or graphql/query
    assert not ig._is_throttle(Exception("JSON decode error"))
    assert not ig._is_throttle(Exception("Profile nike does not exist"))


def test_enrich_views_backs_off_on_throttle(monkeypatch):
    # On the FIRST throttle, enrich_views must stop the whole run and report
    # throttled=True, leaving remaining posts un-enriched (no views_enriched flag)
    # so the caller reuses last-known view counts. No real network: stub the
    # instaloader factory + Post.from_shortcode to raise a 403 immediately.
    monkeypatch.setattr(ig, "ENRICH_SLEEP_S", 0)  # no real sleeping in the test

    class _FakePost:
        @staticmethod
        def from_shortcode(ctx, sc):
            raise Exception("403 Forbidden when accessing graphql/query")

    fake_instaloader = type("M", (), {"Post": _FakePost})()
    # L needs a .context attr (the code calls Post.from_shortcode(L.context, sc)).
    fake_L = type("L", (), {"context": object()})()
    monkeypatch.setattr(ig, "_make_instaloader", lambda cp: (fake_instaloader, fake_L))

    posts = [
        {"shortcode": "A", "is_video": True},
        {"shortcode": "B", "is_video": True},
        {"shortcode": "C", "is_video": True},
    ]
    out, enriched, throttled = ig.enrich_views(posts, "cookies.txt", max_enrich=10)
    assert throttled is True
    assert enriched == 0
    # None got a real view → none flagged views_enriched (caller keeps last-known)
    assert all(not p.get("views_enriched") for p in out)


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
