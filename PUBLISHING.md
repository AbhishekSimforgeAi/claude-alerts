# Publishing checklist

Pre-flight items before pushing this repo to GitHub (or any other public host)
for the first time. Crossed-off items are already done.

## Required before push

- [ ] **Decide the public repo URL.** Owner + repo name (e.g.
      `github.com/<owner>/claude-alerts`). The downstream items here all
      need it.
- [ ] **Fill in `[project.urls]` in `pyproject.toml:36-38`.** Currently a
      placeholder comment. Uncomment and set `Homepage` and `Issues` to the
      real URLs. PyPI metadata reads these if/when the package is ever
      published.
- [ ] **Update `README.md` install line** (`git clone <this-repo>`) to the
      real repo URL.
- [ ] **Decide what to do with `docs/superpowers/plans/2026-04-12-bugfix-release-v0.1.1.md`.**
      Currently untracked, predates this session, documents the v0.1.1 fix.
      Three options: commit (preserves design history), delete (it's
      already in git via the released code), or leave untracked (gitignored
      in practice via no-op).

## Optional but recommended

- [ ] **Tag `v0.4.0`.** The current `pyproject.toml` version is `0.4.0`
      and HEAD has the OSS-prep commit (`11775d5`); a `git tag -a v0.4.0`
      makes the historical release findable.
- [ ] **Push branch then merge to main.** Current work is on
      `feat/overlay-idle-pause`; there's no main on the local repo yet,
      so this becomes the initial push (`git push -u origin
      feat/overlay-idle-pause:main` or similar — depends on hosting choice).
- [ ] **Create a GitHub Release** for `v0.4.0` with the release-notes
      summary (PermissionRequest/Elicitation handling, persistence,
      rate-limit dashboard).
- [ ] **CHANGELOG.md.** Not present today. Could be derived from
      `git log --oneline` against the four version bumps.
- [ ] **CONTRIBUTING.md.** README has a short "Contributing" section that
      could grow into a dedicated file if/when the project picks up
      external contributors.
- [ ] **Issue/PR templates.** `.github/ISSUE_TEMPLATE/` for bug reports
      and feature requests.
- [ ] **CI.** A GitHub Actions workflow that runs `pytest tests/` (skip
      the xvfb e2e on the default runner unless installed).

## Pre-push sanity check

```sh
# Tests pass.
.venv/bin/pytest tests/ --ignore=tests/test_e2e_xvfb.py

# No accidentally-tracked secrets or local state.
git ls-files | grep -E '\.(env|credentials|local\.json)$' || echo "ok"

# Working tree clean except expected items.
git status
```

## Audit deferrals

The following audit findings were intentionally NOT addressed in
`11775d5` (chore: prepare repo for open-source release) — they're
LOW-severity, cosmetic, or judgment calls. Tracked here so they don't
get forgotten.

- Promote hardcoded constants to `Config`:
  `IDLE_SWEEP_INTERVAL_S` / `IDLE_MAX_AGE_S` in `daemon.py:26-27`,
  `SIDECAR_STALE_AFTER_S` in `dashboard.py`, `BACKGROUND_TASK_TOOLS` and
  `TERMINAL_WM_CLASSES` lists.
- `_short_cwd` and `_short_id` use byte-length, not visible width.
  Multibyte / wide CJK / emoji paths render ragged but don't crash.
- `evict_idle` mixes `time.time()` (wall-clock) and `time.monotonic()`.
  Wall-clock leaps survive but could surprise.
- `--verbose` / `-v` CLI flag for stderr logging (currently only
  `CLAUDE_ALERTS_DEBUG=1` env var).
- Settings-file mode preservation in `install-hooks.py` would be
  cleaner as `try/finally` than the current snapshot/restore flow.

None of these block the OSS release.
