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


# ── yt-dlp flat-playlist caption-truncation rescue ───────────────────────────
# THE BUG: yt-dlp's --flat-playlist clips a video's caption to ~72 chars and
# appends a literal "...". When the brand hashtag sits at that boundary,
# `#befreed_0124` arrives as `#befree...`, _is_real_ugc_caption rejects it, and a
# genuine (often high-view) brand video is silently dropped. Fix: detect the clip
# signature, re-fetch the full caption from tikwm, and — if tikwm is throttled —
# KEEP the video rather than lose it. These guard both the detector and the
# keep-on-throttle behavior so the regression can't return.

def test_detects_truncated_brand_tag_prefix():
    # the exact clipped caption yt-dlp produced for the 2.6M purple video
    clipped = "If your favorite color is purple you need to tune in!! 💜 #befree..."
    assert tf._is_real_ugc_caption(clipped, "befreed") is False
    assert tf._caption_maybe_truncates_brand(clipped, "befreed") is True


def test_full_caption_needs_no_rescue():
    full = "tune in!! #befreed_0124 #purple #hobbiesinyour20s #greenscreen "
    assert tf._is_real_ugc_caption(full, "befreed") is True
    assert tf._caption_maybe_truncates_brand(full, "befreed") is False


def test_truncation_detector_has_no_false_positives():
    # genuinely-unrelated captions must NOT trigger a (wasteful) rescue
    for neg in [
        "If your favorite color is blue 💙 #1dfaf_blue #colorblue",
        "The books you read #dfff_disg #readingisfundamental",
        "a video about freedom and being free #motivation",  # 'free' != prefix of 'befreed'
        "check it out...",  # ellipsis but no brand bytes
        "loved this!! #b",  # fragment too short
    ]:
        assert tf._caption_maybe_truncates_brand(neg, "befreed") is False, neg


def _run_one_creator(monkeypatch, catalog, tikwm_return):
    """Drive _fetch_one_creator for a single synthetic creator with the yt-dlp
    walk and the tikwm rescue both stubbed. Returns the kept videos list."""
    import asyncio
    from datetime import datetime, timezone, timedelta

    monkeypatch.setattr(tf, "_ytdlp_creator_catalog", lambda h, max_retries=3: catalog)
    monkeypatch.setattr(tf, "_tikwm_video_caption", lambda vid, author="x": tikwm_return)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    videos: list = []
    seen: set = set()
    asyncio.run(tf._fetch_one_creator(None, "someone", videos, seen, cutoff, "befreed"))
    return videos


def _clipped_catalog():
    from datetime import datetime, timezone

    return [{
        "platform": "tiktok", "external_id": "PURPLE",
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "caption": "loved this!! #befree...", "thumbnail_url": "", "video_url": "",
        "view_count": 2600000, "like_count": 1, "comment_count": 0, "share_count": 0,
        "_author": "someone",
    }]


def test_rescue_keeps_video_when_tikwm_throttled(monkeypatch):
    # tikwm returns None (throttled). The video MUST survive (this is the bug).
    kept = _run_one_creator(monkeypatch, _clipped_catalog(), None)
    assert [v["external_id"] for v in kept] == ["PURPLE"]
    assert kept[0]["view_count"] == 2600000  # walk's correct count preserved


def test_rescue_keeps_and_upgrades_when_tikwm_confirms(monkeypatch):
    full = "loved this!! #befreed_0124 #purple"
    kept = _run_one_creator(monkeypatch, _clipped_catalog(), full)
    assert len(kept) == 1 and kept[0]["caption"] == full  # caption upgraded


def test_rescue_drops_only_on_confirmed_non_match(monkeypatch):
    # tikwm answers and the FULL caption genuinely has no brand → correct drop
    kept = _run_one_creator(monkeypatch, _clipped_catalog(), "loved this!! #freedom #free")
    assert kept == []


# ── merge enrichment: author backfill + brand-safe caption preference ─────────
# THE (unknown) BUG: the tikwm discovery feed sometimes yields a video with no
# author (the item had no author.unique_id), so the row lands author=NULL and
# renders "(unknown)" — even though the SAME video in the creator's yt-dlp walk
# carries _author. First-writer-wins dedup kept the blank copy. _upgrade_metrics
# now backfills identity from the authoritative walk WITHOUT clobbering a value
# discovery already had, and WITHOUT replacing a brand-tagged caption with a
# longer brand-LESS one (the flat-playlist walk truncates non-deterministically).

def test_merge_backfills_missing_author_from_walk():
    existing = {
        "external_id": "V", "_author": "", "author_handle": "",
        "view_count": 398200, "caption": "bc not everyone #befreed_0136",
        "video_url": "https://www.tiktok.com/video/V",
    }
    fresh = {
        "external_id": "V", "_author": "jacob.befreed", "view_count": 399300,
        "caption": "bc not everyone has time to read 300 pages",
        "video_url": "https://www.tiktok.com/@jacob.befreed/video/V",
    }
    videos = [existing]
    tf._merge_into_videos([[fresh]], videos, {"V"}, prefer_metrics=True, brand_tag="befreed")
    v = videos[0]
    assert v["_author"] == "jacob.befreed"
    assert v["author_handle"] == "jacob.befreed"
    assert v["view_count"] == 399300
    assert "/@jacob.befreed/" in v["video_url"]
    # the brand tag must survive — NOT be clobbered by the longer non-brand walk caption
    assert "#befreed_0136" in v["caption"]


def test_merge_does_not_clobber_existing_author():
    existing = {"external_id": "Z", "_author": "realauthor", "view_count": 1, "caption": "x"}
    tf._merge_into_videos(
        [[{"external_id": "Z", "_author": "wrong", "view_count": 2}]],
        [existing], {"Z"}, prefer_metrics=True, brand_tag="befreed",
    )
    assert existing["_author"] == "realauthor"


def test_merge_upgrades_to_fuller_caption_when_brand_kept():
    existing = {"external_id": "W", "_author": "x", "view_count": 1, "caption": "short #befreed"}
    tf._merge_into_videos(
        [[{"external_id": "W", "_author": "x", "view_count": 2,
           "caption": "a much longer caption that still has #befreed_0124 in it"}]],
        [existing], {"W"}, prefer_metrics=True, brand_tag="befreed",
    )
    assert "0124" in existing["caption"]


# ── pure-hashtag discovery: brand-tag name filter ────────────────────────────
# Replaces the noisy full-text keyword search. challenge/search matches loosely,
# so its result list mixes real brand tags with coincidental ones; _is_brand_hashtag
# filters at the NAME level. Leans inclusive (a false keep is filtered later by the
# per-video walk; a false reject permanently loses a creator).

def test_brand_hashtag_keeps_real_brand_tags():
    for name in [
        "befreed", "#befreed", "befreed_0124", "#befreed_0098",
        "usebefreed", "befreedaffirmations", "liamlucasbefreed_0001", "befreed🦋",
    ]:
        assert tf._is_brand_hashtag(name, "befreed") is True, name


def test_brand_hashtag_rejects_coincidental_tags():
    for name in [
        "befree", "freed", "beafraid", "be_afraid", "befearless",
        "befree_fashion", "random", "freedom",
        "befreedom",  # brand token + 'om' = the unrelated word, denylisted
    ]:
        assert tf._is_brand_hashtag(name, "befreed") is False, name


def test_brand_hashtag_campaign_code_boundary():
    # campaign codes (digits/underscore after brand) always qualify
    assert tf._is_brand_hashtag("befreed_0001", "befreed") is True
    assert tf._is_brand_hashtag("befreed0130", "befreed") is True
    # brand at end qualifies
    assert tf._is_brand_hashtag("xyzbefreed", "befreed") is True


def test_discover_all_brand_hashtags_dedups_and_scores(monkeypatch):
    # Stub the two network helpers: a challenge list with the main tag + 2
    # variants (one a dup id), and per-challenge creator sets.
    challenges = [
        ("befreed", "100"),
        ("befreed_0124", "200"),
        ("usebefreed", "300"),
    ]
    feeds = {
        "100": {"alice", "bob"},
        "200": {"alice", "carol"},  # alice in 2 challenges -> higher score
        "300": {"dave"},
    }
    monkeypatch.setattr(tf, "_tikwm_all_brand_challenges", lambda brand, search_pages=3: (challenges, False))
    # _tikwm_creators_in_challenge now returns (creators, throttled)
    monkeypatch.setattr(tf, "_tikwm_creators_in_challenge", lambda cid, pages: (feeds[cid], False))
    monkeypatch.setattr(tf, "_hashtag_feed_delay", lambda: 0.0)
    scores, relay_down = tf._tikwm_discover_all_brand_hashtags("befreed")
    assert relay_down is False
    assert scores["alice"] == 2  # surfaced in 2 brand challenges
    assert scores["bob"] == 1
    assert scores["carol"] == 1
    assert scores["dave"] == 1
    assert set(scores) == {"alice", "bob", "carol", "dave"}


def test_hashtag_sweep_aborts_on_consecutive_throttle(monkeypatch):
    # main tag returns creators fine, then two throttled feeds in a row → abort,
    # so the 4th challenge is never read.
    challenges = [("befreed", "1"), ("befreed_0124", "2"), ("usebefreed", "3"), ("befreed_0099", "4")]
    calls = []

    def fake_feed(cid, pages):
        calls.append(cid)
        if cid == "1":
            return {"alice", "bob"}, False
        if cid in ("2", "3"):
            return set(), True  # throttled
        return {"should_not_reach"}, False

    monkeypatch.setattr(tf, "_tikwm_all_brand_challenges", lambda brand, search_pages=3: (challenges, False))
    monkeypatch.setattr(tf, "_tikwm_creators_in_challenge", fake_feed)
    monkeypatch.setattr(tf, "_hashtag_feed_delay", lambda: 0.0)
    scores, relay_down = tf._tikwm_discover_all_brand_hashtags("befreed")
    # core roster from the main tag survives; the 4th challenge ("4") never read
    assert scores == {"alice": 1, "bob": 1}
    assert relay_down is True  # hard-fails must surface as a relay-down signal
    assert "4" not in calls


# ── _tikwm_get retry/backoff contract ─────────────────────────────────────────
# _tikwm_get encodes the fix for the nondeterministic 111-vs-29 discovery swings:
# dict only on a clean code==0 envelope, retry with backoff on connection error /
# timeout / non-zero envelope, None only after all retries. Every other test stubs
# ABOVE it; these execute the function itself so a regression (single-shot
# "simplification", inverted code check, returning the non-zero envelope) fails.

import json as _json
import time as _time
import urllib.request as _urlreq


class _FakeResp:
    """Context-manager response stub for urllib.request.urlopen."""

    def __init__(self, payload):
        self._body = _json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _scripted_urlopen(script, calls):
    """urlopen stub: pops the next step from `script`; an Exception raises,
    anything else is wrapped in _FakeResp. Records each call in `calls`."""

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _FakeResp(step)

    return fake_urlopen


def test_tikwm_get_retries_transients_then_returns_doc(monkeypatch):
    # connection error → non-zero envelope → clean doc: must return the doc.
    calls: list = []
    script = [
        ConnectionError("reset by peer"),
        {"code": 1, "msg": "throttle"},
        {"code": 0, "data": {"ok": True}},
    ]
    sleeps: list = []
    monkeypatch.setattr(_urlreq, "urlopen", _scripted_urlopen(script, calls))
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    doc = tf._tikwm_get("https://example.invalid/api")
    assert doc == {"code": 0, "data": {"ok": True}}
    assert len(calls) == 3  # exactly 3 attempts
    assert len(sleeps) == 2  # backoff between attempts, none after success


def test_tikwm_get_returns_none_after_all_retries(monkeypatch):
    calls: list = []
    script = [ConnectionError("a"), {"code": -1}, ConnectionError("b")]
    monkeypatch.setattr(_urlreq, "urlopen", _scripted_urlopen(script, calls))
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    assert tf._tikwm_get("https://example.invalid/api") is None
    assert len(calls) == 3  # all retries consumed before giving up


def test_tikwm_get_clean_first_try_is_single_call_no_sleep(monkeypatch):
    calls: list = []
    sleeps: list = []
    script = [{"code": 0, "data": {"videos": []}}]
    monkeypatch.setattr(_urlreq, "urlopen", _scripted_urlopen(script, calls))
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    doc = tf._tikwm_get("https://example.invalid/api")
    assert doc == {"code": 0, "data": {"videos": []}}
    assert len(calls) == 1 and sleeps == []


def test_tikwm_get_rejects_oversized_body(monkeypatch):
    # An over-cap body is a failed attempt (untrusted relay must not OOM us).
    class _HugeResp:
        def read(self, n=-1):
            size = tf.TIKWM_MAX_BODY + 1 if (n is None or n < 0) else n
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(_urlreq, "urlopen", lambda req, timeout=0: _HugeResp())
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    assert tf._tikwm_get("https://example.invalid/api", retries=1) is None


# ── _tikwm_creators_in_challenge: tolerance / cursor state machine ────────────
# The deterministic-coverage logic: TOLERANCE=2 consecutive-empty allowance, the
# stuck-cursor nudge, the self-advance on a stuck EMPTY page (without which the
# tolerance is dead code — tikwm echoes the cursor on blip pages), and the
# partial-set hard_fail return. All previously monkeypatched away by sweep tests.


def _challenge_page(handles, has_more=True, cursor=0):
    return {
        "code": 0,
        "data": {
            "videos": [{"author": {"unique_id": h}} for h in handles],
            "hasMore": has_more,
            "cursor": cursor,
        },
    }


def _scripted_tikwm_get(pages, calls):
    """tf._tikwm_get stub returning scripted pages in call order; records the
    requested cursor (parsed from the URL) per call."""
    import urllib.parse

    def fake_get(url, **kwargs):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(int(qs["cursor"][0]))
        return pages[len(calls) - 1]

    return fake_get


def test_challenge_walk_tolerates_single_empty_pages(monkeypatch):
    # [items, empty, items, empty, empty] → pages 1+3 collected, walk stops only
    # after TWO consecutive empties (the docstring's TOLERANCE promise).
    calls: list = []
    pages = [
        _challenge_page(["a1", "a2"], cursor=30),
        _challenge_page([], cursor=60),
        _challenge_page(["b1"], cursor=90),
        _challenge_page([], cursor=120),
        _challenge_page([], cursor=150),
    ]
    monkeypatch.setattr(tf, "_tikwm_get", _scripted_tikwm_get(pages, calls))
    found, hard_fail = tf._tikwm_creators_in_challenge("cid", pages=10)
    assert found == {"a1", "a2", "b1"}
    assert hard_fail is False
    assert len(calls) == 5  # stopped at the 2nd consecutive empty, not page 10


def test_challenge_walk_self_advances_on_stuck_empty_page(monkeypatch):
    # A transient empty page that ECHOES the cursor (tikwm's usual blip shape)
    # must not end the walk — the cursor self-advances and page 3 is collected.
    calls: list = []
    pages = [
        _challenge_page(["a"], cursor=30),
        _challenge_page([], cursor=30),  # empty AND cursor stuck at 30
        _challenge_page(["b"], has_more=False, cursor=60),
    ]
    monkeypatch.setattr(tf, "_tikwm_get", _scripted_tikwm_get(pages, calls))
    found, hard_fail = tf._tikwm_creators_in_challenge("cid", pages=10)
    assert found == {"a", "b"}
    assert hard_fail is False
    assert calls == [0, 30, 60]  # the blip page was skipped past, not fatal


def test_challenge_walk_nudges_stuck_cursor_with_items(monkeypatch):
    # A non-empty page whose cursor didn't advance: nudge past it and continue.
    calls: list = []
    pages = [
        _challenge_page(["a"], cursor=0),  # echoes cursor 0
        _challenge_page(["b"], has_more=False, cursor=0),
    ]
    monkeypatch.setattr(tf, "_tikwm_get", _scripted_tikwm_get(pages, calls))
    found, hard_fail = tf._tikwm_creators_in_challenge("cid", pages=10)
    assert found == {"a", "b"}
    assert hard_fail is False
    assert calls == [0, 1]  # nudged by len(items), kept walking


def test_challenge_walk_returns_partial_set_on_outage(monkeypatch):
    # _tikwm_get None (all retries failed) on page 2 → (page-1 creators, True),
    # NOT an empty set — partial coverage is kept, outage is reported.
    calls: list = []
    pages = [_challenge_page(["a1", "a2"], cursor=30), None]
    monkeypatch.setattr(tf, "_tikwm_get", _scripted_tikwm_get(pages, calls))
    found, hard_fail = tf._tikwm_creators_in_challenge("cid", pages=10)
    assert found == {"a1", "a2"}
    assert hard_fail is True


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
        finally:
            mp.undo()  # patches must not leak into the next test
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
