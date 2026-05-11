"""Shared logging + timing helpers.

Every script in this repo can `from scripts._log import stage` and use it
as a context manager:

    from scripts._log import stage
    with stage("download"):
        ...

That writes ``[download] start`` / ``[download] done in 4.2s`` to stderr.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

_FORMATTER = logging.Formatter("[%(name)s] %(message)s")
_HANDLER = logging.StreamHandler(sys.stderr)
_HANDLER.setFormatter(_FORMATTER)


def get_logger(name: str) -> logging.Logger:
    """Return a logger writing to stderr with a consistent prefix."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(_HANDLER)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Context manager that logs start/done with timing.

    Re-raises on exception, but logs a structured failure line first.
    """
    logger = get_logger(name)
    start = time.monotonic()
    logger.info("start")
    try:
        yield
    except Exception as e:
        elapsed = time.monotonic() - start
        logger.error("failed after %.2fs: %s: %s", elapsed, type(e).__name__, e)
        raise
    elapsed = time.monotonic() - start
    logger.info("done in %.2fs", elapsed)


def is_ssl_certificate_error(exc: BaseException) -> bool:
    """True iff ``exc`` (or any cause/context in its chain) is an SSL cert error.

    Covers both ``ssl.SSLCertVerificationError`` and the common case where
    yt-dlp / urllib wrap it inside a generic DownloadError or URLError. We
    walk the cause/context chain and look for the canonical CERTIFICATE
    string in any layer.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # ssl.SSLCertVerificationError is a subclass of ssl.SSLError; OSError
        # subclass on managed-cert systems also commonly gets the same
        # CERTIFICATE_VERIFY_FAILED message string.
        msg = str(current)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
            return True
        current = current.__cause__ or current.__context__
    return False


SSL_CERTIFI_HINT = (
    "SSL certificate verification failed. This is common on managed-cert\n"
    "systems (corporate proxies, some macOS configurations).\n"
    "\n"
    "Fix: tell Python where certifi's CA bundle lives, then re-run:\n"
    "\n"
    "    export SSL_CERT_FILE=\"$(python -c 'import certifi; print(certifi.where())')\"\n"
    "\n"
    "If the problem persists, your proxy may be re-signing TLS — you'll need\n"
    "your IT team's CA bundle. Run `python -m scripts.doctor` to verify."
)


def print_ssl_self_help(logger: logging.Logger | None = None) -> None:
    """Print the certifi remediation hint to stderr.

    Use after catching :func:`is_ssl_certificate_error`-positive exceptions:

        try:
            ...
        except Exception as exc:
            if is_ssl_certificate_error(exc):
                print_ssl_self_help(LOGGER)
                sys.exit(1)
            raise
    """
    if logger is None:
        logger = get_logger("ssl-self-help")
    for line in SSL_CERTIFI_HINT.splitlines():
        logger.error(line)
