# Contributing

Our entire engineering practice is **one issue per problem, with a linked PR that resolves it**. Conversations live in issue comments, not in PR descriptions or Slack.

## Workflow

### 1. Open an issue first

Every PR must reference an existing issue. No issue → no PR.

- One issue = one problem. Don't bundle "fix download bug + add new prompt template" into one issue.
- Use comments for design discussion, alternatives, blockers. The issue is the discussion record; the PR is the resolution.
- Apply labels: `area:skill`, `area:scripts`, `area:prompts`, `area:ci`, `phase:v0`, `phase:later`.

### 2. Branch and PR

- Branch name: `<issue-number>-short-slug` (e.g., `12-yt-dlp-error-handling`).
- PR title: imperative, under 70 chars (e.g., `Add yt-dlp retry on transient network errors`).
- PR body: link the issue with `Closes #N` (or `Refs #N` if it's part of a multi-PR effort), then summary + test plan.
- One PR = one logical change. If review surfaces unrelated cleanups, open a separate issue and PR.

### 3. Review and merge

- At least one approving review before merge.
- All CI checks must pass (`ruff`, `pytest`).
- Squash-merge to keep `main` history linear. Never force-push branches under review — use the GitHub "Update branch" button if you need to rebase.
- Author merges after approval. Do not self-approve.

### 4. After merge

- The linked issue auto-closes if you used `Closes #N`.
- Delete the branch.

## What goes where

- **Methodology changes** (how the skill thinks, prompt wording, pipeline ordering): edit `SKILL.md` or `prompts/*.md`. These are markdown — partners with no code background can review. **No Python toolchain required** — see [METHODOLOGY.md](METHODOLOGY.md) for the no-setup contributor track.
- **Deterministic logic** (download, scene detection, frame extraction, schema validation, recipe assembly): edit `scripts/`. These need code review and tests.
- **Reference data** (model signatures, TTS fingerprints): edit `reference/`. Cite sources.

**Note on stage 4 (prompt inference).** This stage is **not** a Python script. It's done by Claude in-session, reading keyframes and writing `inferred.json` files per the methodology in `prompts/infer_prompt.md`. There is no API call and no key. Iteration on inference quality means editing `prompts/infer_prompt.md`, not `scripts/`.

## Code style

- Python: `ruff` for lint + format. Run `ruff check . && ruff format .` before pushing.
- Tests: `pytest`. Every script in `scripts/` should have a corresponding test in `tests/`.
- Type hints required on public functions.

## Commit messages

Conventional-ish, but don't overthink:

```
feat: add yt-dlp retry on transient errors
fix: handle empty cuts.json in extract_keyframes
docs: clarify v0 scope in SKILL.md
```

## Don't

- Don't push to `main`.
- Don't open a PR without an issue.
- Don't bundle unrelated changes.
- Don't merge your own PR.
- Don't force-push to a branch under review.
- Don't commit `recipes/<id>/` outputs — those are run artifacts, gitignored.
