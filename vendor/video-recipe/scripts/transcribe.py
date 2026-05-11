"""Transcribe audio with Whisper and pair words to cuts.

Two modes:

  python -m scripts.transcribe <audio.wav> <transcript.json>
    Whole-audio transcription. Output schema:
      {"language": "en",
       "duration_sec": 47.3,
       "segments": [{"start": 0.0, "end": 4.2, "text": "..."}, ...],
       "words":    [{"start": 0.0, "end": 0.3, "word": "Hello"}, ...]}

  python -m scripts.transcribe <audio.wav> <transcript.json> \\
      --pair-with-cuts <cuts.json> --cuts-dir <recipes/<id>/cuts>
    After whole-audio transcription, also write per-cut
    ``<cuts-dir>/<index>/transcript.json`` containing the words that fall
    inside each cut's [start_sec, end_sec).

Whisper is loaded lazily so the unit tests can stub it without pulling
torch + the model at import time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_whisper(model_name: str = "base") -> Any:
    import whisper

    return whisper.load_model(model_name)


def _result_to_doc(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize Whisper's raw result dict into our transcript.json schema."""
    segments_in = result.get("segments") or []
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    duration = 0.0
    for seg in segments_in:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        text = str(seg.get("text", "")).strip()
        if end > duration:
            duration = end
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        for w in seg.get("words", []) or []:
            wstart = float(w.get("start", w.get("startTime", start)))
            wend = float(w.get("end", w.get("endTime", end)))
            wtext = str(w.get("word", w.get("text", ""))).strip()
            if not wtext:
                continue
            words.append({"start": round(wstart, 3), "end": round(wend, 3), "word": wtext})
    return {
        "language": result.get("language"),
        "duration_sec": round(duration, 3),
        "segments": segments,
        "words": words,
    }


def transcribe_audio(
    audio_path: Path,
    out_path: Path,
    *,
    model_name: str = "base",
    model: Any | None = None,
) -> dict[str, Any]:
    """Transcribe ``audio_path`` and write transcript.json to ``out_path``.

    Returns the transcript document. ``model`` may be passed in for testing.
    """
    if model is None:
        model = _load_whisper(model_name)
    result = model.transcribe(str(audio_path), word_timestamps=True)
    doc = _result_to_doc(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2))
    return doc


def pair_words_to_cuts(
    transcript: dict[str, Any],
    cuts: list[dict[str, Any]],
    cuts_dir: Path,
) -> dict[int, Path]:
    """For each cut, write ``<cuts_dir>/<index>/transcript.json`` with the
    transcript words that fall inside that cut's [start_sec, end_sec).

    Returns a map of cut index -> path written.
    """
    words = transcript.get("words") or []
    out: dict[int, Path] = {}
    for cut in cuts:
        index = int(cut["index"])
        start = float(cut["start_sec"])
        end = float(cut["end_sec"])
        cut_words = [w for w in words if start <= float(w["start"]) < end]
        text = " ".join(w["word"] for w in cut_words).strip()
        cut_doc = {
            "start_sec": start,
            "end_sec": end,
            "text": text,
            "words": cut_words,
        }
        cut_dir = cuts_dir / str(index)
        cut_dir.mkdir(parents=True, exist_ok=True)
        out_path = cut_dir / "transcript.json"
        out_path.write_text(json.dumps(cut_doc, indent=2))
        out[index] = out_path
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Transcribe audio with Whisper.")
    parser.add_argument("audio_path", type=Path)
    parser.add_argument("out_path", type=Path)
    parser.add_argument("--model", default="base", help="Whisper model size (default: base)")
    parser.add_argument(
        "--pair-with-cuts",
        type=Path,
        default=None,
        help="cuts.json path. If set, write per-cut transcript.json files.",
    )
    parser.add_argument(
        "--cuts-dir",
        type=Path,
        default=None,
        help="Directory containing per-cut subdirs (recipes/<id>/cuts/).",
    )
    args = parser.parse_args(argv)
    from scripts._log import (
        get_logger,
        is_ssl_certificate_error,
        print_ssl_self_help,
        stage,
    )

    try:
        with stage("transcribe"):
            doc = transcribe_audio(args.audio_path, args.out_path, model_name=args.model)
    except Exception as exc:
        if is_ssl_certificate_error(exc):
            print_ssl_self_help(get_logger("transcribe"))
            return 1
        raise
    print(args.out_path)

    if args.pair_with_cuts:
        if not args.cuts_dir:
            parser.error("--pair-with-cuts requires --cuts-dir")
        with stage("transcribe.pair_with_cuts"):
            cuts = json.loads(args.pair_with_cuts.read_text())
            out_paths = pair_words_to_cuts(doc, cuts, args.cuts_dir)
        print(f"paired {len(out_paths)} cuts -> {args.cuts_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
