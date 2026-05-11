# Release process

How to cut a release of `video-recipe`. Follow this for every numbered version.

## Pre-release checklist

1. **All open phase issues are closed or moved to a future milestone.**
2. **Main is green.** `gh run list --branch main --limit 3` shows recent successes.
3. **Local clean checkout.** `git status` shows nothing.
4. **Full test suite passes.** `pytest -q` from repo root.

## Cut the release

### 1. Bump version

```bash
# pyproject.toml: version = "X.Y.Z"
# CHANGELOG.md: add a new section for [X.Y.Z] - YYYY-MM-DD with the changes

git checkout -b release/vX.Y.Z
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to vX.Y.Z"
git push -u origin release/vX.Y.Z
gh pr create --title "Release vX.Y.Z" --body "..."
# review, merge after CI green
```

### 2. Run the 3 reference videos

Verify the full pipeline works on all three known-good cases:

```bash
# Reference 1: photoreal demo (Sora announcement)
python -m scripts.run_pipeline "https://www.youtube.com/watch?v=HK6y8DAPN_0" \
  --recipes-root /tmp/release-check --threshold 18

# Reference 2: TikTok-style mixed AI montage
python -m scripts.run_pipeline "https://www.youtube.com/watch?v=rQgaQ1p4tKU" \
  --recipes-root /tmp/release-check --detector adaptive --threshold 3.0

# Reference 3: stylized animation (Ghibli LOTR)
python -m scripts.run_pipeline "https://www.youtube.com/watch?v=vFpD-tfPfxE" \
  --recipes-root /tmp/release-check
```

For each, confirm:

- `pipeline_log.json` shows all stages `ok: true`
- `cuts.json` has a sane number of cuts (3–80; not 1, not 500)
- At least one keyframe per cut exists at `cuts/<i>/{a,b,c}.jpg`
- `transcript.json` exists (or audio is silent — check the audio.wav)
- `ocr.json` files exist on at least some cuts

### 3. Run stage 8 manually on at least one reference

Open Claude Code in the repo, invoke `/video-recipe` on the URL you just pre-processed, and let the agent fill in `inferred.json` files. Then run:

```bash
python -m scripts.assemble_recipe "<url>" /tmp/release-check/<id>
python -m scripts.attribute_model /tmp/release-check/<id>
python -m scripts.eval_recipe /tmp/release-check/<id>/recipe.json
```

Verify:

- `recipe.json` validates against the latest schema
- `model_attribution.primary_model` is set when there's signal
- If ground-truth prompts exist via OCR'd title cards, `eval.json`'s `mean_similarity` > 0.3

### 4. Tag and push

```bash
git checkout main
git pull --ff-only
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

### 5. Create the GitHub release

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-file <(awk "/^## \[X.Y.Z\]/,/^## \[/" CHANGELOG.md | sed '$d')
```

Or via the GitHub UI: paste the matching CHANGELOG section.

## Post-release

- Close the milestone in GitHub.
- Open a new milestone for the next version with the issues you deferred.
- Tweet / blog if the release has visible new capabilities.

## What can fail

- **CI install step times out** — Whisper + torch install is ~5 min. Don't panic; the 1m44s test step is fast once installed.
- **Reference video #1 or #2 disappears from YouTube** — find a new representative video and update this RELEASE.md and ARCHITECTURE.md references.
- **Schema validator throws on an old recipe** — that's a back-compat regression in `assemble_recipe.py`. Don't ship until the legacy shape still validates against the version it was written for.

## Hotfix path

If a critical bug ships:

1. Branch from the tag: `git checkout -b hotfix/X.Y.Z+1 vX.Y.Z`
2. Fix + test + PR + merge to main.
3. Cherry-pick to a new branch for the patch tag.
4. Tag `vX.Y.(Z+1)`, release as above.

Don't backport features in a hotfix — only the actual fix.
