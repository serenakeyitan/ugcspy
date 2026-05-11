"""Tests for scripts.doctor."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import doctor


def test_python_version_passes_on_modern_python() -> None:
    ok, label, _ = doctor._check_python_version()
    assert ok is True
    assert "Python" in label


def test_check_binary_returns_false_for_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, label, hint = doctor._check_binary("definitely-not-a-real-binary", "install hint here")
    assert ok is False
    assert "install hint here" in hint
    assert label == "definitely-not-a-real-binary"


def test_check_binary_returns_true_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    # subprocess will fail on a fake path; we still want ok=True for the binary check.
    ok, label, _ = doctor._check_binary("ffmpeg", "hint")
    assert ok is True
    assert label == "ffmpeg"


def test_check_python_package_returns_false_for_missing() -> None:
    ok, label, hint = doctor._check_python_package(
        "a_package_that_definitely_does_not_exist_42", "pip install foo"
    )
    assert ok is False
    assert "pip install foo" in hint


def test_check_python_package_returns_true_for_present() -> None:
    # sys is always importable
    ok, label, version = doctor._check_python_package("sys", "won't be used")
    assert ok is True
    assert label == "python: sys"


def test_check_disk_space_passes_with_plenty_of_space(tmp_path: Path) -> None:
    ok, _, detail = doctor._check_disk_space(tmp_path)
    # Reasonable assumption: dev environments have >2GB free.
    assert ok is True
    assert "GB free" in detail


def test_check_disk_space_handles_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    ok, _, hint = doctor._check_disk_space(missing)
    assert ok is False
    assert "could not stat" in hint


def test_check_network_returns_false_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the connection to fail to exercise the failure branch."""
    import socket as real_socket

    def fake_create_connection(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated unreachable")

    monkeypatch.setattr(real_socket, "create_connection", fake_create_connection)
    ok, _, hint = doctor._check_network("any-host", 443)
    assert ok is False
    assert "could not reach" in hint


def test_check_certifi_handles_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """When certifi isn't installed, the check fails with a useful hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "certifi":
            raise ImportError("simulated missing certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, _, hint = doctor._check_certifi_resolvable()
    assert ok is False
    assert "pip install certifi" in hint


def test_run_checks_returns_one_result_per_check() -> None:
    """Smoke test: run_checks doesn't crash and produces structured output."""
    results = doctor.run_checks()
    assert len(results) >= 8  # Python + ffmpeg + tesseract + py packages + certifi + disk + network
    for ok, label, _ in results:
        assert isinstance(ok, bool)
        assert isinstance(label, str)
        assert label  # non-empty


def test_format_result_renders_glyph_and_label() -> None:
    out = doctor.format_result((True, "thing", "42"))
    assert "✓" in out
    assert "thing" in out
    assert "42" in out

    out = doctor.format_result((False, "thing", "hint"))
    assert "✗" in out
    assert "thing" in out
    assert "hint" in out


def test_main_returns_zero_when_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", lambda: [(True, "a", ""), (True, "b", "")])
    rc = doctor.main([])
    assert rc == 0


def test_main_returns_one_when_any_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", lambda: [(True, "a", ""), (False, "b", "fix it")])
    rc = doctor.main([])
    assert rc == 1


def test_main_quiet_mode_only_prints_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor, "run_checks", lambda: [(True, "ok-thing", ""), (False, "broken-thing", "hint")]
    )
    doctor.main(["--quiet"])
    captured = capsys.readouterr()
    assert "broken-thing" in captured.out
    assert "ok-thing" not in captured.out
