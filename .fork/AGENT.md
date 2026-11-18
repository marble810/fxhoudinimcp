# Upstream-sync agent instructions

You keep the fork `marble810/fxhoudinimcp` (remote `origin`) up to date with
upstream `healkeiser/fxhoudinimcp` (remote `upstream`). A script does the git
work. Your job: run it, read what it prints, and fix only the narrow cases
listed below. When anything else goes wrong, **stop and report**. Don't
improvise.

## 0. What the fork is (read once)

The fork adds one feature, a "progressive disclosure" mode: 4 gateway tools
instead of 200+. It is built so that it **never edits upstream code**:

| Path | Owner | Rule |
|---|---|---|
| `python/fxhoudinimcp/_fork/**` | fork | Feature code. The only code you may edit. |
| `tests/test_fork_*.py` | fork | Feature tests. You may edit them. |
| `.fork/**` | fork | This file and `sync.py`. **Do not edit.** |
| `python/fxhoudinimcp/__main__.py` | upstream + hook | Upstream file plus 3 hook lines marked `# fork:progressive`. The script maintains it. Never hand-edit it. |
| everything else | upstream | **Never edit.** It must stay byte-identical to upstream. |

`python .fork/sync.py check` enforces this table.

## 1. Setup (each run)

- Work from the repository root.
- `PY` = the project's Python: `.venv\Scripts\python.exe` on Windows,
  `.venv/bin/python` elsewhere. Always use `PY`, never a bare `python`.
- If `PY -m pytest --version` fails, run `uv pip install --python PY -e ".[dev]"`
  (or `PY -m pip install -e ".[dev]"`) once.

## 2. The loop (normal path)

```
git switch main
PY .fork/sync.py auto --push
```

Read the **last lines** it prints, then act on the exit code:

| Exit | Last line starts with | Do this |
|---|---|---|
| 0 | `UP TO DATE` | Nothing to do. Report "up to date". Stop. |
| 0 | `Pushed main to origin` / `Landed on main` | Done. Report the merged upstream commits. Stop. |
| 1 | `TRIAGE NEEDED` | Go to **3. Triage**. You are now on a `fork-sync/<sha>` branch. |
| 2 | `ERROR:` | Environment problem (dirty tree, wrong branch, no network, missing deps). Fix only the cases in **5**. Otherwise report it and stop. |
| 3 | `... needs a human` / `AUTO STOPPED` / `MERGE ABORTED` / `LAND FAILED` / `PUSH FAILED` | Report the full output and stop. Don't try to fix it. |

`auto` handles every merge conflict by itself (upstream files take upstream's
version, fork files keep ours, and the hook is re-inserted). **Never resolve
conflicts by hand. Never run `git merge`, `git rebase`, `git reset --hard` or
`git push --force` yourself.**

## 3. Triage (only after `TRIAGE NEEDED`)

`verify` printed one or more blocks. Handle each block in this order. After
each fix, commit it on the current `fork-sync/*` branch:

```
git add -A
git commit -m "fork: <what you fixed>"
PY .fork/sync.py verify
```

Repeat until `VERIFY OK`, then run `PY .fork/sync.py finish --push`.

**Maximum 3 fix attempts in total.** If it is still failing after the third,
go to **4. Give up**.

### 3a. `CHECK FAILED`

Each line ends with `(run: ...)`. Run exactly that command (it is either
`restore-upstream <path>` or `restore-hook`), then commit. Never edit those
files by hand.

### 3b. `LINT FAILED`

Run the two `fix:` commands it prints (`ruff format`, then `ruff check --fix`)
and commit. If `ruff check` still reports something, fix it by hand. Only edit
files under `python/fxhoudinimcp/_fork/` or `tests/test_fork_*.py`.

### 3c. `TESTS FAILED`: lines tagged `[fork test]`

An upstream change broke one of the fork's assumptions. You may edit **only**
`python/fxhoudinimcp/_fork/*.py` and `tests/test_fork_*.py`. Find the row that
matches the error message:

| Error message contains | Cause | Fix |
|---|---|---|
| `ImportError` / `cannot import name` / `AttributeError` naming `fxhoudinimcp._sdk`, `mcp.server...`, `_tool_manager`, `_tools`, `_lowlevel_server`, `_mcp_server` | Upstream or the MCP SDK renamed something | Edit **only** `_fork/upstream_api.py`. Find the new name in upstream (`git log -p -5 -- python/fxhoudinimcp/_sdk.py`) and re-point the import or accessor. Keep the function names that `upstream_api.py` exports unchanged. |
| `test_categories_are_upstream_tool_modules` | Upstream changed how tool modules are laid out | Adjust `tool_category()` in `_fork/upstream_api.py`. |
| `test_catalog_mirrors_full_surface_exactly` with `additionalProperties` | Upstream stopped forcing strict tool schemas | Report it (go to **4**). Don't work around it. |
| `Upstream now registers tools named like the gateway` | Name clash with `houdini_discover/schema/call/batch` | Go to **4**. |
| `test_hook_*`, `test_cli_*`, `test_env_var_*`, `test_full_mode_is_untouched` | Upstream changed `main()` or how it starts the server | Run `PY .fork/sync.py restore-hook`, commit, then verify again. If it still fails, see whether upstream still calls `run_server` from `fxhoudinimcp._sdk` inside `main()` (`git diff HEAD~1 -- python/fxhoudinimcp/__main__.py`). If it doesn't, go to **4**. |
| a test that names a specific value that changed upstream (a count, a summary text) | The test was too specific | Make the test compute that value dynamically. Never weaken a test until it asserts nothing. |

### 3d. `TESTS FAILED`: lines tagged `[upstream test]`

An upstream test passes on pure upstream but fails on the fork. The fork is
changing upstream behaviour, which must not happen. You may **not** edit
upstream tests. If the traceback clearly goes through
`python/fxhoudinimcp/_fork/` and the fix is obvious, fix it there (one
attempt). Otherwise go to **4**.

Lines listed under `failing on upstream too` are not your problem. Ignore them.

## 4. Give up (report, don't push)

```
git push origin HEAD
```

This pushes the `fork-sync/<sha>` branch, **not main**. Then switch back with
`git switch main` and report using the template in **6**. Don't delete the
branch.

## 5. Environment fixes you may do

- `working tree has uncommitted changes`: run `git status`. If every changed
  file is under `__pycache__`, `.pytest_cache` or other build output, run
  `git stash -u`. Otherwise stop and report. Never discard someone's work.
- `run auto from 'main'`: run `git switch main`, then retry once.
- `branch fork-sync/... already exists`: an earlier run is unfinished.
  `git switch` to it and go to **3. Triage** (count it as a new run with 3
  fresh attempts).
- `could not reinstall dependencies` / `No module named ...` for a third-party
  package: run `uv pip install --python PY -e ".[dev]"`, then rerun the same
  command once.
- Network failures on `fetch` or `push`: retry once after 60 s, then report.

## 6. Report template

```
fork-sync result: UP-TO-DATE | MERGED | NEEDS-HUMAN
upstream range: <old>..<new>  (<N> commits)
branch: main | fork-sync/<sha> (pushed)
fixes made: <none | one line per commit you added>
blocking problem: <exact last 20 lines of sync.py output, if NEEDS-HUMAN>
```

## Hard rules (never break these)

1. Never edit a file outside `python/fxhoudinimcp/_fork/` and `tests/test_fork_*.py`.
2. Never edit `.fork/sync.py` or this file.
3. Never resolve git conflicts by hand. Never force-push. Never push to `upstream`.
4. Never delete, skip, or `xfail` a test to get green, and never weaken one
   until it asserts nothing.
5. Never land on main without `VERIFY OK`. Use `finish`, which re-verifies.
6. When unsure, stop and report. A clean "needs human" beats a wrong merge.
