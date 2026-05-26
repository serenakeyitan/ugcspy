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


# ─── Duration semantics ────────────────────────────────────────────────────
#
# Kling text2video std mode only produces 5s OR 10s segments. Whatever
# duration the recipe asks for, Kling will round to one of those two.
# compose.py used to estimate cost from raw `cut.duration_sec` (which
# under-priced 6-9s cuts and over-priced no one), and pass the raw value
# to Kling (which silently truncated >10s cuts and rounded everything
# else). Both audits caught this.
#
# Now: a single helper maps any requested duration to what Kling will
# actually render and bill for. Use this EVERYWHERE — cost preflight,
# pre-flight refusal, the render call. Recipes with cuts longer than
# Kling's max are refused upfront, not silently truncated.

# Kling std supports these two durations. Pro tier adds others but we
# don't expose Pro yet (recipe.v0.5 has no `mode` field per cut).
KLING_SUPPORTED_DURATIONS: tuple[int, ...] = (5, 10)
KLING_MAX_DURATION_SEC: int = max(KLING_SUPPORTED_DURATIONS)


def kling_billed_duration(requested_sec: float) -> int:
    """Return what Kling will actually render and bill for, given a
    requested duration. <=5 → 5, else 10. Matches src/render/kling.ts
    line ~52. If you change that file, change this one too."""
    if requested_sec <= 5:
        return 5
    return 10


def validate_durations(cuts: list[dict]) -> None:
    """Refuse recipes whose cuts exceed what Kling can render. Better
    to fail upfront than silently truncate the user's content."""
    too_long: list[tuple[int, float]] = []
    for cut in cuts:
        dur = float(cut.get("duration_sec") or 0)
        if dur > KLING_MAX_DURATION_SEC:
            too_long.append((int(cut.get("index", -1)), dur))
    if too_long:
        details = ", ".join(f"cut {i}: {d:.1f}s" for i, d in too_long)
        fail(
            f"cuts exceed Kling's max segment duration ({KLING_MAX_DURATION_SEC}s). "
            f"Affected: {details}. Re-cut the source video into shorter segments "
            f"(re-run /ugcspy-recipe with smaller --max-cut-duration or edit "
            f"recipe.json by hand to split long cuts).",
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

    # 1b. Validate durations — refuse cuts longer than Kling can render
    # rather than silently truncate. Surface this BEFORE cost preflight
    # so the user doesn't see a misleading dollar estimate for a recipe
    # we're about to reject.
    validate_durations(cuts)

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

    # 3. Cost preflight — sum estimated cost across all cuts + TTS + optional lipsync.
    # Uses kling_billed_duration so the estimate matches what we'll actually
    # pay (Kling rounds 6-9s cuts UP to 10s; cost preflight has to know that).
    full_transcript = resolve_recipe_full_transcript(recipe)
    cuts_with_audio = [c for c in cuts if (c.get("transcript") or "").strip()]
    cost_estimate = 0.0
    cost_breakdown: list[str] = []
    # Kling text2video — billed by the rounded-up duration
    kling_total = 0.0
    for cut in cuts:
        billed = kling_billed_duration(float(cut.get("duration_sec") or 0))
        kling_total += 0.10 * billed
    cost_estimate += kling_total
    cost_breakdown.append(f"text2video {len(cuts)} cuts: ${kling_total:.2f}")
    # TTS — sum of per-cut transcript lengths (L2), or fall back to full transcript (legacy path)
    tts_chars = sum(len(c.get("transcript") or "") for c in cuts_with_audio) or len(full_transcript)
    tts_total = (tts_chars / 1_000_000) * 15
    cost_estimate += tts_total
    if tts_chars:
        cost_breakdown.append(f"TTS {tts_chars} chars: ${tts_total:.4f}")
    # L3 lipsync — only on cuts that have audio, only if --lipsync.
    # Billed against the same rounded duration as text2video — the lipsync
    # warp runs on the same Kling-output clip, so its duration is determined
    # by what text2video produced, NOT by the original recipe value.
    if args.lipsync:
        lipsync_total = 0.0
        for cut in cuts_with_audio:
            billed = kling_billed_duration(float(cut.get("duration_sec") or 0))
            lipsync_total += 0.084 * billed
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

        # Always send the BILLED duration to Kling — not the raw recipe
        # value. Kling will round internally anyway, but passing the
        # rounded value makes the cost wire-format match what we logged
        # in the preflight breakdown.
        requested_dur = float(cut.get("duration_sec") or 0)
        billed_dur = kling_billed_duration(requested_dur)
        if billed_dur != requested_dur:
            print(
                f"[compose] cut {i}: recipe asks for {requested_dur:.1f}s, "
                f"Kling will render {billed_dur}s ({'rounded down' if billed_dur < requested_dur else 'rounded up'})..."
            )
        else:
            print(f"[compose] rendering cut {i}/{len(cuts) - 1} ({billed_dur}s)...")
        result = call_render(
            args.ugcspy_bin,
            {"kind": "clip", "prompt": prompt, "duration_sec": billed_dur},
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

    # 4. Normalize every clip to a canonical codec + timebase, then concat.
    #
    # Three reasons:
    #   a) ffmpeg concat demuxer with `-c copy` requires identical codecs
    #      and timebases across inputs. Our pre-concat inputs are a mix of:
    #        - raw Kling text2video MP4 (whatever Kling encodes to)
    #        - Kling lipsync-warped MP4 (possibly different settings)
    #        - re-encoded mix MP4 (with AAC audio we just produced)
    #      Stream-copying across that mix glitches or fails on real
    #      inputs. So we normalize before concat.
    #   b) We need EVERY output to have an audio track so the final
    #      reproduction has continuous audio. Lipsync-warped clips have
    #      Kling's mouth-synced audio baked in; lipsync-failed cuts fall
    #      back to TTS-mix; cuts with no transcript get silent audio
    #      padded to the clip duration. (Lipsync failed-cut handling is
    #      the explicit fix for issue #14, but it's worth fixing the
    #      audio-track shape here too.)
    #   c) Drop the `-shortest` truncation. When TTS audio is shorter
    #      than the Kling clip (the common case — Kling rounds up to
    #      5/10s, TTS is whatever the script length is), padding audio
    #      with silence preserves all paid-for Kling frames. When TTS
    #      is LONGER (rare but possible), we cut audio to clip duration
    #      explicitly — same total length, no A/V drift across the
    #      concat boundary.
    final_clip_paths: list[Path] = []
    for i, (clip, audio) in enumerate(zip(clip_paths, cut_audio_paths)):
        # Read the clip's actual duration via ffprobe — Kling rounded to
        # 5 or 10s, that's the duration to align audio against.
        clip_dur = ffprobe_duration(clip)
        normalized = out_dir / f"final-{i:02d}.mp4"
        # The lipsync warped clip already has synced audio (Kling bakes
        # it in). When --lipsync ran successfully, the clip file already
        # has an audio track; when lipsync failed and fell back, the clip
        # has no audio (Kling text2video has no audio). We detect via
        # ffprobe instead of trusting --lipsync state, which keeps the
        # normalization correct even when the lipsync stage silently
        # fell back to un-warped output (the failure path issue #14
        # tracks fixing properly).
        clip_has_audio = is_lipsync_clip(clip, i, out_dir)
        if clip_has_audio:
            # Lipsync clip — has audio, just normalize codec
            normalize_with_audio(clip, normalized, clip_dur)
        elif audio:
            # Non-lipsync (or lipsync-failed) cut with TTS audio.
            # Pad TTS with silence to clip duration so we don't lose
            # paid-for Kling frames via -shortest truncation.
            mix_clip_with_padded_audio(clip, audio, normalized, clip_dur)
        else:
            # No audio for this cut (no transcript) — pad with silence
            # so concat doesn't choke on missing audio track
            mix_clip_with_silence(clip, normalized, clip_dur)
        final_clip_paths.append(normalized)

    concat_list = out_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in final_clip_paths) + "\n")
    final = args.recipe_dir / "reproduction.mp4"
    # Now stream-copy is safe: every input has identical codec params.
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


# ─── ffmpeg helpers for clip normalization ─────────────────────────────────
#
# Every helper writes to a canonical shape:
#   video: h264, yuv420p, 30fps, fixed timebase
#   audio: AAC stereo 44.1kHz, fixed timebase
# That lets the final concat use stream-copy without glitching.

_CANONICAL_VIDEO_ARGS: list[str] = [
    "-c:v",
    "libx264",
    "-preset",
    "fast",
    "-pix_fmt",
    "yuv420p",
    "-r",
    "30",
    "-video_track_timescale",
    "30000",
]

_CANONICAL_AUDIO_ARGS: list[str] = [
    "-c:a",
    "aac",
    "-ar",
    "44100",
    "-ac",
    "2",
    "-b:a",
    "128k",
]


def ffprobe_duration(path: Path) -> float:
    """Return the duration of a video/audio file in seconds via ffprobe."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        fail(f"ffprobe failed on {path}: {proc.stderr[:200]}", code=3)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        fail(f"ffprobe returned non-numeric duration for {path}: {proc.stdout[:100]}", code=3)


def is_lipsync_clip(clip: Path, cut_index: int, out_dir: Path) -> bool:
    """Heuristic: a clip is the lipsync-warped version if it was written
    by the lipsync stage, which overwrites cut-NN.mp4 with the warp output.
    We can't tell from the file alone, so we check if the lipsync stage
    recorded success by writing a sentinel — but it doesn't. Instead, in
    the current architecture the lipsync stage overwrites the same
    cut-NN.mp4 path, so the safest check is: was --lipsync set AND did
    the cut have a transcript? If both, AND the clip exists, lipsync
    either succeeded (overwrote) or fell back (still un-warped). The
    final-clip stage handles both cases identically: normalize codec,
    keep whatever audio track exists, pad silence if no audio.

    For correctness here we just check: does the clip have an audio
    track? If yes, treat it as lipsync-succeeded (or pre-mixed). If no,
    treat it as needing TTS mix or silence."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def normalize_with_audio(src: Path, dst: Path, clip_dur: float) -> None:
    """Re-encode `src` (which already has audio) to canonical codec
    params. Used for lipsync-warped clips so they concat cleanly with
    TTS-mixed clips from other cuts."""
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(src),
            *_CANONICAL_VIDEO_ARGS,
            *_CANONICAL_AUDIO_ARGS,
            "-t",
            f"{clip_dur:.3f}",
            str(dst),
        ]
    )


def mix_clip_with_padded_audio(clip: Path, audio: Path, dst: Path, clip_dur: float) -> None:
    """Mix `audio` over `clip`. Pad audio with silence to clip_dur when
    audio is shorter (preserves all paid-for Kling video frames). Cut
    audio to clip_dur when audio is longer (no A/V drift across concat
    boundary). Either way: output is exactly clip_dur long.

    apad + atrim is the standard ffmpeg pattern for this. We use
    apad's whole_dur to extend silence to clip_dur, then atrim
    explicitly to that same duration in case the source audio was
    longer than expected."""
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(clip),
            "-i",
            str(audio),
            "-filter_complex",
            f"[1:a]apad=whole_dur={clip_dur:.3f},atrim=duration={clip_dur:.3f}[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            *_CANONICAL_VIDEO_ARGS,
            *_CANONICAL_AUDIO_ARGS,
            "-t",
            f"{clip_dur:.3f}",
            str(dst),
        ]
    )


def mix_clip_with_silence(clip: Path, dst: Path, clip_dur: float) -> None:
    """For cuts with no transcript: re-encode video with a silent audio
    track of clip_dur. Needed so the final concat doesn't fail on
    missing audio streams between cuts."""
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(clip),
            "-f",
            "lavfi",
            "-t",
            f"{clip_dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            *_CANONICAL_VIDEO_ARGS,
            *_CANONICAL_AUDIO_ARGS,
            "-t",
            f"{clip_dur:.3f}",
            str(dst),
        ]
    )


if __name__ == "__main__":
    compose(parse_args())
