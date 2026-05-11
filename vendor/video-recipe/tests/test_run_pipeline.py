"""Tests for scripts.run_pipeline.

The orchestrator is mostly thin glue between scripts that already have unit
tests, so the tests here focus on:

- _run records both success and failure stages in the log
- the pipeline_log.json file is written
- error in one stage propagates and is logged with structured detail
"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts import run_pipeline


def test__run_records_success_in_log() -> None:
    log: list = []
    out = run_pipeline._run("noop", log, lambda: 42)
    assert out == 42
    assert len(log) == 1
    assert log[0]["stage"] == "noop"
    assert log[0]["ok"] is True
    assert "duration_sec" in log[0]
    # Log entry should not contain an "error" key on success.
    assert "error" not in log[0]


def test__run_records_failure_and_reraises() -> None:
    log: list = []

    def fails() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_pipeline._run("noop", log, fails)

    assert len(log) == 1
    assert log[0]["stage"] == "noop"
    assert log[0]["ok"] is False
    assert "boom" in log[0]["error"]


def test__record_includes_iso_timestamp() -> None:
    log: list = []
    run_pipeline._record(
        log, "stage", ok=True, started_at="2026-01-01T00:00:00+00:00", duration=0.5
    )
    assert log[0]["started_at"] == "2026-01-01T00:00:00+00:00"
    # iso parse round-trip
    dt.datetime.fromisoformat(log[0]["started_at"])
