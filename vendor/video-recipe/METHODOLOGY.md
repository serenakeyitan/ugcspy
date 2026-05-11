# Methodology track — markdown-only contribution guide

You don't need Python, ffmpeg, tesseract, or any of the toolchain to contribute meaningfully to `video-recipe`. The skill is **markdown-first by design**: how the agent thinks lives in `SKILL.md` and `prompts/*.md`, and those files are where most quality improvements happen.

This guide is for contributors who want to improve methodology without setting up the full development environment.

## What you can edit without any setup

These files live entirely in markdown / docs / prose. Edit them in the GitHub web UI, no clone required:

- [`SKILL.md`](SKILL.md) — the orchestration playbook the agent reads
- [`prompts/infer_prompt.md`](prompts/infer_prompt.md) — per-cut classification + caption guidance
- [`prompts/identify_hook.md`](prompts/identify_hook.md) — hook pattern enum + worked examples
- [`prompts/identify_tts.md`](prompts/identify_tts.md) — synthetic-voice cues
- [`README.md`](README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CHANGELOG.md`](CHANGELOG.md), [`RELEASE.md`](RELEASE.md), this file

## What requires the toolchain (don't touch in markdown-only PRs)

- `scripts/*.py` — deterministic logic, has tests
- `tests/*.py` — those tests
- `schemas/*.json` — recipe contract, validated by code

If you want to change one of those, follow the regular [`CONTRIBUTING.md`](CONTRIBUTING.md) flow with the Python toolchain.

## 3-step PR flow for markdown changes

### 1. Open the file in GitHub

Click any file above on github.com. Then click the **pencil icon** (top right) to edit it directly in the web UI.

### 2. Make your changes

Write the change. Then in the form below the editor:

- **Commit message**: one line describing the change (e.g. `prompts: clarify shock_cut pattern with TikTok example`)
- **Description** (optional): a paragraph of context — what was unclear before, what's clearer now, any dogfood evidence motivating the change.
- **Choose**: "Create a new branch and start a pull request" — give the branch a name like `improve-shock-cut-clarity`.

GitHub creates the branch, the commit, and opens the PR draft.

### 3. Link an existing issue

Every PR must reference an issue (see [`CONTRIBUTING.md`](CONTRIBUTING.md)). Either:

- Find an open issue your change addresses → put `Closes #N` in the PR body.
- No issue exists yet → open one first describing the methodology gap, then put `Closes #N` in the PR.

Submit. CI runs (it's only `ruff` + `pytest` for markdown — your changes won't break anything since you didn't touch code).

## What gets reviewed

Methodology PRs aren't reviewed for code style. They're reviewed for:

1. **Clarity** — does the agent now do the right thing more reliably? Could a smart reader follow your wording without prior context?
2. **Groundedness** — is there evidence from a real run motivating the change? "I noticed the agent kept doing X on Sora announcement videos" beats "this would be cleaner."
3. **Examples** — added examples should be drawn from real videos we've already dogfooded, or be obviously plausible.

The reviewer might ask for a tightened example, a reordered section, or a missing edge case. They won't ask you to fix Python tests.

## Worked example: improving a hook pattern

Imagine you notice that on TikTok-style cooking videos, agents sometimes label the opening as `shock_cut` when it's really a slower `transformation_tease` ("Here's what we're making" + cut to ingredients). The agent's pattern descriptions in `prompts/identify_hook.md` aren't distinguishing the two clearly enough.

Here's what a quality methodology PR looks like:

**Issue you'd open first:**

> ### Cooking-video opens get classified as shock_cut when they're really transformation_tease
>
> Three TikTok cooking videos I tested in dogfood (videos: A, B, C — links) all opened with "Today we're making X" cut to ingredient lay-out. Agent labeled all three `shock_cut`. The opening isn't surprising — it's a tease of the finished dish coming later. `transformation_tease` fits better but the prompt's example for that pattern only covers makeover content, not food.
>
> ### Proposed change
>
> Add a cooking-content example to the `transformation_tease` row in `prompts/identify_hook.md`. Tighten the `shock_cut` description to require visual surprise, not just "opens with action."

**The PR**: edit `prompts/identify_hook.md`, add the cooking example, tighten the language. One commit, one file, ~15 lines changed.

**The review**: a maintainer asks if you tested the new wording on the same three cooking videos. You re-run the agent on them, confirm two of three now classify correctly, and respond in the PR comments with the result.

**Merge**: clean signal, real evidence, partial improvement.

## What you do NOT need

- A Python install
- ffmpeg, tesseract, whisper
- Local pytest passing
- Knowledge of the schema versions or assembler internals
- An OpenAI / Anthropic / Google API key

Methodology contributors are first-class. The skill works because the markdown is good. The Python is just glue.

## Questions

If something in `SKILL.md` is unclear and you're not sure how to improve it, **open an issue first** (no PR yet). Describe what tripped you up. A maintainer will either clarify (which you can then PR) or surface a deeper structural fix.

Don't stay stuck in silence — confusion in the markdown is itself a methodology bug.
