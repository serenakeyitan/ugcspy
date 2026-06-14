"""Tests for the bridge's snowball mode (scripts/tiktok_fetch.py run_snowball).

run_snowball exposes the follow-graph similarity walk as a standalone call
seeded by USER-supplied creators. It wraps _tikwm_snowball_creators (tested
indirectly via the live HTTP elsewhere); here we lock the OUTPUT CONTRACT that
the bridge owns: seed normalization (strip @, lowercase, dedupe), excluding the
seeds from their own recommendations, the sort order, the {creators,seedResults}
envelope, and the per-seed readability statuses that power the hit-rate.

The follow-graph engine itself is monkeypatched — no network. Run:
  python3 -m pytest test/test_snowball_mode.py
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tiktok_fetch as tf  # noqa: E402


def _run(monkeypatch, capsys, seeds, graph, diag=None):
    """Run run_snowball with _tikwm_snowball_creators stubbed to return `graph`
    ({handle: seedsFollowingCount}) and, if `diag` is given, to populate the
    seed_diag out-param with it ({seed: status}). Returns (parsed_envelope, captured)."""
    captured = {}

    def fake_snowball(seed_handles, max_seeds=60, seed_diag=None):
        captured["seeds"] = list(seed_handles)
        if seed_diag is not None and diag is not None:
            seed_diag.update(diag)
        return graph

    monkeypatch.setattr(tf, "_tikwm_snowball_creators", fake_snowball)
    tf.run_snowball(seeds)
    return json.loads(capsys.readouterr().out), captured


def test_basic_shape_and_sort(capsys, monkeypatch):
    out, _ = _run(monkeypatch, capsys, ["@a", "@b"], {"carol": 2, "dave": 1, "erin": 3})
    # Envelope with creators sorted by seedsFollowing desc, then handle asc.
    assert out["creators"] == [
        {"handle": "erin", "seedsFollowing": 3},
        {"handle": "carol", "seedsFollowing": 2},
        {"handle": "dave", "seedsFollowing": 1},
    ]
    assert "seedResults" in out


def test_ties_break_alphabetically(capsys, monkeypatch):
    out, _ = _run(monkeypatch, capsys, ["a"], {"zoe": 1, "amy": 1, "bob": 1})
    assert [r["handle"] for r in out["creators"]] == ["amy", "bob", "zoe"]


def test_seeds_excluded_from_their_own_recommendations(capsys, monkeypatch):
    # A seed that shows up in the graph (seeds often follow each other) must not
    # be recommended back to the user.
    out, _ = _run(monkeypatch, capsys, ["@alice", "BOB"], {"alice": 5, "bob": 4, "carol": 2})
    assert [r["handle"] for r in out["creators"]] == ["carol"]


def test_seeds_are_normalized_before_the_walk(capsys, monkeypatch):
    # Strip @, lowercase, dedupe — and a duplicate seed must not be walked twice
    # (the score is seed-count; double-walking would double-count followings).
    _, captured = _run(monkeypatch, capsys, ["@Alice", "alice", "  BOB  ", "@bob"], {})
    assert captured["seeds"] == ["alice", "bob"]


def test_empty_graph_emits_empty_creators_not_a_crash(capsys, monkeypatch):
    # The common real case: every seed's following list is private/blocked.
    out, _ = _run(monkeypatch, capsys, ["a", "b"], {})
    assert out["creators"] == []


def test_seed_results_carry_readability_statuses(capsys, monkeypatch):
    # The hit-rate signal: a follow-count on success, -1 blocked, -2 unresolved.
    out, _ = _run(
        monkeypatch, capsys, ["@alice", "bob", "carol"],
        {"x": 1},
        diag={"alice": 12, "bob": -1, "carol": -2},
    )
    by = {r["handle"]: r["status"] for r in out["seedResults"]}
    assert by == {"alice": 12, "bob": -1, "carol": -2}


def test_no_valid_seeds_is_an_error_envelope(capsys, monkeypatch):
    # Blank/@-only seeds normalize away to nothing — run_snowball's own guard
    # fails out (fail() raises SystemExit) with an error envelope on stdout.
    monkeypatch.setattr(tf, "_tikwm_snowball_creators", lambda s, max_seeds=60, seed_diag=None: {})
    with pytest.raises(SystemExit):
        tf.run_snowball(["@", "   ", ""])
    out = json.loads(capsys.readouterr().out)
    assert "error" in out and "seed" in out["error"].lower()


def test_dispatch_snowball_mode(capsys, monkeypatch):
    import io

    monkeypatch.setattr(
        tf, "_tikwm_snowball_creators", lambda s, max_seeds=60, seed_diag=None: {"x": 2}
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"mode": "snowball", "seeds": ["@seed"]}))
    )
    tf.main()
    out = json.loads(capsys.readouterr().out)
    assert out["creators"] == [{"handle": "x", "seedsFollowing": 2}]


def test_dispatch_snowball_missing_seeds_fails(capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"mode": "snowball", "seeds": []})))
    with pytest.raises(SystemExit):
        tf.main()
    out = json.loads(capsys.readouterr().out)
    assert "error" in out and "seed" in out["error"].lower()
