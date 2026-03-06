# Release process

## Cutting a release

1. **Version:** Set `__version__` in `sovereign_os/__init__.py` and `version` in `pyproject.toml` (e.g. `0.4.0`).
2. **Changelog:** In [CHANGELOG.md](../CHANGELOG.md), move entries from `[Unreleased]` into a new section `[0.4.0] — YYYY-MM-DD`.
3. **Commit and tag:**
   ```bash
   git add sovereign_os/__init__.py pyproject.toml CHANGELOG.md
   git commit -m "chore: release v0.4.0"
   git tag -a v0.4.0 -m "Release v0.4.0"
   git push && git push origin v0.4.0
   ```
4. **GitHub Release:** In the repo, **Releases** → **Draft a new release** → choose tag `v0.4.0`, paste the CHANGELOG section as release notes, publish.

## Suggested release notes (short)

- **Title:** Sovereign-OS v0.4.0
- **Body:** Paste the `## [0.4.0]` block from CHANGELOG. Add a one-liner: “One command. One Charter. A digital corporation that thinks, spends, and answers for every token.”

## Changelog format

Keep an **`[Unreleased]`** section at the top for ongoing changes. When releasing, rename it to the version and date, and add a new empty `[Unreleased]` section.
