"""Run the deterministic stages of the video-recipe pipeline.

This wrapper exists so a long-running production pipeline isn't 7 separate
shell commands the user has to chain manually. It orchestrates:

  1. download
  2. detect_cuts
  3. extract_audio       (skipped in --quick)
  4. detect_audio_cuts   (skipped in --quick)
  5. extract_keyframes
  6. transcribe          (skipped in --quick)
  7. ocr_title_cards     (skipped in --quick)

Then **STOPS**. The agent (Claude Code session) does stage 8 — read each
keyframe, classify the cut, write inferred.json. Once the agent is done:

  9.  python -m scripts.assemble_recipe <url> recipes/<id>/
  10. python -m scripts.attribute_model recipes/<id>/
  11. python -m scripts.eval_recipe recipes/<id>/recipe.json    (optional)

These last three are deterministic too but live outside this orchestrator
because (9) and (10) can be re-run after agent revisions, and (11) is
optional. Add ``--with-assemble`` to also run (9) and (10) — convenient
when iterating without an interactive agent.

Per-stage timing and pass/fail status are written to
``recipes/<id>/pipeline_log.json`` so the agent (or a human) can see what
was run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

from scripts import (
    assemble_recipe,
    attribute_model,
    detect_audio_cuts,
    detect_cuts,
    download,
    extract_audio,
    extract_keyframes,
    ocr_title_cards,
    render_html,
    transcribe,
)
from scripts._log import get_logger

LOGGER = get_logger("pipeline")


def _record(
    log: list[dict[str, Any]],
    stage_name: str,
    *,
    ok: bool,
    started_at: str,
    duration: float,
    error: str | None = None,
) -> None:
    log.append(
        {
            "stage": stage_name,
            "ok": ok,
            "started_at": started_at,
            "duration_sec": round(duration, 3),
            **({"error": error} if error else {}),
        }
    )


def _run(stage_name: str, log: list[dict[str, Any]], fn) -> Any:  # type: ignore[no-untyped-def]
    started_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    started = time.monotonic()
    LOGGER.info("[%s] start", stage_name)
    try:
        result = fn()
    except Exception as e:
        elapsed = time.monotonic() - started
        LOGGER.error("[%s] failed after %.2fs: %s: %s", stage_name, elapsed, type(e).__name__, e)
        _record(
            log,
            stage_name,
            ok=False,
            started_at=started_at,
            duration=elapsed,
            error=f"{type(e).__name__}: {e}",
        )
        raise
    elapsed = time.monotonic() - started
    LOGGER.info("[%s] done in %.2fs", stage_name, elapsed)
    _record(log, stage_name, ok=True, started_at=started_at, duration=elapsed)
    return result


def run(
    url: str,
    *,
    recipes_root: Path = Path("recipes"),
    quick: bool = False,
    threshold: float = 27.0,
    detector: str = "content",
    with_assemble: bool = False,
) -> tuple[str, Path, list[dict[str, Any]]]:
    """Run the deterministic pipeline. Returns (video_id, recipe_dir, log)."""
    log: list[dict[str, Any]] = []

    # 1. Download
    video_id, recipe_dir = _run("download", log, lambda: download.download(url, recipes_root))

    # 2. Detect pixel cuts
    cuts_path = recipe_dir / "cuts.json"
    _run(
        "detect_cuts",
        log,
        lambda: detect_cuts.detect_cuts(
            recipe_dir / "source.mp4", cuts_path, threshold=threshold, detector=detector
        ),
    )

    if not quick:
        # 3. Extract audio
        audio_path = recipe_dir / "audio.wav"
        _run(
            "extract_audio",
            log,
            lambda: extract_audio.extract_audio(recipe_dir / "source.mp4", audio_path),
        )

        # 4. Layer audio cuts on pixel cuts (mutate cuts.json in place)
        silence_path = recipe_dir / "silence.json"

        def _audio_cut_step() -> None:
            silences = detect_audio_cuts.detect_silence_boundaries(audio_path)
            silence_path.write_text(json.dumps(silences, indent=2))
            pixel_cuts = json.loads(cuts_path.read_text())
            merged = detect_audio_cuts.merge_silence_into_cuts(pixel_cuts, silences)
            cuts_path.write_text(json.dumps(merged, indent=2))

        _run("detect_audio_cuts", log, _audio_cut_step)

    # 5. Extract keyframes
    cuts_dir = recipe_dir / "cuts"
    _run(
        "extract_keyframes",
        log,
        lambda: extract_keyframes.extract_keyframes(recipe_dir / "source.mp4", cuts_path, cuts_dir),
    )

    if not quick:
        # 6. Transcribe + pair to cuts
        transcript_path = recipe_dir / "transcript.json"
        audio_path = recipe_dir / "audio.wav"

        def _transcribe_step() -> None:
            doc = transcribe.transcribe_audio(audio_path, transcript_path)
            cuts = json.loads(cuts_path.read_text())
            transcribe.pair_words_to_cuts(doc, cuts, cuts_dir)

        _run("transcribe", log, _transcribe_step)

        # 7. OCR title cards
        _run(
            "ocr_title_cards",
            log,
            lambda: ocr_title_cards.ocr_for_recipe(cuts_dir, cuts_path),
        )

    if with_assemble:
        # 9 + 10 + 12: assemble + attribute + render html. Skips stage 8 (agent
        # inference) — only use --with-assemble in test/dev flows where you
        # don't need real inferred prompts.
        repo_root = Path(__file__).resolve().parent.parent

        _run(
            "assemble_recipe",
            log,
            lambda: assemble_recipe.assemble(url, recipe_dir, repo_root),
        )
        _run(
            "attribute_model",
            log,
            lambda: attribute_model.attribute(recipe_dir),
        )
        # Final stage: render the human-readable HTML view alongside recipe.json.
        recipe_json = recipe_dir / "recipe.json"

        def _render_html_step() -> None:
            html = render_html.render(recipe_json)
            (recipe_dir / "recipe.html").write_text(html)

        _run("render_html", log, _render_html_step)

    # Always write the pipeline log.
    (recipe_dir / "pipeline_log.json").write_text(json.dumps(log, indent=2))
    return video_id, recipe_dir, log


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic stages of the video-recipe pipeline."
    )
    parser.add_argument("url")
    parser.add_argument(
        "--recipes-root",
        type=Path,
        default=Path("recipes"),
        help="Where recipe directories live (default: recipes/)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip audio, audio-cut merge, transcription, and OCR. Right for short demo reels.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=27.0,
        help="Cut detector threshold (default 27.0; see SKILL.md table).",
    )
    parser.add_argument(
        "--detector",
        choices=("content", "adaptive"),
        default="content",
        help="Cut detector backend.",
    )
    parser.add_argument(
        "--with-assemble",
        action="store_true",
        help=(
            "Also run assemble_recipe and attribute_model after the deterministic "
            "stages — useful in dev/test flows where you don't need real inferred "
            "prompts. Production runs should let the agent do stage 8 first."
        ),
    )
    args = parser.parse_args(argv)
    video_id, recipe_dir, _ = run(
        args.url,
        recipes_root=args.recipes_root,
        quick=args.quick,
        threshold=args.threshold,
        detector=args.detector,
        with_assemble=args.with_assemble,
    )
    print(f"\nvideo_id: {video_id}\nrecipe_dir: {recipe_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
