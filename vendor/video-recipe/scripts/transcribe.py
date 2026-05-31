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
import re
import sys
from pathlib import Path
from typing import Any


def _load_whisper(model_name: str = "base") -> Any:
    import whisper

    return whisper.load_model(model_name)


# A Whisper segment with no_speech_prob above this is treated as
# non-speech (background music, ambience, silence). Whisper hallucinates
# plausible-sounding text on music beds, so we DROP the text for these
# segments rather than letting fake lyrics leak into briefs. 0.6 is
# conservative: Whisper's own decoder uses 0.6 as its default no-speech
# threshold for suppressing output, so we match it.
NO_SPEECH_PROB_THRESHOLD = 0.6

# Non-lexical vocalizations Whisper emits for sighs / "mmm" / "uh" / breaths.
# We keep these (they're real audio events worth marking in a brief) but tag
# the segment as non-lexical so downstream consumers know it isn't a scripted
# line to lip-sync.
_NON_LEXICAL_RE = re.compile(
    r"^[\s\W]*(?:"
    r"u+h+|u+m+|m+h+m+|h+m+|m+m+|a+h+|o+h+|e+r+|hmm+|uh-huh|mm-hmm|"
    r"\[.*?\]|\(.*?\)|♪+|"  # bracketed cues like [Music], (sighs), and ♪
    r")[\s\W]*$",
    re.IGNORECASE,
)


def _is_non_lexical(text: str) -> bool:
    """True when a segment's text is only a filler sound / bracketed cue
    (语气助词: sighs, 'mmm', 'uh') rather than scripted speech."""
    return bool(text) and bool(_NON_LEXICAL_RE.match(text))


def _result_to_doc(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize Whisper's raw result dict into our transcript.json schema.

    Non-speech handling (issue: BGM / 语气助词):
      - Segments whose no_speech_prob exceeds NO_SPEECH_PROB_THRESHOLD are
        treated as non-speech. We KEEP the segment (with its timing + a
        kind tag) but blank its text so Whisper's hallucinated lyrics over
        a music bed never reach the spoken-narrative field.
      - Segments whose text is only a filler sound / bracketed cue are
        tagged kind="non_lexical" but their text is preserved.
      - A top-level `audio_kind` summarizes the whole track: "speech",
        "music" (no speech segments at all), or "mixed".
    """
    segments_in = result.get("segments") or []
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    duration = 0.0
    speech_seg_count = 0
    nonspeech_seg_count = 0
    for seg in segments_in:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        text = str(seg.get("text", "")).strip()
        no_speech_prob = float(seg.get("no_speech_prob", 0.0) or 0.0)
        if end > duration:
            duration = end

        if no_speech_prob >= NO_SPEECH_PROB_THRESHOLD:
            # Non-speech (music / ambience / silence). Drop the text —
            # whatever Whisper "heard" here is a hallucination over the bed.
            nonspeech_seg_count += 1
            segments.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": "",
                "kind": "non_speech",
                "no_speech_prob": round(no_speech_prob, 3),
            })
            continue

        kind = "non_lexical" if _is_non_lexical(text) else "speech"
        if kind == "speech":
            speech_seg_count += 1
        segments.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "kind": kind,
            "no_speech_prob": round(no_speech_prob, 3),
        })
        # Only real scripted speech contributes word entries used for
        # lip-sync / cut pairing — non-lexical filler shouldn't become a
        # "word" the new creator is told to say.
        if kind != "speech":
            continue
        for w in seg.get("words", []) or []:
            wstart = float(w.get("start", w.get("startTime", start)))
            wend = float(w.get("end", w.get("endTime", end)))
            wtext = str(w.get("word", w.get("text", ""))).strip()
            if not wtext:
                continue
            words.append({"start": round(wstart, 3), "end": round(wend, 3), "word": wtext})

    if speech_seg_count == 0 and nonspeech_seg_count > 0:
        audio_kind = "music"  # nothing but a bed — no spoken narrative
    elif nonspeech_seg_count > 0:
        audio_kind = "mixed"
    else:
        audio_kind = "speech"

    return {
        "language": result.get("language"),
        "duration_sec": round(duration, 3),
        "segments": segments,
        "words": words,
        "audio_kind": audio_kind,
        "has_speech": speech_seg_count > 0,
        "source": "whisper",
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
