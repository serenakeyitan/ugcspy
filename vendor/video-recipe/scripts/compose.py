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
import hashlib
import json
import shutil
import subprocess
import sys
import textwrap
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
    p.add_argument(
        "--backgrounds",
        type=str,
        choices=["off", "pinterest", "web"],
        default="off",
        help=(
            "Search the web for a topic-matching background image per cut and composite it "
            "behind the cut as a blurred backdrop (ffmpeg overlay — no video-gen API cost). "
            "'pinterest' searches Pinterest first then falls back to generic web image search; "
            "'web' uses generic image search only; 'off' (default) skips backgrounds entirely. "
            "Best-effort: a search miss keeps the un-composited clip. Auto-gated to collage / "
            "greenscreen-kinetic formats (where a backdrop helps) when decode.json is present."
        ),
    )
    p.add_argument(
        "--no-burnin",
        action="store_true",
        help="Skip burning OCR'd overlay text into the reproduction. By default, overlay text from recipe.title_cards / cut.ocr_text / cut.caption is rendered as on-screen text per cut. Use this for testing or when the source's overlay is undesirable in the reproduction.",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Discard any previous compose_state.json and re-run from scratch. By default, compose resumes: cuts whose API calls succeeded previously are skipped and their cached outputs reused (so a Kling 502 mid-pipeline doesn't force you to re-pay for completed cuts). Use this when you want a clean run.",
    )
    p.add_argument(
        "--no-disclosure",
        action="store_true",
        help="Skip the AI-generated disclosure watermark on the final reproduction. Default behavior burns 'AI-generated' bottom-right of the output, required by FTC + EU AI Act + TikTok ToS for AIGC content. Using this flag prints a loud warning — most platforms now enforce labeling of synthetic content.",
    )
    p.add_argument(
        "--disclosure-text",
        type=str,
        default="AI-generated",
        help="Override the default disclosure text. Keep it short — long text dominates the frame. Default: 'AI-generated'.",
    )
    p.add_argument(
        "--disclosure-position",
        type=str,
        choices=["bottom-right", "bottom-left", "top-right", "top-left"],
        default="bottom-right",
        help="Where to place the disclosure watermark. Default: bottom-right (matches platform conventions for AI-labels).",
    )
    p.add_argument(
        "--tts",
        type=str,
        choices=["auto", "kling", "openai"],
        default="auto",
        help=(
            "Choose the TTS provider for the WHOLE reproduction. Mixing providers per-cut "
            "would break voice timbre + speech cadence (说话的节奏) across cut boundaries, so we "
            "always pick one for the whole video. "
            "'auto' (default): use Kling TTS when every cut transcript fits in 120 chars "
            "(bundled with lipsync, simpler integration); else use OpenAI TTS (no char limit). "
            "'kling': force Kling TTS, refuse upfront if any cut exceeds 120 chars. "
            "'openai': force OpenAI TTS (current behavior, no character constraint)."
        ),
    )
    p.add_argument(
        "--kling-voice-id",
        type=str,
        default=None,
        help=(
            "Kling voice catalog ID to use when --tts is kling or auto-resolves to kling. "
            "If unset, Kling uses its default voice for the chosen language. Voice catalog is "
            "in Kling's dev portal; we don't ship a baked-in list because it changes. "
            "Set this once you've picked a voice that matches the target creator."
        ),
    )
    p.add_argument(
        "--kling-voice-language",
        type=str,
        choices=["en", "zh"],
        default="en",
        help=(
            "Kling TTS language. Currently only Chinese ('zh') and English ('en') are supported. "
            "Other languages auto-translate to English per Kling docs."
        ),
    )
    p.add_argument(
        "--kling-voice-speed",
        type=float,
        default=1.0,
        help=(
            "Kling TTS speech rate, 0.8 (slowest) to 2.0 (fastest). Default 1.0 is natural "
            "conversational pacing. Lower values can feel sluggish on short transcripts; higher "
            "values useful for tight-cut videos where TTS audio must fit within the cut duration."
        ),
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


# ─── Per-cut background imagery (Pinterest/web composite) ──────────────────
#
# Formats where a searched backdrop actually helps. Greenscreen-kinetic /
# collage UGC is built ON a background image layer; reproducing it flat
# looks empty. Talking-head / AI-montage don't benefit (the frame is the
# subject), so we skip them unless decode.json is absent (then we trust the
# user's explicit --backgrounds opt-in and apply to every cut).
_BACKGROUND_ELIGIBLE_FORMATS: frozenset[str] = frozenset({
    "greenscreen_kinetic_listicle",
    "collage_voiceover",
    "ai_montage_kinetic",
})


def backgrounds_eligible(decode: dict | None) -> tuple[bool, str]:
    """Decide whether --backgrounds should run, given decode signals.

    - decode None (no decode.json) → trust the user's explicit opt-in,
      apply to all cuts.
    - decode present with an eligible format.kind → enabled.
    - decode present with an ineligible kind → disabled (a backdrop would
      just sit behind a talking head pointlessly)."""
    if decode is None:
        return True, "no decode signal (decode.json missing) — applying backgrounds as requested"
    kind = (decode.get("format") or {}).get("kind")
    if not kind:
        return True, "decode present but no format.kind — applying as requested"
    if kind in _BACKGROUND_ELIGIBLE_FORMATS:
        return True, f"enabled — format.kind = {kind!r}"
    return False, (
        f"disabled — format.kind = {kind!r} is not background-eligible "
        f"({sorted(_BACKGROUND_ELIGIBLE_FORMATS)}); a backdrop wouldn't help this format."
    )


def _cut_background_query(cut: dict) -> str:
    """Build the image-search query for a cut from its overlay/caption +
    scene text. Imported lazily so compose stays importable without the
    backgrounds module's optional deps."""
    from scripts.backgrounds import derive_query

    overlay = (cut.get("caption") or cut.get("ocr_text") or "").strip()
    scene = (resolve_cut_prompt(cut) or "").strip()
    return derive_query(overlay_text=overlay, scene_description=scene)


def _apply_cut_background(
    cut: dict,
    dst: Path,
    out_dir: Path,
    cut_idx: int,
    width: int,
    height: int,
    sources: list,
) -> None:
    """Search for + composite a background behind the cut clip at `dst`,
    in place. Best-effort: any miss/failure leaves `dst` untouched."""
    from scripts.backgrounds import composite_background, fetch_background

    query = _cut_background_query(cut)
    if not query:
        return
    bg_img = fetch_background(query, out_dir / f"bg-{cut_idx:02d}.jpg", sources)
    if not bg_img:
        return
    composited = out_dir / f"cut-{cut_idx:02d}-bg.mp4"
    if composite_background(dst, bg_img, composited, width, height):
        # Replace the clip in place so downstream stitch/lipsync see the
        # composited frame.
        shutil.move(str(composited), str(dst))
        print(f"[compose] cut {cut_idx}: composited background ('{query}')")


# ─── decode.json signal loading + gating ───────────────────────────────────
#
# decode.py (separate ugcspy stage) classifies videos by `format.kind`
# (talking_head_floating_card, greenscreen_kinetic_listicle, ai_montage,
# etc.) and `format.is_ai_generated`. compose.py can read these signals
# to (a) refuse human-shot videos before any API spend and (b) gate the
# --lipsync feature to talking-head formats only (Kling lipsync rejects
# faceless clips with code 1006 anyway; we save the user the bill and
# the failure round-trip).
#
# decode.json is OPTIONAL in the recipe directory. When missing, we
# default to "no signals" — refusal falls back to the legacy N/A
# prefix check, and --lipsync is left on (the user opted in explicitly,
# and the Kling API will reject the cut if there's no face).
#
# Talking-head format names from decode.py:classify_format()'s ladder.
# If decode.py grows new format kinds, expand this set.
_LIPSYNC_ELIGIBLE_FORMATS: frozenset[str] = frozenset({
    "talking_head_floating_card",
    "talking_head_with_static_overlay",
    "multi_scene_talking_head",
})


def load_decode_signals(recipe_dir: Path) -> dict | None:
    """Read decode.json from the recipe dir if present. Returns the
    parsed dict, or None when decode.json is missing/invalid. We don't
    fail loudly on missing decode.json — compose can still run without
    it, just without the extra gating signal."""
    p = recipe_dir / "decode.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        print(
            f"[compose] warning: decode.json at {p} is invalid JSON — "
            f"skipping decode-derived gating.",
            file=sys.stderr,
        )
        return None


def reject_non_ai_recipes(decode: dict | None) -> None:
    """Refuse recipes whose source decode classifies the video as
    NOT AI-generated. The slash command docs claim compose does this;
    until this fix, only the N/A-prefix string check fired (and that
    only worked if the agent that wrote the prompt remembered the
    prefix). With decode.json's structured signal, the gate is
    enforced regardless of prompt shape.

    No-op when decode.json is missing — falls through to the legacy
    N/A check in the cut loop."""
    if not decode:
        return
    fmt = decode.get("format") or {}
    is_ai = fmt.get("is_ai_generated")
    # Only refuse when the field is explicitly False. Missing field
    # means "decode couldn't tell" — let the user proceed.
    if is_ai is False:
        kind = fmt.get("kind", "unknown")
        fail(
            f"decode.json classifies this video as NOT AI-generated "
            f"(format.kind = {kind!r}). AI-reproducing a real creator looks "
            f"uncanny and risks misleading attribution. Use /ugcspy-fork to "
            f"brief a real creator instead. If you genuinely want to override "
            f"this, edit decode.json to set format.is_ai_generated: true.",
            code=1,
        )


def lipsync_eligible(decode: dict | None) -> tuple[bool, str]:
    """Decide whether --lipsync should actually run, given decode signals.

    Returns (eligible, reason) where reason is a short explanation
    shown in the cost preflight.

    Three states:
      - decode is None (file missing) → trust the user's --lipsync flag.
        Kling will reject faceless clips with code 1006 if our guess is
        wrong, and we fall back gracefully.
      - decode is present but no format.kind → conservative refusal.
        Decode ran but couldn't classify; safer to skip lipsync than to
        spend money on a likely-faceless cut.
      - decode has a known format.kind → look it up against the
        lipsync-eligible set."""
    if decode is None:
        return True, "no decode signal (decode.json missing) — running lipsync as requested"
    fmt = decode.get("format") or {}
    kind = fmt.get("kind")
    if not kind:
        return False, (
            "disabled — decode.json present but format.kind absent. "
            "Conservative refusal; re-run /ugcspy-decode to classify, "
            "or force lipsync by setting format.kind in decode.json."
        )
    if kind in _LIPSYNC_ELIGIBLE_FORMATS:
        return True, f"enabled — format.kind = {kind!r}"
    return False, (
        f"disabled — format.kind = {kind!r} is not in the lipsync-eligible set "
        f"({sorted(_LIPSYNC_ELIGIBLE_FORMATS)}). Lipsync warps a face; this format "
        f"likely has none. Saving the ~$0.084/sec per cut."
    )


# ─── TTS provider selection (issue #24) ────────────────────────────────────
#
# Two TTS providers are available:
#
#   - "openai" (current default): OpenAI TTS via the existing render adapter.
#     No character limit per call. Renders to MP3, then optionally warped
#     via separate Kling lipsync call (audio2video mode).
#
#   - "kling": Kling's TTS bundled into the lipsync endpoint (text2video mode).
#     Cleaner integration (one API call per cut for tts+lipsync), better
#     lipsync quality (Kling generates the TTS for the lipsync, not
#     retrofitting external audio). Hard 120-char limit per call.
#
# We pick ONE provider for the whole reproduction — mixing per-cut would
# break voice timbre + 说话的节奏 (speech rhythm) across cut boundaries,
# making the reproduction sound Frankenstein-stitched.
#
# --tts auto picks based on max cut transcript length:
#   - all cuts ≤120 chars → kling
#   - any cut >120 chars  → openai

KLING_TTS_CHAR_LIMIT = 120


def pick_tts_provider(
    cuts: list[dict],
    requested: str,
    lipsync_on: bool,
) -> tuple[str, str]:
    """Return (provider, reason). provider ∈ {"openai", "kling"}.
    Refuses with code 1 if --tts kling is forced but not actually usable.

    Coupling note: Kling TTS only exists inside Kling's lip-sync endpoint
    (mode: text2video). There's no standalone Kling-TTS API. So Kling TTS
    is only available when lipsync_on. When lipsync is off (non-talking-
    head format, or --lipsync flag absent), OpenAI is the only option.

    Args:
      cuts: recipe cuts to compute max transcript length over.
      requested: user's --tts flag value, ∈ {"auto", "kling", "openai"}.
      lipsync_on: whether lipsync will actually run (combines --lipsync
        flag with the decode-derived format gate). When False, Kling TTS
        isn't available regardless of what the user asked for.
    """
    # Only consider cuts that actually have non-empty transcripts. Cuts
    # with no spoken text don't need TTS at all and shouldn't influence
    # the picker — otherwise a recipe with zero transcripts would force
    # Kling TTS for "nothing" which doesn't make sense.
    transcripts = [
        (int(c.get("index", i)), len((c.get("transcript") or "").strip()))
        for i, c in enumerate(cuts)
        if (c.get("transcript") or "").strip()
    ]
    over_limit = [(idx, n) for idx, n in transcripts if n > KLING_TTS_CHAR_LIMIT]

    if requested == "openai":
        return "openai", "user forced --tts openai (no char limit, separate API call)"

    if requested == "kling":
        if not lipsync_on:
            fail(
                "--tts kling requires --lipsync to be active (Kling TTS only exists as a "
                "sub-mode of Kling's lipsync endpoint; there's no standalone Kling TTS). "
                "Either add --lipsync, or re-run with --tts openai. If --lipsync was set but "
                "the decode-derived format gate turned it off, the source video isn't a "
                "talking-head format and lipsync wouldn't help anyway.",
                code=1,
            )
        if not transcripts:
            fail(
                "--tts kling was forced, but the recipe has no per-cut transcripts to speak. "
                "Kling TTS+lipsync would render silence with no audio source. Either add "
                "transcripts to recipe.json's cut entries, or omit --tts (no TTS needed for "
                "this recipe).",
                code=1,
            )
        if over_limit:
            details = ", ".join(f"cut {idx}: {n} chars" for idx, n in over_limit)
            fail(
                f"--tts kling was forced, but {len(over_limit)} cut(s) exceed Kling's "
                f"{KLING_TTS_CHAR_LIMIT}-char TTS limit. Affected: {details}. "
                f"Either re-run without --tts (auto picks openai for long cuts), or shorten "
                f"the cut transcripts in recipe.json.",
                code=1,
            )
        return "kling", "user forced --tts kling (all cuts within 120-char limit; bundled with lipsync)"

    # auto
    if not transcripts:
        return "openai", "auto: no cut transcripts (no TTS needed; provider doesn't matter)"
    if not lipsync_on:
        return (
            "openai",
            "auto: lipsync is off (non-talking-head format or --lipsync not set), so Kling TTS isn't available",
        )
    if over_limit:
        max_idx, max_n = max(over_limit, key=lambda t: t[1])
        return (
            "openai",
            f"auto: cut {max_idx} has {max_n} chars > {KLING_TTS_CHAR_LIMIT} char Kling limit",
        )
    return "kling", (
        f"auto: max cut transcript = {max(n for _, n in transcripts)} chars, "
        f"fits within Kling's {KLING_TTS_CHAR_LIMIT}-char limit; lipsync is on (bundled cleaner)"
    )


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


# ─── Resume / idempotency state (issue #16) ───────────────────────────────
#
# Long composes (10+ cuts, ~15 min wall time) WILL fail mid-way at some
# point — transient Kling 502, OpenAI rate-limit, network hiccup. Without
# resume, the next compose run re-pays for every successful cut from the
# first run. That's directly users-burning-money.
#
# This module persists per-cut, per-stage state to
# `<recipe_dir>/reproduction/compose_state.json` after every successful
# API call. On the next compose invocation:
#
#   1. State file present + recipe.json unchanged → resume mode. Skip
#      stages marked `done` whose output file still exists.
#   2. State file present + recipe.json changed → refuse to resume;
#      tell the user to either revert the recipe OR pass `--no-resume`
#      to discard previous progress explicitly.
#   3. State file absent → fresh run (default behavior for first-time).
#
# Recipe hash includes only the cuts + tts blocks (the bits that affect
# what we render). Editorial fields like source_url + generated_at don't
# invalidate the cache.


# Bumped to "2" in PR #28 — args_signature now includes --tts and the kling
# voice fields, which means existing v1 state files have an incomplete
# signature and resuming from them risks the Frankenstein-cadence bug both
# audit agents caught. The schema-mismatch branch in init_or_load_state
# treats v1 state as "discard and start fresh," which is the right call
# given we can't reconstruct the missing fields safely.
STATE_SCHEMA_VERSION = "2"


def compute_recipe_hash(recipe: dict) -> str:
    """SHA-256 hash of the parts of recipe.json that affect rendering.
    Editorial fields like generated_at + source_url don't invalidate the
    cache. We canonicalize via sorted JSON so dict ordering doesn't
    change the hash."""
    relevant = {
        "cuts": recipe.get("cuts"),
        "tts": recipe.get("tts"),
        "title_cards": recipe.get("title_cards"),
        "voiceover": recipe.get("voiceover"),
    }
    canon = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def args_signature(args: argparse.Namespace) -> str:
    """Short stable string of args that affect rendering decisions.
    If the user re-runs with different flags, the cache is invalidated.

    MUST include every arg whose change would silently produce mixed/stale
    outputs across cached cuts. PR #27 added TTS-related args (--tts,
    --kling-voice-id, --kling-voice-language, --kling-voice-speed); failing
    to include them was the bug both audit agents flagged at HIGH after
    PR #27 merged. See issue #28 for the full Frankenstein-cadence scenario.

    Not included (intentionally):
      - --budget: cost ceiling, doesn't change render output
      - --dry-run: never reaches the render layer
      - --no-resume: this IS the cache control; doesn't go in the signature
      - --ugcspy-bin: tool path, doesn't affect outputs
      - --disclosure-* + --no-disclosure: applied at the FINAL watermark step,
        not per-cut. Changing these on resume re-writes only the final mp4.
        (Strictly we could include them, but the cost is one ffmpeg pass,
        not a re-render. Keep them out to allow cheap watermark iteration.)
    """
    return (
        f"lipsync={bool(getattr(args, 'lipsync', False))}"
        f"|no_burnin={bool(getattr(args, 'no_burnin', False))}"
        f"|tts={getattr(args, 'tts', 'auto')}"
        f"|kling_voice_id={getattr(args, 'kling_voice_id', None) or ''}"
        f"|kling_voice_lang={getattr(args, 'kling_voice_language', 'en')}"
        f"|kling_voice_speed={getattr(args, 'kling_voice_speed', 1.0):.2f}"
        # backgrounds composites a backdrop into each cut's clip — a cached
        # plain clip and a cached composited clip aren't interchangeable, so
        # toggling this must invalidate the resume cache.
        f"|backgrounds={getattr(args, 'backgrounds', 'off')}"
    )


def state_path(recipe_dir: Path) -> Path:
    """Where compose_state.json lives. Same dir as the cut output files
    so removing the reproduction dir wipes both."""
    return recipe_dir / "reproduction" / "compose_state.json"


def load_state(recipe_dir: Path) -> dict | None:
    """Read compose_state.json if present. Returns None on missing or
    unreadable. Caller decides what to do (refuse / discard / resume)."""
    p = state_path(recipe_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[compose] warning: compose_state.json at {p} is unreadable ({e}); "
            f"treating as fresh run.",
            file=sys.stderr,
        )
        return None


def save_state(recipe_dir: Path, state: dict) -> None:
    """Atomically write compose_state.json. We write to a temp path first
    and rename — partial writes from a Ctrl-C never leave a corrupt file."""
    p = state_path(recipe_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def init_or_load_state(
    recipe_dir: Path,
    recipe: dict,
    args: argparse.Namespace,
) -> tuple[dict, int]:
    """Return (state, resumed_cut_count). When the existing state matches
    the current recipe + args, we resume from it. When it mismatches,
    refuse with a clear error so the user makes the call. When --no-resume
    is set, start fresh.

    The state dict is initialized with empty per-cut sub-objects for every
    cut so callers don't have to special-case "first time seeing this cut."
    """
    cuts = recipe.get("cuts") or []
    fresh = {
        "schema_version": STATE_SCHEMA_VERSION,
        "recipe_hash": compute_recipe_hash(recipe),
        "args_signature": args_signature(args),
        "total_cost": 0.0,
        "cuts": [
            {"index": int(c.get("index", i)), "text2video": {}, "tts": {}, "lipsync": {}}
            for i, c in enumerate(cuts)
        ],
    }
    if getattr(args, "no_resume", False):
        # Caller asked for a clean slate. Wipe any prior state.
        if state_path(recipe_dir).exists():
            print("[compose] --no-resume: discarding previous compose_state.json")
        return fresh, 0

    existing = load_state(recipe_dir)
    if existing is None:
        return fresh, 0
    if existing.get("schema_version") != STATE_SCHEMA_VERSION:
        print(
            f"[compose] compose_state.json schema mismatch "
            f"({existing.get('schema_version')!r} vs {STATE_SCHEMA_VERSION!r}); "
            f"discarding previous state.",
            file=sys.stderr,
        )
        return fresh, 0
    if existing.get("recipe_hash") != fresh["recipe_hash"]:
        fail(
            "recipe.json has changed since the last compose_state.json was written. "
            "Resuming would mix outputs from the old + new recipe — silent corruption. "
            "Either revert recipe.json to match the previous run, or pass --no-resume "
            "to discard previous progress and start fresh.",
            code=1,
        )
    if existing.get("args_signature") != fresh["args_signature"]:
        fail(
            f"compose args changed since the last run "
            f"({existing.get('args_signature')!r} → {fresh['args_signature']!r}). "
            f"Resuming would mix outputs with different render settings. "
            f"Pass --no-resume to discard previous progress, or re-run with the "
            f"matching flags.",
            code=1,
        )
    # State matches — resume. Count how many cuts have at least one
    # completed stage so we can report the resume scope to the user.
    resumed = sum(
        1
        for c in existing.get("cuts", [])
        if any(c.get(stage, {}).get("status") == "done" for stage in ("text2video", "tts", "lipsync"))
    )
    if resumed > 0:
        print(
            f"[compose] resuming from previous run — {resumed}/{len(cuts)} cuts "
            f"have completed stages, ${existing.get('total_cost', 0):.2f} already spent."
        )
    return existing, resumed


def stage_done(state: dict, cut_idx: int, stage: str) -> bool:
    """True iff this stage of this cut was previously completed successfully.

    Returns False for:
      - missing cut entry
      - stage never attempted (empty dict)
      - stage attempted but failed (status: "failed", written by
        record_stage_failure — issue #29). Failed stages should be
        retried on resume, not skipped.

    Caller still checks the output file exists on disk before trusting
    the cache — state can lie if the user manually deleted files."""
    for c in state.get("cuts", []):
        if c.get("index") == cut_idx:
            return c.get(stage, {}).get("status") == "done"
    return False


# Stage dependency map: re-running a stage invalidates its downstream
# stages, since their cached outputs were produced against the OLD
# upstream output. text2video produces dst; tts produces audio_path;
# lipsync warps dst using both. Re-running text2video means lipsync is
# stale; re-running tts also means lipsync is stale.
_STAGE_DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "text2video": ("lipsync",),
    "tts": ("lipsync",),
    "lipsync": (),
}


def record_stage(
    state: dict,
    recipe_dir: Path,
    cut_idx: int,
    stage: str,
    cost: float,
    external_id: str | None = None,
) -> None:
    """Mark a stage as done in the state dict and persist immediately.
    Also invalidates any downstream stages so we don't reuse outputs
    produced against the old upstream output. Persist-on-every-success
    means a Ctrl-C between stages loses at most the in-flight stage."""
    for c in state.get("cuts", []):
        if c.get("index") == cut_idx:
            entry: dict = {"status": "done", "cost": float(cost)}
            if external_id:
                entry["external_id"] = external_id
            c[stage] = entry
            # Invalidate downstream stages — they were produced against
            # the now-stale upstream output.
            for downstream in _STAGE_DOWNSTREAM.get(stage, ()):
                if c.get(downstream, {}).get("status") == "done":
                    print(
                        f"[compose] cut {cut_idx}: {stage} re-ran, invalidating {downstream} cache",
                        file=sys.stderr,
                    )
                    c[downstream] = {}
            break
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(recipe_dir, state)


def record_stage_failure(
    state: dict,
    recipe_dir: Path,
    cut_idx: int,
    stage: str,
    error: str,
) -> None:
    """Mark a stage as failed in the state dict and persist immediately.
    Called BEFORE fail() in unrecoverable error paths so the next run
    knows what was attempted and can resume cleanly.

    Issue #29: previously a Kling-TTS lipsync failure called fail() with
    no record. The next run saw text2video cached + lipsync still pending,
    but the user's only path was to retry with --tts openai. After PR #28
    that's an args_signature mismatch → forces --no-resume → discards the
    cached text2video clips → 2× text2video cost. Recording the failure
    lets the user re-run with the SAME --tts kling (retrying just the
    failed lipsync) OR change args knowingly.

    We record cost=0 because we don't actually know whether Kling billed
    for the failed attempt. The conservative answer ("don't claim a cost
    that may not have been incurred") is safer than over-counting. The
    `error` string is persisted for forensics.

    Does NOT invalidate downstream stages — a failed stage means
    downstreams couldn't have run yet anyway."""
    for c in state.get("cuts", []):
        if c.get("index") == cut_idx:
            c[stage] = {
                "status": "failed",
                "cost": 0.0,
                "error": error,
            }
            break
    # total_cost unchanged; we didn't pay for this attempt (per the
    # comment above — if Kling did bill, we'll see it on the bill, but
    # we won't double-count it via state)
    save_state(recipe_dir, state)


# ─── Overlay burn-in helpers ───────────────────────────────────────────────
#
# Caption / overlay burn-in is the ffmpeg drawtext pass that renders the
# source video's on-screen text into the AI-generated reproduction. For
# kinetic-typography UGC (Mya pattern, talking-head with static overlay)
# the overlay IS the content — without it, the reproduction is silent
# imagery with no message. PR #11 deleted this step while shipping lipsync;
# this restores it.
#
# Text source priority per cut:
#   1. Top-level recipe.title_cards entry matching cut.index (legacy
#      schema used by hand-edited recipes like 7630138325545880845)
#   2. cut.caption (v0.5 editorial overlay — production-added kinetic text)
#   3. cut.ocr_text (v0.5 — OCR'd from source frames)
#
# Presentation handling (v1, single drawtext block per cut):
#   - static_overlay_full_duration → text visible for the cut's full duration
#   - any other (kinetic_per_chunk, animated, missing) → static drawtext as
#     fallback. Animated kinetic typography is a future improvement and
#     deserves its own ASS-subtitle pipeline (#15-followup).

# Conservative line width for 9:16 mobile video. Wider text wraps to more
# lines but each line stays readable. 30 chars is a good default for
# ~40-50pt fontsize on 1080-wide output.
_BURNIN_WRAP_COLUMNS: int = 30
# Cap on how many lines we'll burn. Beyond this, the overlay covers too
# much of the frame and becomes unreadable. Recipes with very long OCR
# text get truncated to the first N lines with a "…" marker.
_BURNIN_MAX_LINES: int = 12


def resolve_cut_burnin(cut: dict, recipe: dict) -> tuple[str | None, str]:
    """Return (burnin_text, presentation) for a cut, or (None, '') if no
    burn-in text resolves. Tries top-level recipe.title_cards first
    (legacy shape), then per-cut fields (v0.5 canonical).

    The `presentation` string tells the renderer how to display the
    text (static_overlay_full_duration / kinetic_per_chunk / unknown).
    v1 ignores it and always renders as static, but we surface it so a
    future kinetic-typography pipeline can branch on it."""
    cut_idx = int(cut.get("index", -1))

    # Priority 1: top-level title_cards array, find entry matching cut_idx
    title_cards = recipe.get("title_cards") or []
    for tc in title_cards:
        if int(tc.get("cut_index", -1)) == cut_idx:
            text = (tc.get("ocr_text") or "").strip()
            if text:
                return text, tc.get("presentation") or "unknown"

    # Priority 2: cut.caption (v0.5 editorial overlay)
    caption = (cut.get("caption") or "").strip()
    if caption:
        return caption, "static_overlay_full_duration"

    # Priority 3: cut.ocr_text (v0.5 — raw OCR from source frames)
    ocr = (cut.get("ocr_text") or "").strip()
    if ocr:
        return ocr, "static_overlay_full_duration"

    return None, ""


def wrap_burnin_text(text: str, columns: int = _BURNIN_WRAP_COLUMNS, max_lines: int = _BURNIN_MAX_LINES) -> str:
    """Wrap raw overlay text into a multi-line drawtext payload.

    Splits on existing newlines (recipes often pre-format with \n), then
    wraps each segment to `columns`. Truncates with an ellipsis when the
    total line count exceeds `max_lines` so the overlay never covers the
    whole frame."""
    if not text:
        return ""
    lines: list[str] = []
    # Respect existing line breaks in the source text — recipes often
    # write headlines + numbered lists with \n separators.
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            # Empty line preserves visual paragraph break
            lines.append("")
            continue
        wrapped = textwrap.fill(
            paragraph,
            width=columns,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(wrapped.split("\n"))
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["…"]
    return "\n".join(lines)


def escape_drawtext(text: str) -> str:
    """Escape special characters for ffmpeg's drawtext filter syntax.

    drawtext is fragile: backslash, colon, single-quote, comma, percent,
    and bracket all need special handling inside the filtergraph string.
    See https://ffmpeg.org/ffmpeg-filters.html#drawtext"""
    # Order matters — escape backslash first so we don't double-escape
    # the escapes we add later.
    out = text.replace("\\", "\\\\")
    out = out.replace(":", "\\:")
    out = out.replace("'", "\\'")
    out = out.replace("%", "\\%")
    # Commas and brackets are filtergraph separators
    out = out.replace(",", "\\,")
    out = out.replace("[", "\\[")
    out = out.replace("]", "\\]")
    # Drawtext interprets the newline literal as a line break when
    # text_shaping is enabled. We use textfile= to sidestep most of these
    # issues but keep escaping here for defense in depth.
    return out


# Font resolution: try a list of common system font paths so the same
# code works on macOS dev machines and Linux CI. drawtext's compiled-in
# default font isn't reliable across builds, so we explicitly pick.
_FONT_CANDIDATES: tuple[str, ...] = (
    # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux (Ubuntu default + common alternatives)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",  # Arch / Alpine
)


def _resolve_burnin_font() -> str | None:
    """Find the first available font from our candidate list, or None
    when no candidate exists. None lets drawtext fall back to its
    compiled-in default (which is fine when present, errors otherwise)."""
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


# Detect whether ffmpeg was built with libfreetype (drawtext support).
# Homebrew's default ffmpeg formula omits freetype on some builds; without
# it the drawtext filter throws "Filter not found" and compose fails.
# We probe once at module load and cache the result. When drawtext is
# unavailable, the burn-in pass is skipped with a clear warning rather
# than crashing the entire compose.
_DRAWTEXT_AVAILABLE: bool | None = None


def drawtext_available() -> bool:
    """Cached probe for ffmpeg drawtext support."""
    global _DRAWTEXT_AVAILABLE
    if _DRAWTEXT_AVAILABLE is not None:
        return _DRAWTEXT_AVAILABLE
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        _DRAWTEXT_AVAILABLE = "drawtext" in proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        _DRAWTEXT_AVAILABLE = False
    return _DRAWTEXT_AVAILABLE


def build_drawtext_filter(burnin_text: str, clip_dur: float) -> str:
    """Build a drawtext filter expression for static-overlay burn-in.

    Returns an ffmpeg filtergraph string ready to drop into -vf or
    -filter_complex."""
    escaped = escape_drawtext(burnin_text)
    # Position: top-center, with padding from the top. Mobile UGC overlays
    # typically sit in the upper third of the frame to avoid the captions
    # area at the bottom.
    # fontcolor=white with a black box behind for legibility on any background.
    # fontsize=42 is good for 1080-wide 9:16 video; readable but doesn't dominate.
    font = _resolve_burnin_font()
    font_arg = f":fontfile={font}" if font else ""
    return (
        f"drawtext=text='{escaped}'"
        f"{font_arg}"
        f":fontcolor=white"
        f":fontsize=42"
        f":box=1"
        f":boxcolor=black@0.55"
        f":boxborderw=12"
        f":x=(w-text_w)/2"
        f":y=h/8"
        f":line_spacing=8"
    )


# ─── AI-disclosure watermark (issue #17) ───────────────────────────────────
#
# FTC + EU AI Act + TikTok ToS all require labeling of AI-generated content.
# Unlabeled output of this tool is a compliance gap — especially for lipsync
# warps where the synthetic mouth is on a face resembling a real creator.
# We burn a disclosure watermark on the final reproduction by default.
#
# Position uses corner-anchored placement to match how platforms typically
# render their own AI-label badges (TikTok's "AI-Generated" badge sits
# bottom-right; Instagram's "AI" tag sits top-left on Reels).
#
# Like the caption burn-in, this requires ffmpeg with libfreetype. When
# missing we degrade gracefully: a warning prints, the final reproduction
# is written without the watermark. The user gets explicit notice so they
# don't unknowingly post unlabeled AI content.

# Maps user-facing position names to drawtext (x, y) expressions.
_DISCLOSURE_POSITIONS: dict[str, tuple[str, str]] = {
    "bottom-right": ("w-text_w-30", "h-text_h-30"),
    "bottom-left": ("30", "h-text_h-30"),
    "top-right": ("w-text_w-30", "30"),
    "top-left": ("30", "30"),
}


def build_disclosure_filter(text: str, position: str) -> str:
    """Build a drawtext filter for the AI-disclosure watermark.

    Smaller fontsize than the caption burn-in (28 vs 42) — the disclosure
    is meant to be visible-but-not-dominant. Black box behind for
    legibility on any background; corner-anchored placement matches
    platform conventions for AI labels."""
    x_expr, y_expr = _DISCLOSURE_POSITIONS.get(position, _DISCLOSURE_POSITIONS["bottom-right"])
    escaped = escape_drawtext(text)
    font = _resolve_burnin_font()
    font_arg = f":fontfile={font}" if font else ""
    return (
        f"drawtext=text='{escaped}'"
        f"{font_arg}"
        f":fontcolor=white"
        f":fontsize=28"
        f":box=1"
        f":boxcolor=black@0.65"
        f":boxborderw=8"
        f":x={x_expr}"
        f":y={y_expr}"
    )


def apply_disclosure_watermark(src: Path, dst: Path, text: str, position: str) -> None:
    """Single ffmpeg pass: apply disclosure drawtext over `src` and write
    to `dst`. Stream-copies audio (no need to re-encode); only re-encodes
    video. Faster than a full re-encode of the whole reproduction."""
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(src),
            "-vf",
            build_disclosure_filter(text, position),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",  # audio passes through; only video gets a new encode
            str(dst),
        ]
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

    # 1c. Load decode.json signals (optional). decode.json sits next to
    # recipe.json in the recipe dir when /ugcspy-decode has run.
    # Used by reject_non_ai_recipes (structured refusal) and lipsync_eligible
    # (format-based --lipsync gate). reject_non_ai_recipes runs immediately
    # because we want the strongest refusal signal to fire as early as possible.
    decode_signals = load_decode_signals(args.recipe_dir)
    reject_non_ai_recipes(decode_signals)

    # 1d. Initialize resume state. This must run BEFORE cost preflight so
    # a recipe-hash mismatch refusal fires even on --dry-run (the user
    # finds out about the stale state file before they commit to spending).
    # init_or_load_state may call fail() with code 1 — that's correct here.
    state, _resumed_count = init_or_load_state(args.recipe_dir, recipe, args)

    # 2. Pre-flight refusal check — if ANY cut is marked N/A (legacy human-shot
    # UGC marker that predates decode.json). Refuses before any API spend.
    # decode.json's is_ai_generated check (above) is the better gate, but the
    # N/A prefix is still required for recipes that don't have decode.json.
    for i, cut in enumerate(cuts):
        prompt = resolve_cut_prompt(cut) or ""
        if prompt.startswith("N/A"):
            fail(
                f"cut {i} marked N/A (likely a human-shot UGC video — "
                f"reproduction by AI render won't match the source). "
                f"Use /ugcspy-fork to brief a real creator instead.",
                code=1,
            )

    # 2b. Decide whether lipsync actually runs given decode signals. We surface
    # this AFTER refusal checks so the user sees the lipsync decision only
    # for recipes we're actually going to compose.
    lipsync_on, lipsync_reason = (False, "not requested")
    if args.lipsync:
        lipsync_on, lipsync_reason = lipsync_eligible(decode_signals)
    print(f"[compose] lipsync: {lipsync_reason}")

    # 2c. Pick TTS provider for the WHOLE reproduction. Mixing per-cut would
    # break voice timbre + 说话的节奏 (cadence) across cut boundaries.
    # Coupling: Kling TTS only exists as a sub-mode of Kling lipsync, so
    # the picker considers lipsync_on too.
    tts_provider, tts_reason = pick_tts_provider(cuts, args.tts, lipsync_on)
    print(f"[compose] tts provider: {tts_provider} ({tts_reason})")

    # 2d. Decide whether per-cut background compositing runs. Gated to
    # backdrop-friendly formats via decode.json. No API cost (pure ffmpeg),
    # so this is purely an output-quality knob — but it does add wall time
    # (a web search + download + an ffmpeg re-encode per cut).
    backgrounds_on = False
    bg_sources: list = []
    tech_width = 1080
    tech_height = 1920
    if args.backgrounds != "off":
        backgrounds_on, bg_reason = backgrounds_eligible(decode_signals)
        print(f"[compose] backgrounds ({args.backgrounds}): {bg_reason}")
        if backgrounds_on:
            from scripts.backgrounds import pick_sources

            bg_sources = pick_sources(args.backgrounds)
            # Output frame size: prefer the source's real dimensions from
            # decode.json so the backdrop matches the reproduction aspect.
            tech = (decode_signals or {}).get("technical") or {}
            tech_width = int(tech.get("width") or 1080) or 1080
            tech_height = int(tech.get("height") or 1920) or 1920

    # 3. Cost preflight — sum estimated cost across all cuts + TTS + optional lipsync.
    # Uses kling_billed_duration so the estimate matches what we'll actually
    # pay (Kling rounds 6-9s cuts UP to 10s; cost preflight has to know that).
    full_transcript = resolve_recipe_full_transcript(recipe)
    cuts_with_audio = [c for c in cuts if (c.get("transcript") or "").strip()]
    cost_estimate = 0.0
    cost_breakdown: list[str] = []
    # Kling text2video — billed by the rounded-up duration. Always runs
    # regardless of TTS provider (we need a base video for lipsync to warp,
    # or for the OpenAI TTS to be mixed over).
    kling_total = 0.0
    for cut in cuts:
        billed = kling_billed_duration(float(cut.get("duration_sec") or 0))
        kling_total += 0.10 * billed
    cost_estimate += kling_total
    cost_breakdown.append(f"text2video {len(cuts)} cuts: ${kling_total:.2f}")
    # TTS cost — depends on provider:
    #   openai: charged by character count (~$15 per 1M chars)
    #   kling:  bundled into the lipsync call, no separate TTS cost
    tts_chars = sum(len(c.get("transcript") or "") for c in cuts_with_audio) or len(full_transcript)
    if tts_provider == "openai" and tts_chars:
        tts_total = (tts_chars / 1_000_000) * 15
        cost_estimate += tts_total
        cost_breakdown.append(f"openai TTS {tts_chars} chars: ${tts_total:.4f}")
    # L3 lipsync — only on cuts that have audio, only when lipsync_on. Same
    # per-second cost regardless of which mode (audio2video vs text2video).
    # Billed against the same rounded duration as text2video — the lipsync
    # warp runs on the same Kling-output clip, so its duration is determined
    # by what text2video produced, NOT by the original recipe value.
    if lipsync_on:
        lipsync_total = 0.0
        for cut in cuts_with_audio:
            billed = kling_billed_duration(float(cut.get("duration_sec") or 0))
            lipsync_total += 0.084 * billed
        cost_estimate += lipsync_total
        if lipsync_total > 0:
            label = "lipsync+kling-tts" if tts_provider == "kling" else "lipsync"
            cost_breakdown.append(f"{label} {len(cuts_with_audio)} cuts: ${lipsync_total:.2f}")

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

    # 3. Render each cut: clip → optional per-cut TTS → optional lipsync warp.
    # Resume-aware: if a stage was completed in a previous run AND its
    # output file still exists on disk, skip the API call. State is
    # persisted after every successful API call so a Ctrl-C between
    # stages loses at most the in-flight stage.
    clip_paths: list[Path] = []
    cut_audio_paths: list[Path | None] = []  # parallel to cuts; None if no audio for this cut
    lipsync_statuses: list[tuple[int, str]] = []  # per-cut: (cut_index, status_string)
    out_dir = args.recipe_dir / "reproduction"
    out_dir.mkdir(exist_ok=True)
    # State was initialized earlier (before cost preflight) so the
    # recipe-hash-mismatch refusal fires on --dry-run too. By the time
    # we reach the render loop, state is already loaded or freshly built.
    total_cost = float(state.get("total_cost", 0.0))
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
        # Text2video stage. Resume-cache: if state says we already
        # rendered this cut AND the cached mp4 still exists, skip the
        # API call entirely.
        cut_idx = int(cut.get("index", i))
        dst = out_dir / f"cut-{i:02d}.mp4"
        cut_video_id: str | None = None
        if stage_done(state, cut_idx, "text2video") and dst.exists() and dst.stat().st_size > 0:
            # Reuse cached clip — no API call, no cost added (state already
            # has the cost from the previous run).
            for c in state["cuts"]:
                if c.get("index") == cut_idx:
                    cut_video_id = c.get("text2video", {}).get("external_id")
                    break
            print(f"[compose] cut {i}/{len(cuts) - 1}: text2video cached (skipping Kling call)")
        else:
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
            shutil.copy(mp4, dst)
            # Capture the Kling task_id (external_id) — needed for L3 lipsync
            cut_video_id = result.get("external_id")
            cost_this = float(result["cost_usd"])
            total_cost += cost_this
            print(f"[compose]   cost so far: ${total_cost:.2f}")
            # Persist BEFORE the budget check so a budget-exceeded abort
            # still saves the work we just paid for. The next run can
            # resume this cut even though we exited 4.
            record_stage(state, args.recipe_dir, cut_idx, "text2video", cost_this, cut_video_id)
            if total_cost > args.budget:
                fail(
                    f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i}",
                    code=4,
                )

        # Background composite (#: per-cut Pinterest/web backdrop). Runs on
        # the finalized text2video clip BEFORE TTS/lipsync so the lipsync
        # warp (if any) operates on the composited frame. No video-gen API
        # cost — pure ffmpeg overlay. Best-effort: a search miss or composite
        # failure keeps the un-composited clip and moves on. Gated to formats
        # where a backdrop helps (collage/greenscreen); skipped otherwise.
        if backgrounds_on:
            _apply_cut_background(
                cut=cut,
                dst=dst,
                out_dir=out_dir,
                cut_idx=cut_idx,
                width=int(tech_width),
                height=int(tech_height),
                sources=bg_sources,
            )

        # L2: per-cut TTS, aligned to this cut's spoken window.
        # Only renders OpenAI TTS when tts_provider == "openai". When
        # tts_provider == "kling", TTS happens inside the lipsync call
        # (L3 below) — no separate audio file is rendered.
        audio_path: Path | None = None
        if cut_transcript and tts_provider == "openai":
            audio_path = out_dir / f"cut-{i:02d}.mp3"
            if stage_done(state, cut_idx, "tts") and audio_path.exists() and audio_path.stat().st_size > 0:
                print(f"[compose]   cut {i}: TTS cached (skipping OpenAI call)")
            else:
                print(f"[compose]   rendering per-cut OpenAI TTS ({len(cut_transcript)} chars)...")
                tts_result = call_render(args.ugcspy_bin, {"kind": "tts", "text": cut_transcript})
                tts_src = Path(tts_result["mp3_path"])
                shutil.copy(tts_src, audio_path)
                cost_this = float(tts_result["cost_usd"])
                total_cost += cost_this
                record_stage(state, args.recipe_dir, cut_idx, "tts", cost_this)
                if total_cost > args.budget:
                    fail(
                        f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i} TTS",
                        code=4,
                    )
        cut_audio_paths.append(audio_path)

        # L3: optional lipsync warp — replaces dst with a face-synced version.
        # Only runs when lipsync_on (which combines the user's --lipsync
        # flag with the decode-derived format gate). When lipsync fails,
        # we keep the un-warped clip — the downstream concat layer
        # ffprobe-detects whether the clip has audio and routes to either
        # `normalize_with_audio` (lipsync succeeded) or
        # `mix_clip_with_padded_audio` (lipsync failed, mix the per-cut
        # TTS we already rendered above). So a failed cut is never silent
        # in --lipsync mode — that was the issue #14 silent-cut bug.
        lipsync_status = "skipped"  # for the closing summary
        # Lipsync runs when:
        #   - lipsync_on (combines --lipsync + decode format gate), AND
        #   - cut_video_id is set (text2video succeeded), AND
        #   - (tts_provider == "openai" and audio_path exists)  → audio2video mode
        #     OR (tts_provider == "kling" and cut_transcript exists)  → text2video mode
        kling_tts_mode = tts_provider == "kling" and bool(cut_transcript)
        openai_lipsync_mode = tts_provider == "openai" and audio_path is not None
        if lipsync_on and cut_video_id and (kling_tts_mode or openai_lipsync_mode):
            if stage_done(state, cut_idx, "lipsync") and dst.exists() and dst.stat().st_size > 0:
                # Lipsync was previously successful — dst already holds the
                # warped video. Skip the API call.
                print(f"[compose]   cut {i}: lipsync cached (skipping Kling warp call)")
                lipsync_status = "cached"
                # Don't fall through to the call_render block
                lipsync_statuses.append((cut_idx, lipsync_status))
                clip_paths.append(dst)
                continue
            if kling_tts_mode:
                print(
                    f"[compose]   running Kling lipsync (text2video mode, "
                    f"{len(cut_transcript)} chars, voice_id={args.kling_voice_id or 'default'})..."
                )
            else:
                print("[compose]   running Kling lipsync (audio2video mode, mixing OpenAI TTS)...")
            try:
                if kling_tts_mode:
                    lipsync_payload = {
                        "kind": "lipsync_text2video",
                        "video_id": cut_video_id,
                        "text": cut_transcript[:KLING_TTS_CHAR_LIMIT],
                        "voice_id": args.kling_voice_id,
                        "voice_language": args.kling_voice_language,
                        # voice_speed wired through per issue #30. Kling
                        # validates the range [0.8, 2.0] in render adapter;
                        # we trust argparse's float parse and pass through.
                        "voice_speed": args.kling_voice_speed,
                    }
                else:
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
                    cost_this = float(lip_result["cost_usd"])
                    total_cost += cost_this
                    lipsync_status = f"ok (+${cost_this:.2f})"
                    record_stage(
                        state,
                        args.recipe_dir,
                        cut_idx,
                        "lipsync",
                        cost_this,
                        lip_result.get("external_id"),
                    )
                    print(f"[compose]   lipsync ok; cost now: ${total_cost:.2f}")
                else:
                    lipsync_status = "no-mp4-fallback (paid $0)"
                    print(
                        "[compose]   lipsync returned no mp4 — keeping un-warped clip + TTS fallback (paid $0 for warp)"
                    )
                    if kling_tts_mode:
                        # No fallback audio exists in Kling-TTS mode — the
                        # lipsync was the ONLY source of audio for this cut.
                        # Falling back to silent video would be worse than
                        # failing loudly; the user should re-run with
                        # --tts openai if they want the resilient path.
                        # Issue #29: record the failure to state BEFORE
                        # fail() so the next run can resume cleanly (text2video
                        # stays cached, lipsync re-attempts) instead of
                        # forcing --no-resume → 2× text2video cost.
                        record_stage_failure(
                            state,
                            args.recipe_dir,
                            cut_idx,
                            "lipsync",
                            error="kling-tts: lipsync returned no mp4",
                        )
                        fail(
                            f"cut {i}: Kling TTS+lipsync failed (no mp4 returned). "
                            f"In --tts kling mode there's no audio fallback for failed cuts. "
                            f"Re-run with --tts openai to get the resilient path "
                            f"(OpenAI TTS rendered separately; failed lipsync cuts fall back "
                            f"to un-warped clip with the OpenAI audio mixed in), "
                            f"OR re-run with the same --tts kling to retry just this cut's lipsync "
                            f"(text2video stays cached, no double-billing).",
                            code=2,
                        )
            except SystemExit:
                # call_render fails with SystemExit on error. Behavior diverges
                # by TTS provider:
                #   - openai mode: keep the un-warped clip; the downstream
                #     concat layer mixes in the per-cut OpenAI TTS so the cut
                #     isn't silent. Graceful degrade.
                #   - kling mode: no fallback audio exists (we didn't render
                #     OpenAI TTS this run). Failing loudly is the only honest
                #     option — silent fallback would break the reproduction.
                if kling_tts_mode:
                    # Issue #29: persist the failure before fail() so retry
                    # doesn't force a full re-render of cached text2video clips.
                    record_stage_failure(
                        state,
                        args.recipe_dir,
                        cut_idx,
                        "lipsync",
                        error="kling-tts: lipsync call raised SystemExit (API error)",
                    )
                    fail(
                        f"cut {i}: Kling TTS+lipsync failed. In --tts kling mode there's "
                        f"no audio fallback for failed cuts. Re-run with --tts openai for "
                        f"the resilient path (separate TTS, failed lipsync degrades gracefully), "
                        f"OR re-run with the same --tts kling to retry just this cut's lipsync "
                        f"(text2video stays cached, no double-billing).",
                        code=2,
                    )
                lipsync_status = "failed-fallback (paid $0)"
                print(
                    f"[compose]   lipsync failed for cut {i} — keeping un-warped clip + TTS fallback (paid $0 for warp)"
                )
            if total_cost > args.budget:
                fail(
                    f"running cost ${total_cost:.2f} exceeded budget ${args.budget:.2f} after cut {i} lipsync",
                    code=4,
                )
        lipsync_statuses.append((i, lipsync_status))
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
    burnin_summary: list[tuple[int, str]] = []  # (cut_idx, "burned N chars" | "skipped: reason")
    for i, (clip, audio) in enumerate(zip(clip_paths, cut_audio_paths)):
        # Read the clip's actual duration via ffprobe — Kling rounded to
        # 5 or 10s, that's the duration to align audio against.
        clip_dur = ffprobe_duration(clip)
        normalized = out_dir / f"final-{i:02d}.mp4"

        # Resolve per-cut burn-in text from recipe.title_cards or
        # cut.caption / cut.ocr_text (in that priority). When --no-burnin
        # is passed or no text resolves, burnin_filter is None and the
        # normalize helpers skip drawtext.
        burnin_filter: str | None = None
        cuts_idx = int(cuts[i].get("index", i))
        if args.no_burnin:
            burnin_summary.append((cuts_idx, "skipped: --no-burnin"))
        elif not drawtext_available():
            # ffmpeg without libfreetype can't run drawtext. Degrade
            # gracefully — emit a clear one-time warning at the first
            # cut, then skip burn-in for every cut.
            if i == 0:
                print(
                    "[compose] warning: ffmpeg drawtext filter unavailable "
                    "(this build lacks libfreetype). Burn-in skipped. "
                    "To enable: install ffmpeg with `--enable-libfreetype` "
                    "(brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg "
                    "--with-freetype, or `apt install ffmpeg` on Debian/Ubuntu "
                    "ships with freetype by default).",
                    file=sys.stderr,
                )
            burnin_summary.append((cuts_idx, "skipped: drawtext unavailable in ffmpeg"))
        else:
            burnin_text, presentation = resolve_cut_burnin(cuts[i], recipe)
            if burnin_text:
                wrapped = wrap_burnin_text(burnin_text)
                burnin_filter = build_drawtext_filter(wrapped, clip_dur)
                burnin_summary.append(
                    (cuts_idx, f"burned {len(burnin_text)} chars ({presentation})")
                )
            else:
                burnin_summary.append((cuts_idx, "skipped: no overlay text in recipe"))

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
            # Lipsync clip — has audio, just normalize codec + optional burn-in
            normalize_with_audio(clip, normalized, clip_dur, burnin=burnin_filter)
        elif audio:
            # Non-lipsync (or lipsync-failed) cut with TTS audio.
            # Pad TTS with silence to clip duration so we don't lose
            # paid-for Kling frames via -shortest truncation.
            mix_clip_with_padded_audio(clip, audio, normalized, clip_dur, burnin=burnin_filter)
        else:
            # No audio for this cut (no transcript) — pad with silence
            # so concat doesn't choke on missing audio track
            mix_clip_with_silence(clip, normalized, clip_dur, burnin=burnin_filter)
        final_clip_paths.append(normalized)

    concat_list = out_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in final_clip_paths) + "\n")
    final = args.recipe_dir / "reproduction.mp4"
    # First concat all the normalized clips into a stitched intermediate.
    # Stream-copy is safe here: every input has identical codec params.
    # Then optionally apply the disclosure watermark to that stitched file.
    stitched = out_dir / "stitched.mp4"
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(stitched)])

    # Apply AI-disclosure watermark unless explicitly opted out. Required
    # by FTC + EU AI Act + TikTok ToS for AI-generated content.
    disclosure_status: str
    if args.no_disclosure:
        print(
            "[compose] ⚠ WARNING: --no-disclosure was set. The reproduction will NOT carry an AI-generated label. "
            "Most platforms (TikTok, Instagram, YouTube) require AIGC labeling under their ToS, "
            "and FTC + EU AI Act require disclosure of synthetic content. "
            "Use this flag only when you have a specific reason (e.g. you're applying a different label downstream).",
            file=sys.stderr,
        )
        shutil.move(str(stitched), str(final))
        disclosure_status = "skipped (--no-disclosure)"
    elif not drawtext_available():
        # ffmpeg without libfreetype can't render the watermark either. We
        # already warned earlier about caption burn-in being skipped; surface
        # the disclosure issue separately because it's a compliance gap.
        print(
            "[compose] ⚠ WARNING: ffmpeg drawtext unavailable; AI-disclosure watermark NOT applied. "
            "Install ffmpeg with libfreetype (apt install ffmpeg on Debian/Ubuntu, "
            "or brew tap homebrew-ffmpeg/ffmpeg + brew install homebrew-ffmpeg/ffmpeg/ffmpeg "
            "--with-freetype on macOS) and re-run to add the required label.",
            file=sys.stderr,
        )
        shutil.move(str(stitched), str(final))
        disclosure_status = "skipped (drawtext unavailable)"
    else:
        print(
            f"[compose] applying AI-disclosure watermark "
            f"({args.disclosure_text!r} at {args.disclosure_position})..."
        )
        apply_disclosure_watermark(stitched, final, args.disclosure_text, args.disclosure_position)
        # Clean up the intermediate stitched.mp4 since the watermarked
        # version is what the user wants.
        try:
            stitched.unlink()
        except OSError:
            pass
        disclosure_status = f"burned {args.disclosure_text!r} at {args.disclosure_position}"

    print(f"\n[compose] ✓ reproduction.mp4 written to {final}")
    print(f"[compose] AI-disclosure: {disclosure_status}")
    print(f"[compose] total cost: ${total_cost:.2f} of ${args.budget:.2f} budget")
    # Per-cut lipsync summary (only emitted when lipsync was on for at least one cut)
    if any(s != "skipped" for _, s in lipsync_statuses):
        print("[compose] lipsync per-cut status:")
        for cut_idx, status in lipsync_statuses:
            print(f"  - cut {cut_idx}: {status}")
    # Per-cut burn-in summary — always emitted so the user knows whether
    # their reproduction will carry the source's on-screen text.
    if burnin_summary:
        burned_count = sum(1 for _, s in burnin_summary if s.startswith("burned"))
        print(f"[compose] caption burn-in: {burned_count}/{len(burnin_summary)} cuts")
        for cut_idx, status in burnin_summary:
            print(f"  - cut {cut_idx}: {status}")


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


def normalize_with_audio(src: Path, dst: Path, clip_dur: float, burnin: str | None = None) -> None:
    """Re-encode `src` (which already has audio) to canonical codec
    params. Used for lipsync-warped clips so they concat cleanly with
    TTS-mixed clips from other cuts.

    When `burnin` is provided, applies a drawtext filter overlay before
    re-encoding. burnin is a complete drawtext filter spec from
    build_drawtext_filter()."""
    cmd = ["-y", "-i", str(src)]
    if burnin:
        # Use -vf so the drawtext filter applies to the video stream only;
        # audio passes through unchanged.
        cmd.extend(["-vf", burnin])
    cmd.extend(
        [
            *_CANONICAL_VIDEO_ARGS,
            *_CANONICAL_AUDIO_ARGS,
            "-t",
            f"{clip_dur:.3f}",
            str(dst),
        ]
    )
    run_ffmpeg(cmd)


def mix_clip_with_padded_audio(
    clip: Path,
    audio: Path,
    dst: Path,
    clip_dur: float,
    burnin: str | None = None,
) -> None:
    """Mix `audio` over `clip`. Pad audio with silence to clip_dur when
    audio is shorter (preserves all paid-for Kling video frames). Cut
    audio to clip_dur when audio is longer (no A/V drift across concat
    boundary). Either way: output is exactly clip_dur long.

    apad + atrim is the standard ffmpeg pattern for this. We use
    apad's whole_dur to extend silence to clip_dur, then atrim
    explicitly to that same duration in case the source audio was
    longer than expected.

    When `burnin` is provided, chains a drawtext filter on the video
    stream within the same -filter_complex graph."""
    # Build the filter_complex: optional video drawtext + mandatory audio pad
    video_filter = f"[0:v]{burnin}[v]" if burnin else ""
    audio_filter = f"[1:a]apad=whole_dur={clip_dur:.3f},atrim=duration={clip_dur:.3f}[a]"
    if video_filter:
        filter_complex = f"{video_filter};{audio_filter}"
        video_map = "[v]"
    else:
        filter_complex = audio_filter
        video_map = "0:v:0"
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(clip),
            "-i",
            str(audio),
            "-filter_complex",
            filter_complex,
            "-map",
            video_map,
            "-map",
            "[a]",
            *_CANONICAL_VIDEO_ARGS,
            *_CANONICAL_AUDIO_ARGS,
            "-t",
            f"{clip_dur:.3f}",
            str(dst),
        ]
    )


def mix_clip_with_silence(clip: Path, dst: Path, clip_dur: float, burnin: str | None = None) -> None:
    """For cuts with no transcript: re-encode video with a silent audio
    track of clip_dur. Needed so the final concat doesn't fail on
    missing audio streams between cuts.

    When `burnin` is provided, applies drawtext to the video stream."""
    cmd = [
        "-y",
        "-i",
        str(clip),
        "-f",
        "lavfi",
        "-t",
        f"{clip_dur:.3f}",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]
    if burnin:
        cmd.extend(["-vf", burnin])
    cmd.extend(
        [
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
    run_ffmpeg(cmd)


if __name__ == "__main__":
    compose(parse_args())
