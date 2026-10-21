# CI workflow — one manual step

GitHub blocks App tokens from writing files under `.github/workflows/`, so this
file could not be committed by the automation. It is kept here so you can move
it into place with two clicks.

## Do this once

1. Open `ci/workflow-ci.yml` on the `v2` branch
2. Copy its contents
3. Create `.github/workflows/ci.yml` in the repo and paste
4. Commit

Or from a clone:

```bash
mkdir -p .github/workflows
cp ci/workflow-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: run tests on every push"
git push
```

## What it does

On every push and PR it runs:

| Step | What it proves |
|---|---|
| `pytest tests/ -q` | 291 unit + API tests |
| `python scripts/smoke.py` | 103 end-to-end checks against a real HTTP server |
| `pyflakes` | no unused imports / undefined names |

Railway deploys independently of CI, so a red build does not block a deploy.
If you want it to, add a required status check in the branch protection rules.
