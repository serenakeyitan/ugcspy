"""Tests for scripts._log."""

from __future__ import annotations

import logging

import pytest

from scripts import _log


def test_get_logger_returns_same_instance() -> None:
    a = _log.get_logger("test-A")
    b = _log.get_logger("test-A")
    assert a is b


def _attach_caplog_handler(logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
    """Our loggers don't propagate (intentional, see _log.py docstring), so
    pytest's caplog fixture can't see their records. Attach caplog's handler
    explicitly so tests can assert on emitted records.
    """
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=logger.name)


def test_get_logger_emits_with_consistent_format(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = _log.get_logger("test-stderr")
    _attach_caplog_handler(logger, caplog)
    logger.info("hello world")
    assert any("hello world" in r.getMessage() for r in caplog.records)


def test_stage_logs_start_and_done(caplog: pytest.LogCaptureFixture) -> None:
    logger = _log.get_logger("test-stage")
    _attach_caplog_handler(logger, caplog)
    with _log.stage("test-stage"):
        pass
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m == "start" for m in msgs)
    assert any(m.startswith("done in") for m in msgs)


def test_stage_re_raises_and_logs_failure(caplog: pytest.LogCaptureFixture) -> None:
    logger = _log.get_logger("test-failing")
    _attach_caplog_handler(logger, caplog)
    with pytest.raises(RuntimeError, match="kaboom"), _log.stage("test-failing"):
        raise RuntimeError("kaboom")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("failed after" in m and "RuntimeError" in m and "kaboom" in m for m in msgs)


def test_logger_does_not_propagate(capsys: pytest.CaptureFixture[str]) -> None:
    """Our handler attaches once and the logger doesn't bubble to root.
    Important so a project-wide root-logger configuration in the harness
    doesn't double-print our messages.
    """
    logger = _log.get_logger("test-propagate")
    assert logger.propagate is False
    # And the level is INFO — debug shouldn't surface unless explicitly raised
    assert logger.level == logging.INFO


# --- SSL self-help (#46) -------------------------------------------------


def test_is_ssl_certificate_error_detects_ssl_error() -> None:
    import ssl

    err = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )
    assert _log.is_ssl_certificate_error(err) is True


def test_is_ssl_certificate_error_detects_via_message() -> None:
    """yt-dlp wraps SSL errors in a generic DownloadError; the canonical
    string should still match anywhere in the chain."""
    inner = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    outer = RuntimeError("yt-dlp could not resolve URL")
    outer.__cause__ = inner
    assert _log.is_ssl_certificate_error(outer) is True


def test_is_ssl_certificate_error_returns_false_for_unrelated() -> None:
    err = ValueError("nothing to do with SSL")
    assert _log.is_ssl_certificate_error(err) is False


def test_is_ssl_certificate_error_handles_circular_chain() -> None:
    """A pathological circular __cause__ chain shouldn't loop forever."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    # Neither contains the SSL string — should return False without hanging.
    assert _log.is_ssl_certificate_error(a) is False


def test_print_ssl_self_help_writes_remediation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = _log.get_logger("test-ssl-help")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR, logger="test-ssl-help")
    _log.print_ssl_self_help(logger)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "SSL_CERT_FILE" in msgs
    assert "certifi" in msgs
    assert "doctor" in msgs.lower()
