# Releasing automaton-engine

One-time setup and the per-release steps.

---

## One-time setup: PyPI Trusted Publishing

Do this once before the first release. It lets the release workflow publish to PyPI without storing an API token in GitHub Secrets — GitHub's OIDC token is used instead.

1. **Create a PyPI account** at https://pypi.org/account/register/ if you don't have one.

2. **Add a Trusted Publisher** at https://pypi.org/manage/account/publishing/:
   - Owner: `730richey730`
   - Repository: `echoes`
   - Workflow filename: `release.yml`
   - Environment name: `release`

3. **Enable the `release` environment** in the GitHub repo:
   - Settings → Environments → New environment → name it `release`
   - Optionally add a required reviewer (yourself) so you must approve before it publishes.

4. **Enable GitHub Pages** for the docs site:
   - Settings → Pages → Source: "Deploy from a branch" → Branch: `gh-pages`
   - The docs deploy workflow pushes to `gh-pages` automatically on every push to `main`.

---

## Per-release checklist

### 1. Update CHANGELOG.md

Move items from `[Unreleased]` into a new versioned section:

```markdown
## [0.4.0] — 2026-06-01

### Added
- ...

[0.4.0]: https://github.com/730richey730/echoes/compare/v0.3.0...v0.4.0
```

Update the `[Unreleased]` comparison link at the bottom:
```markdown
[Unreleased]: https://github.com/730richey730/echoes/compare/v0.4.0...HEAD
```

### 2. Bump the version in pyproject.toml

```toml
version = "0.4.0"
```

### 3. Confirm tests pass locally

```bash
cd automaton
python -m pytest tests/ -q
```

### 4. Commit

```bash
git add automaton/pyproject.toml automaton/CHANGELOG.md
git commit -m "chore: release v0.4.0"
git push
```

### 5. Tag and push

```bash
git tag v0.4.0
git push --tags
```

This triggers the `release.yml` workflow, which:
1. Runs the full test suite
2. Builds the wheel + sdist and runs `twine check`
3. Publishes to PyPI via Trusted Publishing
4. Creates a GitHub Release with the CHANGELOG entry as the body and the wheel + sdist attached

### 6. Verify

- **PyPI**: https://pypi.org/project/automaton-engine/ — new version should appear within a minute.
- **GitHub Release**: https://github.com/730richey730/echoes/releases — check the release notes look right.
- **Docs**: https://730richey730.github.io/echoes/ — updated on the push to main in step 4.
- **Install test**: `pip install automaton-engine==0.4.0 && automaton --help`

---

## Cutting v0.3.0 right now

The current state of `main` is v0.3.0. Steps:

```bash
cd C:\Users\730ri\projects\echoes

# Verify clean state
git status
python -m pytest automaton/tests/ -q

# Commit anything staged
git add -A
git commit -m "chore: release v0.3.0"
git push

# Tag
git tag v0.3.0
git push --tags
```

Then watch the Actions tab: https://github.com/730richey730/echoes/actions

---

## Pre-release tags

Tags containing `rc`, `alpha`, or `beta` are automatically marked as pre-releases on GitHub. Example:

```bash
git tag v0.4.0rc1
git push --tags
```

The GitHub Release will show "Pre-release" and the wheel will still be published to PyPI (as `0.4.0rc1`).

---

## Hotfix releases

If a bug needs fixing without releasing unreleased features:

```bash
git checkout v0.3.0
git checkout -b hotfix/0.3.1
# fix the bug, add a test
git commit -m "fix: ..."
git push -u origin hotfix/0.3.1
# update CHANGELOG, bump to 0.3.1
git tag v0.3.1
git push --tags
```
