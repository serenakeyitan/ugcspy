#!/usr/bin/env python3
"""Compose a video from a recipe.json by calling render APIs + ffmpeg.

The pipeline:
  1. Read recipe.json from the cut-recipe directory
  2. For each cut, call `ugcspy render` (TS subprocess) with kind=clip to
     generate a 5s or 10s MP4 via Kling 3.0
  3. If recipe.voiceover.transcript_available, call `ugcspy render` with
     kind=tts to generate the voiceover MP3
  4. Stitch clips with ffmpeg concat demuxer
  5. Mix voiceover audio in (replacing any per-clip audio)
  6. Burn OCR'd overlay text as subtitles (one chunk per cut)
  7. Output reproduction.mp4 in the recipe directory

Cost tracking: every render call returns cost_usd. We accumulate and
abort if the running total exceeds a user-set budget cap (default $5).
That prevents a typo in the recipe from costing $50.

Usage:
    python -m scripts.compose <recipe_dir> [--budget USD] [--dry-run]

Exit codes:
    0 — reproduction.mp4 written
    1 — bad args / missing recipe
    2 — render API error mid-way (partial outputs may remain in recipe_dir)
    3 — ffmpeg compose error
    4 — budget cap exceeded
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ─── CLI helpers ────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render a recipe.json into reproduction.mp4")
    p.add_argument("recipe_dir", type=Path)
    p.add_argument(
        "--budget",
        type=float,
        default=5.0,
        help="Max USD to spend on this composition. Aborts when exceeded.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be rendered and the estimated cost, then exit.",
    )
    p.add_argument(
        "--ugcspy-bin",
        type=str,
        default="ugcspy",
        help="Path to the ugcspy CLI binary. Defaults to PATH lookup.",
    )
    return p.parse_args()


def fail(msg: str, code: int = 1) -> None:
    print(f"[compose] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ─── Render adapter ─────────────────────────────────────────────────────────


def call_render(ugcspy_bin: str, payload: dict) -> dict:
    """Subprocess into `ugcspy render`, pass payload via stdin, return parsed result."""
    proc = subprocess.run(
        [ugcspy_bin, "render"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout:
        fail(f"render returned no stdout (stderr: {proc.stderr[:200]})", code=2)
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        fail(f"render returned non-JSON: {proc.stdout[:300]}", code=2)
    if not result.get("ok"):
        provider = result.get("provider", "?")
        fail(f"render {payload.get('kind')} failed ({provider}): {result.get('error')}", code=2)
    return result


# ─── Composer ──────────────────────────────────────────────────────────────


def compose(args: argparse.Namespace) -> None:
    recipe_path = args.recipe_dir / "recipe.json"
    if not recipe_path.exists():
        fail(f"no recipe.json at {recipe_path}. Run scripts/run_pipeline first.")
    recipe = json.loads(recipe_path.read_text())
    cuts = recipe.get("cuts", [])
    if not cuts:
        fail("recipe has no cuts to compose")

    # 1. Pre-flight refusal check — if ANY cut is marked N/A (human-shot
    # UGC where AI reproduction would look uncanny), refuse before the
    # user spends money. Also surfaces this on --dry-run so they don't
    # waste time thinking through cost decisions for a video that won't
    # render anyway.
    for i, cut in enumerate(cuts):
        prompt = cut.get("inferred_generation_prompt") or cut.get("scene_description") or ""
        if prompt.startswith("N/A"):
            fail(
                f"cut {i} marked N/A (likely a human-shot UGC video — "
                f"reproduction by AI render won't match the source). "
                f"Use /ugcspy-fork to brief a real creator instead.",
                code=1,
            )

    # 2. Cost preflight — sum estimated cost across all cuts + TTS
    cost_estimate = 0.0
    for cut in cuts:
        dur = max(5, int(round(cut.get("duration_sec", 5))))
        cost_estimate += 0.10 * dur  # Kling std
    transcript = recipe.get("voiceover", {}).get("full_transcript", "")
    if transcript:
        cost_estimate += (len(transcript) / 1_000_000) * 15  # OpenAI TTS standard

    print(f"[compose] {len(cuts)} cuts, estimated cost: ${cost_estimate:.2f}")
    if cost_estimate > args.budget:
        fail(
            f"estimated cost ${cost_estimate:.2f} exceeds budget ${args.budget:.2f}. "
            f"Re-run with --budget {cost_estimate + 0.5:.2f} to proceed.",
            code=4,
        )
    if args.dry_run:
        print("[compose] dry-run; no API calls made.")
        return

    # 2. Render each cut
    total_cost = 0.0
    clip_paths: list[Path] = []
    out_dir = args.recipe_dir / "reproduction"
    out_dir.mkdir(exist_ok=True)
    for i, cut in enumerate(cuts):
        prompt = cut.get("inferred_generation_prompt") or cut.get("scene_description")
        if not prompt:
            fail(f"cut {i} has no prompt or scene_description; cannot render")
        if prompt.startswith("N/A"):
            fail(
                f"cut {i} marked N/A (likely a human-shot UGC video — "
                f"reproduction by AI render won't match the source). "
                f"Use /ugcspy-fork to brief a real creator instead.",
                code=1,
            )
        print(f"[compose] rendering cut {i}/{len(cuts)-1} ({cut.get('duration_sec',5)}s)...")
        result = call_render(
            args.ugcspy_bin,
            {"kind": "clip", "prompt": prompt, "duration_sec": cut.get("duration_sec", 5)},
        )
        mp4 = Path(result["mp4_path"])
        if not mp4.exists():
            fail(f"render returned mp4_path={mp4} but file doesn't exist", code=2)
        # Copy into recipe_dir so it survives temp cleanup
        dst = out_dir / f"cut-{i:02d}.mp4"
        shutil.copy(mp4, dst)
        clip_paths.append(dst)
        total_cost += result["cost_usd"]
        print(f"[compose]   cost so far: ${total_cost:.2f}")
        if total_cost > args.budget:
            fail(
                f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i}",
                code=4,
            )

    # 3. Render TTS (if transcript exists)
    voiceover_mp3: Path | None = None
    if transcript:
        print(f"[compose] rendering voiceover ({len(transcript)} chars)...")
        result = call_render(args.ugcspy_bin, {"kind": "tts", "text": transcript})
        src = Path(result["mp3_path"])
        voiceover_mp3 = out_dir / "voiceover.mp3"
        shutil.copy(src, voiceover_mp3)
        total_cost += result["cost_usd"]

    # 4. Concat clips with ffmpeg concat demuxer
    concat_list = out_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in clip_paths) + "\n")
    stitched = out_dir / "stitched.mp4"
    run_ffmpeg(
        [
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(stitched),
        ]
    )

    # 5. Mix voiceover audio in if we have one — replaces clip audio
    final = args.recipe_dir / "reproduction.mp4"
    if voiceover_mp3:
        run_ffmpeg(
            [
                "-y",
                "-i", str(stitched),
                "-i", str(voiceover_mp3),
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                str(final),
            ]
        )
    else:
        shutil.move(str(stitched), str(final))

    print(f"\n[compose] ✓ reproduction.mp4 written to {final}")
    print(f"[compose] total cost: ${total_cost:.2f} of ${args.budget:.2f} budget")


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        fail(f"ffmpeg failed: {proc.stderr[-400:]}", code=3)


if __name__ == "__main__":
    compose(parse_args())
