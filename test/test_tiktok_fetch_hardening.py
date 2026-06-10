"""Hardening tests for scripts/tiktok_fetch.py (eng-hardening pass, 2026-06).

Covers the fail-soft contracts added around untrusted external data (tikwm
relay fields, yt-dlp subprocess output), the outage-vs-empty distinction in
hashtag discovery, snowball seed ordering/clamping, and env-tunable clamps.

No network, no venv: stdlib + pytest only. All HTTP/subprocess boundaries are
monkeypatched. Run with: python3 -m pytest test/test_tiktok_fetch_hardening.py
"""
import asyncio
import json
import sys
import time
import types
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import the bridge module (scripts/ is not a package; load by path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tiktok_fetch as tf  # noqa: E402


NOW_TS = int(datetime.now(timezone.utc).timestamp())
OLD_CUTOFF = datetime.now(timezone.utc) - timedelta(days=365)


# ── untrusted-value helpers ───────────────────────────────────────────────────

def test_as_int_parses_or_returns_none():
    assert tf._as_int(30) == 30
    assert tf._as_int("30") == 30
    assert tf._as_int("garbage") is None
    assert tf._as_int(None) is None
    assert tf._as_int([1]) is None


def test_safe_int_defaults_on_garbage():
    assert tf._safe_int("12") == 12
    assert tf._safe_int("lots", default=0) == 0
    assert tf._safe_int(None, default=7) == 7


def test_safe_ts_rejects_garbage_and_out_of_range():
    assert tf._safe_ts(NOW_TS) is not None
    assert tf._safe_ts("not-a-ts") is None
    assert tf._safe_ts(10**18) is None  # out-of-range epoch must not raise


def test_tikwm_item_with_garbage_create_time_dropped_not_crash():
    item = {"video_id": "B", "create_time": "soon", "author": {"unique_id": "b"}}
    assert tf._tikwm_item_to_raw(item) is None


def test_ytdlp_entry_with_garbage_timestamp_dropped_not_crash():
    assert tf._ytdlp_entry_to_raw({"id": "x", "timestamp": "soon"}, "h") is None


def test_ytdlp_entry_with_garbage_counts_defaults_to_zero():
    e = {"id": "x", "timestamp": NOW_TS, "view_count": "lots"}
    raw = tf._ytdlp_entry_to_raw(e, "h")
    assert raw is not None and raw["view_count"] == 0


def test_mappers_drop_non_dict_items():
    # a hostile/malformed array element (string, null) drops, never crashes
    assert tf._tikwm_item_to_raw("garbage") is None
    assert tf._tikwm_item_to_raw(None) is None
    assert tf._ytdlp_entry_to_raw("garbage", "h") is None
    assert tf._ytdlp_entry_to_raw(None, "h") is None


# ── keyword mode: retry client + cursor guards (the 'never crashes' contract) ─

def test_fetch_page_routes_through_retry_client(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return {"code": 0, "data": {"videos": []}}

    monkeypatch.setattr(tf, "_tikwm_get", fake_get)
    data = tf._tikwm_fetch_page("kw", 0)
    assert data == {"videos": []}
    assert len(calls) == 1 and "feed/search" in calls[0]


def test_run_keyword_treats_garbage_cursor_as_end_of_feed(monkeypatch, capsys):
    page = {
        "videos": [{"video_id": "V1", "create_time": NOW_TS, "author": {"unique_id": "a"}}],
        "hasMore": True,
        "cursor": "garbage",  # hostile/malformed relay cursor
    }
    calls = []

    def fake_page(kw, cursor):
        calls.append(cursor)
        return page

    monkeypatch.setattr(tf, "_tikwm_fetch_page", fake_page)
    tf.run_keyword("x", 30)  # must not raise ValueError
    out = json.loads(capsys.readouterr().out)
    assert [v["external_id"] for v in out] == ["V1"]
    assert calls == [0]  # garbage cursor ends paging, doesn't loop or crash


def test_run_keyword_drops_malformed_item_keeps_rest(monkeypatch, capsys):
    page = {
        "videos": [
            {"video_id": "B", "create_time": "not-a-ts", "author": {"unique_id": "b"}},
            {"video_id": "G", "create_time": NOW_TS, "author": {"unique_id": "g"}},
        ],
        "hasMore": False,
        "cursor": 0,
    }
    monkeypatch.setattr(tf, "_tikwm_fetch_page", lambda kw, cursor: page if cursor == 0 else None)
    tf.run_keyword("x", 30)
    out = json.loads(capsys.readouterr().out)
    assert [v["external_id"] for v in out] == ["G"]


# ── hashtag mode: outage must not masquerade as an authoritative empty ────────

def test_hashtag_outage_with_zero_creators_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("UGCSPY_USE_CHROMIUM", raising=False)
    monkeypatch.setattr(
        tf, "_tikwm_discover_all_brand_hashtags", lambda tag, **kw: ({}, True)
    )
    raised = None
    try:
        asyncio.run(tf.run_hashtag("befreed", 30))
    except SystemExit as e:
        raised = e
    assert raised is not None and raised.code != 0
    out = capsys.readouterr().out
    assert "error" in json.loads(out.strip().splitlines()[-1])


def test_hashtag_genuinely_empty_brand_stays_clean_success(monkeypatch, capsys):
    monkeypatch.delenv("UGCSPY_USE_CHROMIUM", raising=False)
    monkeypatch.setattr(
        tf, "_tikwm_discover_all_brand_hashtags", lambda tag, **kw: ({}, False)
    )
    asyncio.run(tf.run_hashtag("befreed", 30))  # no SystemExit
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1]) == []


def test_snowball_seeds_passed_strongest_first(monkeypatch, capsys):
    # The 60-seed cap inside _tikwm_snowball_creators slices the FRONT of this
    # list, so order must be signal rank — not arbitrary dict/hash order.
    monkeypatch.delenv("UGCSPY_USE_CHROMIUM", raising=False)
    monkeypatch.setattr(
        tf,
        "_tikwm_discover_all_brand_hashtags",
        lambda tag, **kw: ({"low": 1, "high": 5, "mid": 3}, False),
    )
    received = {}

    def fake_snowball(seeds, max_seeds=60):
        received["seeds"] = list(seeds)
        return {}

    monkeypatch.setattr(tf, "_tikwm_snowball_creators", fake_snowball)
    monkeypatch.setattr(tf, "_max_seed_creators", lambda: 0)  # skip the walk
    asyncio.run(tf.run_hashtag("befreed", 30))
    capsys.readouterr()
    assert received["seeds"] == ["high", "mid", "low"]


# ── snowball: worker clamp + retry-once on a Cloudflare blip ──────────────────

def test_snowball_worker_env_is_clamped_and_completes(monkeypatch):
    # UGCSPY_SNOWBALL_WORKERS=99 must be clamped (to <=8) and the pool must
    # complete without network when every seed fails to resolve.
    monkeypatch.setenv("UGCSPY_SNOWBALL_WORKERS", "99")
    monkeypatch.delenv("UGCSPY_SNOWBALL_PAGES", raising=False)
    monkeypatch.setattr(tf, "_tikwm_user_id", lambda seed: None)
    assert tf._tikwm_snowball_creators(["a", "b", "c"]) == {}


def test_snowball_retry_recovers_seed_after_one_blip(monkeypatch):
    # /following fails once (throttle blip) then succeeds — the seed's
    # followings must still be collected (retry-once contract).
    monkeypatch.delenv("UGCSPY_SNOWBALL_WORKERS", raising=False)
    monkeypatch.delenv("UGCSPY_SNOWBALL_PAGES", raising=False)
    monkeypatch.setattr(tf, "_tikwm_user_id", lambda seed: "123")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    attempts = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode("utf-8")

        def read(self, n=-1):
            return self._body if n is None or n < 0 else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=0):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("cloudflare blip")
        return _Resp({
            "code": 0,
            "data": {
                "followings": [{"unique_id": "tail1"}, {"unique_id": "tail2"}],
                "hasMore": False,
            },
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    scores = tf._tikwm_snowball_creators(["seed1"])
    assert scores == {"tail1": 1, "tail2": 1}
    assert attempts["n"] == 2


def test_tikwm_user_id_uses_retry_client(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return {"code": 0, "data": {"user": {"id": 42}}}

    monkeypatch.setattr(tf, "_tikwm_get", fake_get)
    assert tf._tikwm_user_id("@someone") == "42"
    assert len(calls) == 1 and "user/info" in calls[0][0]
    monkeypatch.setattr(tf, "_tikwm_get", lambda url, **kw: None)
    assert tf._tikwm_user_id("@someone") is None


# ── yt-dlp subprocess hardening ───────────────────────────────────────────────

def test_ytdlp_subprocess_pins_utf8_with_replace(monkeypatch):
    # text=True alone decodes with the LOCALE encoding (strict) — emoji captions
    # on a latin-1 LANG raised UnicodeDecodeError and killed the whole search.
    import subprocess

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"entries": []}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tf._ytdlp_creator_catalog("someone") == []
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


def test_ytdlp_non_dict_json_is_failed_attempt_not_crash(monkeypatch, capsys):
    import subprocess

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps([1, 2]), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert tf._ytdlp_creator_catalog("someone", max_retries=2) == []
    assert "non-object JSON" in capsys.readouterr().err


def test_ytdlp_total_failure_is_diagnosable_on_stderr(monkeypatch, capsys):
    # A broken/blocked yt-dlp must NOT return [] silently — user mode would
    # report an empty catalog as a clean success with zero diagnostics.
    import subprocess

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="blocked by tiktok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert tf._ytdlp_creator_catalog("someone", max_retries=2) == []
    err = capsys.readouterr().err
    assert "@someone" in err and "blocked by tiktok" in err


def test_fetch_one_creator_swallows_walk_crash(monkeypatch, capsys):
    # Docstring contract: per-creator failures are swallowed. A crash in the
    # walk (e.g. the old UnicodeDecodeError) must not propagate to the gather.
    def boom(handle, max_retries=3):
        raise UnicodeDecodeError("ascii", b"x", 0, 1, "locale decode quirk")

    monkeypatch.setattr(tf, "_ytdlp_creator_catalog", boom)
    videos: list = []
    asyncio.run(tf._fetch_one_creator(None, "someone", videos, set(), OLD_CUTOFF, "befreed"))
    assert videos == []
    assert "someone" in capsys.readouterr().err


# ── env tunable clamps ────────────────────────────────────────────────────────

def test_env_tunables_are_clamped_to_sane_maxima(monkeypatch):
    monkeypatch.setenv("UGCSPY_WALK_CONCURRENCY", "9999")
    assert tf._creator_walk_concurrency() == 64
    monkeypatch.setenv("UGCSPY_MAX_SEED_CREATORS", "999999")
    assert tf._max_seed_creators() == 2000
    monkeypatch.setenv("UGCSPY_YTDLP_RESCUE", "9999")
    assert tf._ytdlp_rescue_budget() == 200


def test_env_tunable_defaults_unchanged(monkeypatch):
    for var in ("UGCSPY_WALK_CONCURRENCY", "UGCSPY_MAX_SEED_CREATORS", "UGCSPY_YTDLP_RESCUE"):
        monkeypatch.delenv(var, raising=False)
    assert tf._creator_walk_concurrency() == 16
    assert tf._max_seed_creators() == 200
    assert tf._ytdlp_rescue_budget() == 25


# ── campaign codes: hashtag names are literal strings ─────────────────────────

def test_campaign_codes_kept_verbatim_not_zero_padded():
    videos = [{"caption": "loved it #befreed_117 and #befreed_0124"}]
    codes = tf._discover_campaign_codes(videos, "befreed")
    assert "117" in codes and "0124" in codes
    assert "0117" not in codes  # zfill corrupted 2-3 digit codes into fake tags


# ── truncation rescue: brand-EXTENDING clipped fragments ──────────────────────

def test_truncation_rescue_covers_brand_extending_clips():
    # '#befreed_0124' clipped at the underscore / '#befreedapp' clipped mid-word
    # fail _is_real_ugc_caption but ARE truncations of accepted forms.
    for cap in ["great app #befreed_", "great app #befreeda", "great app #befreedap"]:
        assert tf._is_real_ugc_caption(cap, "befreed") is False, cap
        assert tf._caption_maybe_truncates_brand(cap, "befreed") is True, cap


def test_truncation_rescue_still_rejects_unrelated_extensions():
    # '#befreedom' is the unrelated word, not a clipped campaign tag.
    long_tail = " on liberty and life choices in your twenties, a longer essay caption"
    assert (
        tf._caption_maybe_truncates_brand("thoughts" + long_tail + " #befreedom", "befreed")
        is False
    )


# ── _create_api: failed session bootstrap must close the browser ──────────────

def test_create_api_closes_entered_context_when_sessions_fail(monkeypatch):
    events = []

    class FakeApi:
        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *args):
            events.append("exit")

        async def create_sessions(self, **kwargs):
            events.append("create")
            raise RuntimeError("No valid sessions found")

    fake_mod = types.ModuleType("TikTokApi")
    fake_mod.TikTokApi = FakeApi
    monkeypatch.setitem(sys.modules, "TikTokApi", fake_mod)
    monkeypatch.delenv("MS_TOKEN", raising=False)
    raised = None
    try:
        asyncio.run(tf._create_api())
    except RuntimeError as e:
        raised = e
    assert raised is not None
    # the already-entered context (headful Chromium) was closed, not leaked
    assert events == ["enter", "create", "exit"]
