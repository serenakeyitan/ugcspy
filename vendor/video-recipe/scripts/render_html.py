"""Render recipe.json into a single self-contained recipe.html.

The HTML reads top-to-bottom as a production blueprint:

    Title metadata + source link
    Hook (pattern + duration + text + voiceover)
    Shot list (one section per cut: keyframe thumbnail, kind badge, duration,
               prompt or null reason, caption, voiceover slice)
    TTS block (script + likely_synthetic + evidence)
    Model attribution

Keyframes are embedded as base64 data URLs so the file is self-contained —
open it in any browser without needing the rest of the recipe directory.
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import json
import sys
from pathlib import Path
from typing import Any

KIND_COLORS = {
    "ai_clip": "#3b82f6",  # blue
    "title_card": "#a855f7",  # purple
    "non_ai_footage": "#10b981",  # green
    "lumped_cuts": "#f59e0b",  # amber
    "transition": "#6b7280",  # grey
    "unreadable": "#ef4444",  # red
}

HOOK_COLORS = {
    "question": "#3b82f6",
    "claim": "#a855f7",
    "shock_cut": "#ef4444",
    "transformation_tease": "#10b981",
    "pattern_break": "#f59e0b",
}


def _data_url(path: Path) -> str:
    """Embed an image as a base64 data URL."""
    if not path.exists():
        return ""
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def _esc(s: Any) -> str:
    """HTML-escape a value, handling None."""
    if s is None:
        return ""
    return html_lib.escape(str(s))


def _section(title: str, body: str) -> str:
    return f"""
<section class="block">
  <h2>{title}</h2>
  {body}
</section>
"""


def _render_hook(hook: dict[str, Any] | None) -> str:
    if not hook:
        return _section("Hook", "<p class='muted'>No clear hook identified.</p>")
    pattern = hook.get("pattern", "")
    color = HOOK_COLORS.get(pattern, "#6b7280")
    parts = []
    parts.append(
        f'<span class="badge" style="background:{color}">'
        f"{_esc(pattern)}</span>"
        f' <span class="muted">{hook.get("duration_sec", "?")}s · cuts '
        f"{', '.join(str(i) for i in hook.get('spans_cuts', []))}</span>"
    )
    if hook.get("text"):
        parts.append(f"<p class='caption-quote'>caption: <strong>{_esc(hook['text'])}</strong></p>")
    if hook.get("voiceover"):
        parts.append(
            f"<p class='voiceover-quote'>voiceover: <em>\"{_esc(hook['voiceover'])}\"</em></p>"
        )
    if hook.get("first_visual"):
        parts.append(
            f"<p class='detail'><span class='label'>first visual</span> "
            f"{_esc(hook['first_visual'])}</p>"
        )
    return _section("Hook", "".join(parts))


def _render_cut(cut: dict[str, Any], recipe_dir: Path, index: int) -> str:
    kind = cut.get("inferred_kind", "unreadable")
    color = KIND_COLORS.get(kind, "#6b7280")
    keyframes = cut.get("keyframes") or []
    # Use the middle (b) frame as the thumbnail; fall back to the first available.
    thumb_path: Path | None = None
    for kf in keyframes:
        candidate = recipe_dir / kf
        if candidate.exists():
            if kf.endswith("b.jpg"):
                thumb_path = candidate
                break
            thumb_path = thumb_path or candidate
    thumb_html = ""
    if thumb_path:
        thumb_html = f'<img class="thumb" src="{_data_url(thumb_path)}" alt="cut {index} keyframe">'

    head = (
        f'<div class="cut-head">'
        f'<span class="cut-index">cut {index}</span>'
        f'<span class="badge" style="background:{color}">{_esc(kind)}</span>'
        f'<span class="muted">{cut.get("duration_sec", "?")}s · '
        f"{cut.get('start_sec', '?')}–{cut.get('end_sec', '?')}s</span>"
        f"</div>"
    )

    body_parts = []
    inferred = cut.get("inferred")
    if isinstance(inferred, dict) and inferred.get("prompt"):
        body_parts.append(
            f'<p class="prompt"><span class="label">prompt</span> {_esc(inferred["prompt"])}</p>'
        )
        meta_pairs = []
        for key in ("style", "camera", "lighting", "aspect_ratio"):
            if inferred.get(key):
                meta_pairs.append(
                    f'<span class="meta-pair"><span class="meta-key">{key}</span>'
                    f" {_esc(inferred[key])}</span>"
                )
        if meta_pairs:
            body_parts.append(f'<div class="meta-row">{"".join(meta_pairs)}</div>')
    elif cut.get("inferred_error"):
        body_parts.append(
            f'<p class="error"><span class="label">why null</span> '
            f"{_esc(cut['inferred_error'])}</p>"
        )

    if cut.get("caption"):
        body_parts.append(
            f'<p class="caption-quote"><span class="label">caption</span> '
            f"<strong>{_esc(cut['caption'])}</strong></p>"
        )

    if cut.get("transcript"):
        body_parts.append(
            f'<p class="voiceover-quote"><span class="label">voiceover</span> '
            f'<em>"{_esc(cut["transcript"])}"</em></p>'
        )

    if cut.get("paired_prompt_text"):
        body_parts.append(
            f'<p class="ground-truth"><span class="label">paired prompt</span> '
            f"<strong>{_esc(cut['paired_prompt_text'])}</strong></p>"
        )

    return (
        f'<div class="cut">'
        f'<div class="cut-thumb">{thumb_html}</div>'
        f'<div class="cut-body">{head}{"".join(body_parts)}</div>'
        f"</div>"
    )


def _render_shot_list(recipe: dict[str, Any], recipe_dir: Path) -> str:
    cuts = recipe.get("cuts") or []
    if not cuts:
        return _section("Shot list", "<p class='muted'>No cuts.</p>")
    body = "".join(_render_cut(c, recipe_dir, c.get("index", i)) for i, c in enumerate(cuts))
    return _section("Shot list", body)


def _render_tts(tts: dict[str, Any] | None) -> str:
    if not tts:
        return _section("Voiceover (TTS)", "<p class='muted'>No voiceover detected.</p>")
    label = "AI-generated TTS" if tts.get("likely_synthetic") else "Real human speech"
    color = "#ef4444" if tts.get("likely_synthetic") else "#10b981"
    parts = []
    parts.append(
        f'<span class="badge" style="background:{color}">{label}</span> '
        f'<span class="muted">{tts.get("duration_sec", "?")}s · '
        f"{_esc(tts.get('language') or '?')}</span>"
    )
    if tts.get("model"):
        parts.append(f'<p class="detail"><span class="label">model</span> {_esc(tts["model"])}</p>')
    if tts.get("script"):
        parts.append(f'<p class="voiceover-quote"><em>"{_esc(tts["script"])}"</em></p>')
    evidence = tts.get("evidence") or []
    if evidence:
        items = "".join(f"<li>{_esc(e)}</li>" for e in evidence)
        parts.append(f'<p class="label">evidence</p><ul class="evidence">{items}</ul>')
    return _section("Voiceover (TTS)", "".join(parts))


def _render_attribution(attr: dict[str, Any] | None) -> str:
    if not attr or attr.get("primary_model") is None:
        return _section(
            "Model attribution",
            "<p class='muted'>No clear model attribution.</p>",
        )
    parts = []
    parts.append(
        f'<span class="badge" style="background:#a855f7">'
        f"{_esc(attr['primary_model'])}</span> "
        f'<span class="muted">confidence {attr.get("confidence", 0)}</span>'
    )
    candidates = attr.get("candidates") or {}
    if candidates:
        chips = "".join(
            f'<span class="chip">{_esc(m)} <small>{c}</small></span>'
            for m, c in sorted(candidates.items(), key=lambda kv: -kv[1])
        )
        parts.append(f'<p class="label">candidates</p><div class="chip-row">{chips}</div>')
    evidence = attr.get("evidence") or []
    if evidence:
        items = "".join(f"<li>{_esc(e)}</li>" for e in evidence)
        parts.append(f'<p class="label">evidence</p><ul class="evidence">{items}</ul>')
    return _section("Model attribution", "".join(parts))


CSS = """
* { box-sizing: border-box; }
body {
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: #1f2937;
  background: #fafafa;
  max-width: 820px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}
header h1 { font-size: 28px; margin: 0 0 4px; }
header .subtitle { color: #6b7280; font-size: 14px; }
header .meta { margin: 12px 0 0; color: #6b7280; font-size: 13px; }
header .source-link { word-break: break-all; }
.block {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 20px 24px;
  margin: 16px 0;
}
.block h2 {
  font-size: 18px;
  margin: 0 0 14px;
  color: #111827;
  letter-spacing: -0.01em;
}
.badge {
  display: inline-block;
  background: #6b7280;
  color: white;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: lowercase;
  font-family: ui-monospace, monospace;
}
.muted { color: #6b7280; font-size: 13px; }
.label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: #6b7280;
}
.detail { margin: 6px 0; font-size: 14px; }
.cut {
  display: flex;
  gap: 16px;
  padding: 14px 0;
  border-top: 1px solid #f3f4f6;
}
.cut:first-of-type { border-top: 0; padding-top: 4px; }
.cut-thumb {
  flex: 0 0 96px;
  width: 96px;
  height: 170px;
  background: #f3f4f6;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cut-thumb img.thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.cut-body { flex: 1; min-width: 0; }
.cut-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.cut-index { font-weight: 600; }
.prompt {
  margin: 8px 0;
  font-size: 14px;
  line-height: 1.55;
}
.prompt .label { display: block; margin-bottom: 2px; }
.meta-row {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px;
}
.meta-pair {
  background: #f3f4f6;
  padding: 3px 8px;
  border-radius: 6px;
}
.meta-key {
  font-weight: 600;
  color: #6b7280;
  margin-right: 4px;
}
.caption-quote {
  margin: 8px 0;
  padding: 6px 10px;
  background: #fef3c7;
  border-left: 3px solid #f59e0b;
  font-size: 14px;
  border-radius: 4px;
}
.voiceover-quote {
  margin: 6px 0;
  padding: 6px 10px;
  background: #ecfdf5;
  border-left: 3px solid #10b981;
  font-size: 14px;
  border-radius: 4px;
}
.ground-truth {
  margin: 6px 0;
  padding: 6px 10px;
  background: #ede9fe;
  border-left: 3px solid #a855f7;
  font-size: 14px;
  border-radius: 4px;
}
.error {
  margin: 6px 0;
  padding: 6px 10px;
  background: #fef2f2;
  border-left: 3px solid #ef4444;
  font-size: 13px;
  border-radius: 4px;
  color: #7f1d1d;
}
.evidence {
  margin: 6px 0 0;
  padding-left: 20px;
  font-size: 13px;
  color: #374151;
}
.evidence li { margin-bottom: 2px; }
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
.chip {
  background: #f3f4f6;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 13px;
  font-family: ui-monospace, monospace;
}
.chip small { color: #6b7280; margin-left: 4px; }
footer {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid #e5e7eb;
  font-size: 12px;
  color: #9ca3af;
  text-align: center;
}
footer a { color: inherit; }
"""


def render(recipe_path: Path) -> str:
    """Read recipe.json, return a self-contained HTML string."""
    recipe = json.loads(recipe_path.read_text())
    recipe_dir = recipe_path.parent

    title = recipe.get("video_id", "video-recipe")
    duration = recipe.get("duration_sec")
    resolution = recipe.get("resolution") or "—"
    fps = recipe.get("fps") or "—"
    source_url = recipe.get("source_url", "")
    generated = recipe.get("generated_at", "")
    schema = recipe.get("schema_version", "?")

    subtitle = (
        f"video-recipe v{_esc(schema)} · {_esc(duration)}s · {_esc(resolution)} @ {_esc(fps)}fps"
    )
    source_link = f'<a class="source-link" href="{_esc(source_url)}">{_esc(source_url)}</a>'
    header = f"""
<header>
  <h1>{_esc(title)}</h1>
  <p class="subtitle">{subtitle}</p>
  <p class="meta">source: {source_link}</p>
  <p class="meta">generated {_esc(generated)}</p>
</header>
"""

    body = (
        _render_hook(recipe.get("hook"))
        + _render_shot_list(recipe, recipe_dir)
        + _render_tts(recipe.get("tts"))
        + _render_attribution(recipe.get("model_attribution"))
    )

    footer = (
        "<footer>generated by "
        '<a href="https://github.com/serenakeyitan/video-recipe">video-recipe</a>'
        "</footer>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>recipe: {_esc(title)}</title>
  <style>{CSS}</style>
</head>
<body>
{header}{body}{footer}
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render recipe.json into recipe.html.")
    parser.add_argument("recipe_path", type=Path, help="recipes/<id>/recipe.json")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path (default: <recipe_path>.html next to recipe.json)",
    )
    args = parser.parse_args(argv)
    out = args.out or args.recipe_path.parent / "recipe.html"
    out.write_text(render(args.recipe_path))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
