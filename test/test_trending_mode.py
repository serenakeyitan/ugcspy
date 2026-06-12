"""Tests for the bridge's trending mode (scripts/tiktok_fetch.py run_trending).

Covers the rotating-feed contract: repeated pulls + dedupe, the two-empty-
rounds drain stop, is_ad skipping, and the untrusted shapes that crashed the
first live run (author as a plain string on some feed/list rotations).

No network: _tikwm_get is monkeypatched. Run: python3 -m pytest test/test_trending_mode.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tiktok_fetch as tf  # noqa: E402

NOW = int(datetime.now(timezone.utc).timestamp())


def _item(vid, author="someone", views=1000, **extra):
    d = {
        "video_id": str(vid),
        "create_time": NOW,
        "title": f"caption {vid} #fyp",
        "author": {"unique_id": author},
        "play_count": views,
    }
    d.update(extra)
    return d


def _run(monkeypatch, pages, capsys):
    calls = {"n": 0}

    def fake_get(url, **kw):
        i = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        page = pages[i]
        return None if page is None else {"code": 0, "data": page}

    monkeypatch.setattr(tf, "_tikwm_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    tf.run_trending("US", 7)
    return json.loads(capsys.readouterr().out), calls["n"]


def test_rotating_pulls_dedupe_across_rounds(capsys, monkeypatch):
    out, _ = _run(monkeypatch, [
        [_item(1), _item(2)],
        [_item(2), _item(3)],  # 2 is a repeat — rotation overlap
        [],
        [],
    ], capsys)
    assert sorted(v["external_id"] for v in out) == ["1", "2", "3"]


def test_two_empty_rounds_drain_the_rotation(capsys, monkeypatch):
    out, calls = _run(monkeypatch, [
        [_item(1)],
        [],  # nothing fresh
        [],  # second consecutive dry round → stop
        [_item(99)],  # must never be reached
    ], capsys)
    assert [v["external_id"] for v in out] == ["1"]
    assert calls == 3


def test_is_ad_items_are_skipped(capsys, monkeypatch):
    out, _ = _run(monkeypatch, [
        [_item(1, is_ad=True), _item(2)],
        [],
        [],
    ], capsys)
    assert [v["external_id"] for v in out] == ["2"]


def test_author_as_plain_string_does_not_crash(capsys, monkeypatch):
    """Live failure on first run: some feed/list rotations carry author as a
    bare string, and the mapper's dict assumption crashed the whole pull."""
    out, _ = _run(monkeypatch, [
        [dict(_item(1), author="bare_string_handle"), _item(2)],
        [],
        [],
    ], capsys)
    assert len(out) == 2
    by_id = {v["external_id"]: v for v in out}
    assert by_id["1"]["_author"] == "bare_string_handle"


def test_scalar_data_envelope_is_an_empty_round_not_a_crash(capsys, monkeypatch):
    """Codex gate: {"code":0,"data":"temporarily unavailable"} crashed the
    documented fail-soft contract by calling .get() on a string."""
    out, _ = _run(monkeypatch, [
        [_item(1)],
        "temporarily unavailable",  # scalar data → empty round
        [],
    ], capsys)
    assert [v["external_id"] for v in out] == ["1"]


def test_relay_down_fails_soft_with_partial_results(capsys, monkeypatch):
    out, _ = _run(monkeypatch, [
        [_item(1)],
        None,  # _tikwm_get exhausted retries
    ], capsys)
    assert [v["external_id"] for v in out] == ["1"]


def test_rounds_env_is_clamped(monkeypatch):
    monkeypatch.setenv("UGCSPY_TRENDING_ROUNDS", "9999")
    assert tf._trending_rounds() == 30
    monkeypatch.setenv("UGCSPY_TRENDING_ROUNDS", "garbage")
    assert tf._trending_rounds() == 8


def test_dispatch_trending_mode(capsys, monkeypatch):
    import io

    monkeypatch.setattr(tf, "_tikwm_get", lambda url, **kw: {"code": 0, "data": [_item(7)]})
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"mode": "trending", "region": "us", "days": 7})))
    tf.main()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["external_id"] == "7"
