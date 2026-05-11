"""Preflight environment check.

Run before the first pipeline invocation. Reports each check with a one-line
remediation hint when something fails. Exits 0 when all required checks pass,
1 otherwise.

    python -m scripts.doctor
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

CheckResult = tuple[bool, str, str]  # (ok, label, hint_or_detail)

MIN_PYTHON = (3, 11)
MIN_DISK_GB = 2.0


def _check_python_version() -> CheckResult:
    actual = sys.version_info[:2]
    if actual >= MIN_PYTHON:
        return True, f"Python {actual[0]}.{actual[1]}", ""
    return (
        False,
        f"Python {actual[0]}.{actual[1]}",
        f"requires Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} — install via pyenv or python.org",
    )


def _check_binary(name: str, install_hint: str) -> CheckResult:
    bin_path = shutil.which(name)
    if not bin_path:
        return False, name, install_hint
    # Try a `--version` (some tools use -version, ignore failures gracefully).
    version: str = ""
    try:
        for flag in ("--version", "-version"):
            r = subprocess.run([bin_path, flag], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout:
                version = r.stdout.splitlines()[0].strip()
                break
            if r.returncode == 0 and r.stderr:
                version = r.stderr.splitlines()[0].strip()
                break
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return True, name, version or bin_path


def _check_tesseract_english() -> CheckResult:
    bin_path = shutil.which("tesseract")
    if not bin_path:
        return (
            False,
            "tesseract",
            "install: `brew install tesseract` (macOS) or `apt-get install tesseract-ocr`",
        )
    try:
        r = subprocess.run([bin_path, "--list-langs"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return (
            False,
            "tesseract --list-langs",
            "tesseract installed but failed to enumerate languages",
        )
    out = (r.stdout + r.stderr).splitlines()
    if any(line.strip() == "eng" for line in out):
        return True, "tesseract eng pack", "available"
    return (
        False,
        "tesseract eng pack",
        "install english data: brew install tesseract-lang OR apt-get install tesseract-ocr-eng",
    )


def _check_python_package(name: str, install_hint: str) -> CheckResult:
    try:
        importlib.import_module(name)
    except ImportError:
        return False, f"python: {name}", install_hint
    # importlib.metadata is the modern way to get a package version without
    # touching __version__ (which some packages have started deprecating).
    # The package name in setup may differ from the import name (e.g. PIL =>
    # Pillow) — the lookup map below covers our deps.
    import importlib.metadata as md

    package_name = {
        "PIL": "Pillow",
        "yt_dlp": "yt-dlp",
        "whisper": "openai-whisper",
    }.get(name, name)
    try:
        version = md.version(package_name)
    except md.PackageNotFoundError:
        version = "installed (no metadata)"
    return True, f"python: {name}", version


def _check_certifi_resolvable() -> CheckResult:
    try:
        import certifi

        path = Path(certifi.where())
    except ImportError:
        return False, "certifi", "pip install certifi"
    if not path.exists():
        return False, "certifi", f"certifi reports {path} but the file is missing"
    return True, "certifi", str(path)


def _check_disk_space(working_dir: Path = Path(".")) -> CheckResult:
    try:
        usage = shutil.disk_usage(working_dir)
    except OSError as e:
        return False, "disk space", f"could not stat {working_dir}: {e}"
    free_gb = usage.free / (1024**3)
    if free_gb < MIN_DISK_GB:
        return (
            False,
            "disk space",
            f"only {free_gb:.1f} GB free at {working_dir}; need >= {MIN_DISK_GB} GB",
        )
    return True, "disk space", f"{free_gb:.1f} GB free"


def _check_network(host: str = "www.youtube.com", port: int = 443) -> CheckResult:
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as e:
        return (
            False,
            f"network: {host}:{port}",
            f"could not reach {host}: {e}. Check connectivity / proxy.",
        )
    return True, f"network: {host}:{port}", "reachable"


def run_checks() -> list[CheckResult]:
    """Run every check in order and return the results."""
    checks: list[Callable[[], CheckResult]] = [
        _check_python_version,
        lambda: _check_binary(
            "ffmpeg",
            "install: `brew install ffmpeg` (macOS) or `apt-get install ffmpeg` (Debian/Ubuntu)",
        ),
        _check_tesseract_english,
        lambda: _check_python_package(
            "yt_dlp", 'install: `pip install -e ".[dev]"` from the repo root'
        ),
        lambda: _check_python_package("scenedetect", 'pip install -e ".[dev]"'),
        lambda: _check_python_package("whisper", 'pip install -e ".[dev]"'),
        lambda: _check_python_package("jsonschema", 'pip install -e ".[dev]"'),
        lambda: _check_python_package("PIL", 'pip install -e ".[dev]"'),
        _check_certifi_resolvable,
        _check_disk_space,
        _check_network,
    ]
    return [c() for c in checks]


def format_result(result: CheckResult) -> str:
    ok, label, detail = result
    glyph = "✓" if ok else "✗"  # ✓ or ✗
    if detail:
        return f"  {glyph} {label} ({detail})"
    return f"  {glyph} {label}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Preflight environment check.")
    parser.add_argument(
        "--quiet", action="store_true", help="Only print failures and the final summary."
    )
    args = parser.parse_args(argv)

    print("video-recipe doctor — checking environment...")
    results = run_checks()
    failures = [r for r in results if not r[0]]
    for r in results:
        if not args.quiet or not r[0]:
            print(format_result(r))

    if failures:
        print(f"\n{len(failures)}/{len(results)} checks failed. Fix the items marked ✗ above.")
        return 1
    print(f"\nAll {len(results)} checks passed. You're good to go.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
