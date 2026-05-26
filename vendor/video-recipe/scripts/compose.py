#!/usr/bin/env python3
"""Compose a video from a recipe.json by calling render APIs + ffmpeg.

The pipeline:
  1. Read recipe.json from the cut-recipe directory
  2. For each cut, build a prompt enriched with the spoken text for that
     cut's time window (L1 — gives Kling text2video a target for mouth
     movements; "free" improvement, no extra API call)
  3. For each cut, call `ugcspy render` kind=clip → 5s or 10s MP4 via Kling
  4. For each cut WITH a spoken-text window, call `ugcspy render` kind=tts
     to render a per-cut MP3 (L2 — replaces the single-MP3 approach so
     audio events land at the right cuts even if Kling cuts shift)
  5. For each cut whose generated clip has a face AND has audio, call
     `ugcspy render` kind=lipsync with {video_id, audio_file: base64-mp3}
     → warped MP4 with mouth movements synced to the TTS (L3 — talking-head
     reproduction. Roughly +$0.084/sec but only for talking-head cuts.)
  6. Stitch warped/un-warped clips with ffmpeg concat demuxer
  7. Output reproduction.mp4 in the recipe directory

The L3 lip-sync pass is opt-in via --lipsync. Default off because it
roughly doubles per-clip cost. When on, it only triggers for cuts that
have a spoken transcript (no point lip-syncing a silent cut).

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
    p.add_argument(
        "--lipsync",
        action="store_true",
        help="Apply Kling lip-sync warp to cuts that have audio. Roughly doubles cost (~+$0.084/sec per cut warped). Recommended for talking-head reproductions; pointless for greenscreen-kinetic or AI-montage.",
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


# ─── Recipe contract helpers ───────────────────────────────────────────────
#
# The recipe schema went through several revisions. compose has to read
# from real `run_pipeline.py` output (v0.5: cut.inferred.prompt, top-level
# tts.script) AND from legacy hand-edited recipes that put the prompt at
# the top level. Centralize the lookup here so the rest of compose doesn't
# care which shape the recipe uses.
#
# When neither shape resolves a prompt for a cut, that's a validation
# failure — fail loudly with the cut index, don't let dry-run pass and
# burn money on the first real cut.


def resolve_cut_prompt(cut: dict) -> str | None:
    """Return the prompt for a cut, trying every known schema location
    in priority order. Returns None if no prompt is present."""
    # v0.5 canonical: cut.inferred.prompt (nested under the inferred object)
    inferred = cut.get("inferred")
    if isinstance(inferred, dict):
        p = inferred.get("prompt")
        if isinstance(p, str) and p.strip():
            return p
    # Legacy / hand-edited recipes: top-level inferred_generation_prompt
    p = cut.get("inferred_generation_prompt")
    if isinstance(p, str) and p.strip():
        return p
    # Older still: top-level scene_description (pre-v0.4)
    p = cut.get("scene_description")
    if isinstance(p, str) and p.strip():
        return p
    return None


def resolve_recipe_full_transcript(recipe: dict) -> str:
    """Return the full spoken-text transcript, trying every known location.
    Returns empty string if none is present.

    This is the LEGACY single-transcript path used by cost-preflight when
    no per-cut transcripts exist. Per-cut transcripts (cut.transcript)
    take priority over this in the main render loop."""
    # v0.5 canonical: top-level tts.script (creator's intended read)
    tts = recipe.get("tts")
    if isinstance(tts, dict):
        s = tts.get("script")
        if isinstance(s, str) and s.strip():
            return s
    # v0.5 also: top-level audio block has only a transcript_path, not
    # inline text — we don't read it here because the path is repo-relative
    # and we'd need recipe_dir context. The per-cut transcript field is
    # the primary source post-#6.
    # Legacy: voiceover.full_transcript (pre-v0.5)
    vo = recipe.get("voiceover")
    if isinstance(vo, dict):
        s = vo.get("full_transcript")
        if isinstance(s, str) and s.strip():
            return s
    return ""


def validate_compose_ready(cuts: list[dict]) -> None:
    """Walk every cut, assert a prompt is resolvable. Fail loudly with
    the cut index if any cut lacks a prompt. Runs BEFORE the cost
    preflight so a malformed recipe never makes it to dry-run output."""
    missing: list[int] = []
    for cut in cuts:
        if resolve_cut_prompt(cut) is None:
            missing.append(int(cut.get("index", -1)))
    if missing:
        fail(
            f"recipe is missing prompts on cut(s) {missing}. Expected one of: "
            f"cut.inferred.prompt (v0.5), cut.inferred_generation_prompt (legacy), "
            f"or cut.scene_description (pre-v0.4). Re-run /ugcspy-recipe or check "
            f"the recipe shape against schemas/recipe.v0.5.json.",
            code=1,
        )


# ─── Composer ──────────────────────────────────────────────────────────────


def compose(args: argparse.Namespace) -> None:
    recipe_path = args.recipe_dir / "recipe.json"
    if not recipe_path.exists():
        fail(f"no recipe.json at {recipe_path}. Run scripts/run_pipeline first.")
    recipe = json.loads(recipe_path.read_text())
    cuts = recipe.get("cuts", [])
    if not cuts:
        fail("recipe has no cuts to compose")

    # 1. Validate the recipe contract BEFORE anything else. If a recipe
    # has a cut without a resolvable prompt (under any known schema
    # shape), fail loudly with the cut index. This protects users from
    # spending money on a doomed run when the recipe was generated by a
    # newer or older pipeline than compose was built against.
    validate_compose_ready(cuts)

    # 2. Pre-flight refusal check — if ANY cut is marked N/A (human-shot
    # UGC where AI reproduction would look uncanny), refuse before the
    # user spends money. Also surfaces this on --dry-run so they don't
    # waste time thinking through cost decisions for a video that won't
    # render anyway.
    for i, cut in enumerate(cuts):
        prompt = resolve_cut_prompt(cut) or ""
        if prompt.startswith("N/A"):
            fail(
                f"cut {i} marked N/A (likely a human-shot UGC video — "
                f"reproduction by AI render won't match the source). "
                f"Use /ugcspy-fork to brief a real creator instead.",
                code=1,
            )

    # 3. Cost preflight — sum estimated cost across all cuts + TTS + optional lipsync
    full_transcript = resolve_recipe_full_transcript(recipe)
    cuts_with_audio = [c for c in cuts if (c.get("transcript") or "").strip()]
    cost_estimate = 0.0
    cost_breakdown: list[str] = []
    # Kling text2video — same for every cut
    kling_total = 0.0
    for cut in cuts:
        dur = max(5, int(round(cut.get("duration_sec", 5))))
        kling_total += 0.10 * dur
    cost_estimate += kling_total
    cost_breakdown.append(f"text2video {len(cuts)} cuts: ${kling_total:.2f}")
    # TTS — sum of per-cut transcript lengths (L2), or fall back to full transcript (legacy path)
    tts_chars = sum(len(c.get("transcript") or "") for c in cuts_with_audio) or len(full_transcript)
    tts_total = (tts_chars / 1_000_000) * 15
    cost_estimate += tts_total
    if tts_chars:
        cost_breakdown.append(f"TTS {tts_chars} chars: ${tts_total:.4f}")
    # L3 lipsync — only on cuts that have audio, only if --lipsync
    if args.lipsync:
        lipsync_total = 0.0
        for cut in cuts_with_audio:
            dur = max(5, int(round(cut.get("duration_sec", 5))))
            lipsync_total += 0.084 * dur
        cost_estimate += lipsync_total
        if lipsync_total > 0:
            cost_breakdown.append(f"lipsync {len(cuts_with_audio)} cuts: ${lipsync_total:.2f}")

    print(f"[compose] {len(cuts)} cuts, estimated cost: ${cost_estimate:.2f}")
    for line in cost_breakdown:
        print(f"  - {line}")
    if cost_estimate > args.budget:
        fail(
            f"estimated cost ${cost_estimate:.2f} exceeds budget ${args.budget:.2f}. "
            f"Re-run with --budget {cost_estimate + 0.5:.2f} to proceed.",
            code=4,
        )
    if args.dry_run:
        print("[compose] dry-run; no API calls made.")
        return

    # 3. Render each cut: clip → optional per-cut TTS → optional lipsync warp
    total_cost = 0.0
    clip_paths: list[Path] = []
    cut_audio_paths: list[Path | None] = []  # parallel to cuts; None if no audio for this cut
    out_dir = args.recipe_dir / "reproduction"
    out_dir.mkdir(exist_ok=True)
    for i, cut in enumerate(cuts):
        base_prompt = resolve_cut_prompt(cut)
        # validate_compose_ready already asserted every cut has a prompt,
        # but defend in depth in case the recipe was mutated between
        # validation and here.
        if not base_prompt:
            fail(f"cut {i} has no resolvable prompt; cannot render")
        if base_prompt.startswith("N/A"):
            fail(
                f"cut {i} marked N/A (likely a human-shot UGC video — "
                f"reproduction by AI render won't match the source). "
                f"Use /ugcspy-fork to brief a real creator instead.",
                code=1,
            )

        # L1: append the spoken text for this cut to the prompt so Kling
        # text2video has a target for mouth movements. Cheap, no extra API.
        cut_transcript = (cut.get("transcript") or "").strip()
        if cut_transcript:
            # Truncate very long reads so we don't exceed Kling's prompt limits;
            # the words matter more than the full read for diffusion-based
            # mouth steering.
            short = cut_transcript[:300]
            prompt = f"{base_prompt} The person says: '{short}'"
        else:
            prompt = base_prompt

        print(f"[compose] rendering cut {i}/{len(cuts)-1} ({cut.get('duration_sec',5)}s)...")
        result = call_render(
            args.ugcspy_bin,
            {"kind": "clip", "prompt": prompt, "duration_sec": cut.get("duration_sec", 5)},
        )
        mp4 = Path(result["mp4_path"])
        if not mp4.exists():
            fail(f"render returned mp4_path={mp4} but file doesn't exist", code=2)
        dst = out_dir / f"cut-{i:02d}.mp4"
        shutil.copy(mp4, dst)
        # Capture the Kling task_id (external_id) — needed for L3 lipsync
        cut_video_id = result.get("external_id")
        total_cost += result["cost_usd"]
        print(f"[compose]   cost so far: ${total_cost:.2f}")
        if total_cost > args.budget:
            fail(
                f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i}",
                code=4,
            )

        # L2: per-cut TTS, aligned to this cut's spoken window
        audio_path: Path | None = None
        if cut_transcript:
            print(f"[compose]   rendering per-cut TTS ({len(cut_transcript)} chars)...")
            tts_result = call_render(args.ugcspy_bin, {"kind": "tts", "text": cut_transcript})
            tts_src = Path(tts_result["mp3_path"])
            audio_path = out_dir / f"cut-{i:02d}.mp3"
            shutil.copy(tts_src, audio_path)
            total_cost += tts_result["cost_usd"]
            if total_cost > args.budget:
                fail(f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i} TTS", code=4)
        cut_audio_paths.append(audio_path)

        # L3: optional lipsync warp — replaces dst with a face-synced version
        if args.lipsync and audio_path and cut_video_id:
            print("[compose]   running Kling lipsync warp...")
            try:
                lipsync_payload = {
                    "kind": "lipsync",
                    "video_id": cut_video_id,
                    "audio_path": str(audio_path),
                }
                lip_result = call_render(args.ugcspy_bin, lipsync_payload)
                lip_mp4 = Path(lip_result["mp4_path"])
                if lip_mp4.exists():
                    # Overwrite the un-warped clip with the warped one
                    shutil.copy(lip_mp4, dst)
                    total_cost += lip_result["cost_usd"]
                    print(f"[compose]   lipsync ok; cost now: ${total_cost:.2f}")
                else:
                    print("[compose]   lipsync returned no mp4 — keeping un-warped clip")
            except SystemExit:
                # call_render fails with SystemExit on error — for lipsync,
                # we'd rather log and continue with the un-warped clip than
                # abort the whole reproduction. The Kling lipsync API rejects
                # videos with no clear face — that's not a fatal compose error.
                print(f"[compose]   lipsync failed for cut {i} — keeping un-warped clip")
            if total_cost > args.budget:
                fail(f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i} lipsync", code=4)
        clip_paths.append(dst)

    # 4. Concat clips with ffmpeg. When L3 lipsync ran, each clip already
    # has its synced audio baked in. When L3 didn't run, we need to mix
    # the per-cut TTS into each clip first.
    if args.lipsync:
        # Lipsync clips have audio; straight concat works
        concat_list = out_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{p.name}'" for p in clip_paths) + "\n")
        final = args.recipe_dir / "reproduction.mp4"
        run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(final)])
    else:
        # Mix each clip with its corresponding per-cut TTS first, THEN concat.
        # Removes the "TTS drifts out of sync with cuts" failure mode from
        # the old single-MP3 approach.
        mixed_paths: list[Path] = []
        for i, (clip, audio) in enumerate(zip(clip_paths, cut_audio_paths)):
            if audio:
                mixed = out_dir / f"mixed-{i:02d}.mp4"
                run_ffmpeg([
                    "-y",
                    "-i", str(clip),
                    "-i", str(audio),
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-shortest",
                    str(mixed),
                ])
                mixed_paths.append(mixed)
            else:
                mixed_paths.append(clip)
        concat_list = out_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{p.name}'" for p in mixed_paths) + "\n")
        final = args.recipe_dir / "reproduction.mp4"
        run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(final)])

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
