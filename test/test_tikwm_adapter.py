"""Unit tests for the tikwm -> RawVideo adapter in scripts/tiktok_fetch.py.

No network: feeds a recorded tikwm /api/feed/search item shape through the
mapper and asserts the RawVideo contract. Run with: python3 -m pytest test/test_tikwm_adapter.py
(or python3 test/test_tikwm_adapter.py for the inline runner).
"""
import sys
from pathlib import Path

# Import the bridge module (scripts/ is not a package; load by path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tiktok_fetch as tf  # noqa: E402


# A recorded tikwm /api/feed/search item (trimmed to the fields we map).
SAMPLE_ITEM = {
    "video_id": "7640930255158512927",
    "create_time": 1774999971,
    "title": "Skincare times #koreanskincare #skincareroutine",
    "origin_cover": "https://example.invalid/cover.jpg",
    "play_count": 1161384,
    "digg_count": 52394,
    "comment_count": 177,
    "share_count": 1778,
    "author": {"unique_id": "actuallykatiexyi"},
}


def test_maps_all_rawvideo_fields():
    raw = tf._tikwm_item_to_raw(SAMPLE_ITEM)
    assert raw is not None
    assert raw["platform"] == "tiktok"
    assert raw["external_id"] == "7640930255158512927"
    assert raw["view_count"] == 1161384
    assert raw["like_count"] == 52394
    assert raw["comment_count"] == 177
    assert raw["share_count"] == 1778
    assert raw["caption"].startswith("Skincare times")
    assert raw["_author"] == "actuallykatiexyi"
    # video_url uses the creator handle
    assert "@actuallykatiexyi/video/7640930255158512927" in raw["video_url"]
    # posted_at is ISO 8601
    assert raw["posted_at"].startswith("2026-")


def test_returns_none_on_missing_id_or_time():
    assert tf._tikwm_item_to_raw({"create_time": 0, "video_id": ""}) is None
    assert tf._tikwm_item_to_raw({"create_time": 1774999971}) is None  # no id
    assert tf._tikwm_item_to_raw({"video_id": "x"}) is None  # no time


def test_handles_missing_metrics_without_crashing():
    item = {"video_id": "1", "create_time": 1774999971, "author": {"unique_id": "x"}}
    raw = tf._tikwm_item_to_raw(item)
    assert raw is not None
    # missing counts default to 0, not crash
    assert raw["view_count"] == 0
    assert raw["like_count"] == 0
    assert raw["caption"] == ""


def test_missing_author_still_maps():
    item = dict(SAMPLE_ITEM)
    del item["author"]
    raw = tf._tikwm_item_to_raw(item)
    assert raw is not None
    assert raw["_author"] == ""
    # falls back to bare video URL when no author
    assert "video/7640930255158512927" in raw["video_url"]


# ─── discovery: wide collection + signal scoring (the 38→1403 fix) ───

def _page(videos, has_more=False, cursor=0):
    return {"videos": videos, "hasMore": has_more, "cursor": cursor}


def _vid(handle, title=""):
    return {"author": {"unique_id": handle}, "title": title}


def test_discovery_is_wide_by_default(monkeypatch):
    # The bug: discovery dropped creators whose surfaced title lacked the brand.
    # Fix: default precise=False collects EVERY surfaced creator; the per-video
    # brand filter runs later in the yt-dlp coverage pass.
    page = _page([
        _vid("alice", "befreed changed my reading"),  # brand in title
        _vid("bob", "my morning routine"),             # NO brand in title
        _vid("carol", "study with me"),                # NO brand in title
    ])
    monkeypatch.setattr(tf, "_tikwm_fetch_page", lambda kw, cursor: page if cursor == 0 else None)
    found = tf._tikwm_discover_creators("befreed", ["befreed"], pages=1)
    # All three surfaced creators are kept — not just alice.
    assert set(found) == {"alice", "bob", "carol"}


def test_discovery_precise_mode_still_filters(monkeypatch):
    page = _page([
        _vid("alice", "befreed changed my reading"),
        _vid("bob", "my morning routine"),
    ])
    monkeypatch.setattr(tf, "_tikwm_fetch_page", lambda kw, cursor: page if cursor == 0 else None)
    found = tf._tikwm_discover_creators("befreed", ["befreed"], pages=1, precise=True)
    # precise=True keeps only the brand-title creator.
    assert found == ["alice"]


def test_scored_discovery_ranks_by_signal(monkeypatch):
    # alice surfaces twice (once with brand title), bob once. alice should outrank.
    page = _page([
        _vid("alice", "befreed is great"),  # +1 +2 brand bonus = 3
        _vid("bob", "random clip"),          # +1
        _vid("alice", "another clip"),       # +1  -> alice total 4
    ])
    monkeypatch.setattr(tf, "_tikwm_fetch_page", lambda kw, cursor: page if cursor == 0 else None)
    scores = tf._tikwm_discover_scored("befreed", ["befreed"], pages=1)
    assert scores["alice"] > scores["bob"]
    assert scores["alice"] >= 4 and scores["bob"] == 1


class _MonkeyPatch:
    """Tiny monkeypatch shim so the inline runner works without pytest."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    import inspect
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            print(f"  ✓ {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
