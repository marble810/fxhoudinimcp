#!/usr/bin/env python3
"""Deterministic upstream-sync tooling for the fxhoudinimcp fork.

Stdlib only. Run from anywhere inside the repository with the project's Python
(the one that has the dev extras installed, e.g. ``.venv``)::

    python .fork/sync.py auto [--push]    # THE LOOP: fetch, branch, merge, verify, land
    python .fork/sync.py finish [--push]  # after manual triage on a fork-sync/* branch
    python .fork/sync.py status           # what upstream has that we lack
    python .fork/sync.py merge            # fetch + merge + auto-resolve + commit
    python .fork/sync.py check            # fork-boundary invariants (fast, no tests)
    python .fork/sync.py verify           # check + ruff + pytest, vs an upstream baseline
    python .fork/sync.py restore-hook     # rebuild __main__.py = upstream + hook
    python .fork/sync.py restore-upstream PATH...  # reset upstream-owned files

Exit codes: 0 success / nothing to do, 1 a check or test failed,
2 usage or environment error, 3 merge needs a human (see .fork/AGENT.md).

The fork's whole footprint on upstream is defined by the constants below.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# -- fork boundary (edit only together with .fork/AGENT.md) ------------------

UPSTREAM_REMOTE = "upstream"
UPSTREAM_BRANCH = "main"

# Paths the fork owns outright. Upstream never has them; on conflict keep ours.
FORK_OWNED_PREFIXES = (
    ".fork/",
    "python/fxhoudinimcp/_fork/",
    "tests/test_fork_",
)

# Upstream files the fork touches, and only by inserting marked hook lines.
HOOK_MARKER = "# fork:progressive"
HOOKED_FILE = "python/fxhoudinimcp/__main__.py"
HOOK_FUNCTION = "main"
HOOK_LINES = (
    f"from fxhoudinimcp._fork import progressive as _fork_progressive  {HOOK_MARKER}",
    "",  # ruff format wants a blank line after an import
    f"_fork_progressive.install()  {HOOK_MARKER}",
)

# Lint/format only what the fork wrote; upstream lints its own code.
FORK_LINT_PATHS = (".fork", "python/fxhoudinimcp/_fork", HOOKED_FILE)
FORK_TEST_GLOB = "tests/test_fork_*.py"

# Untracked build outputs a fresh checkout needs before tests can import.
GENERATED_FILES = ("python/fxhoudinimcp/_version.py",)

# ---------------------------------------------------------------------------

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_HUMAN = 0, 1, 2, 3


class SyncError(Exception):
    """An environment or usage problem; message is shown, exit code 2."""


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SyncError("not inside a git repository")
    return Path(out.stdout.strip())


ROOT: Path


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc


def say(message: str) -> None:
    print(message, flush=True)


def is_fork_owned(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in FORK_OWNED_PREFIXES)


def upstream_ref(args: argparse.Namespace) -> str:
    return getattr(args, "ref", None) or f"{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}"


def show(ref: str, path: str) -> str | None:
    proc = git("show", f"{ref}:{path}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def lf(text: str) -> str:
    return text.replace("\r\n", "\n")


# -- hook handling -------------------------------------------------------------


def strip_hook(source: str) -> str:
    """*source* without the hook: marker lines and blank lines sandwiched between them."""
    lines = source.splitlines(keepends=True)
    kept = []
    for index, line in enumerate(lines):
        if HOOK_MARKER in line:
            continue
        sandwiched = (
            not line.strip()
            and 0 < index < len(lines) - 1
            and HOOK_MARKER in lines[index - 1]
            and HOOK_MARKER in lines[index + 1]
        )
        if sandwiched:
            continue
        kept.append(line)
    return "".join(kept)


def inject_hook(source: str) -> str:
    """Insert HOOK_LINES as the first statements of HOOK_FUNCTION (after any docstring)."""
    source = lf(strip_hook(source))
    tree = ast.parse(source)
    func = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == HOOK_FUNCTION
        ),
        None,
    )
    if func is None:
        raise SyncError(
            f"{HOOKED_FILE} no longer defines a top-level {HOOK_FUNCTION}(); "
            "the entry point moved upstream -- needs a human (see .fork/AGENT.md)."
        )
    lines = source.splitlines(keepends=True)
    body = func.body
    first = body[0]
    has_doc = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    if has_doc and len(body) > 1:
        anchor = body[1]
    elif has_doc:
        anchor = None
    else:
        anchor = first
    if anchor is not None:
        start = min([anchor.lineno, *(d.lineno for d in getattr(anchor, "decorator_list", []))])
        insert_at = start - 1
        indent = re.match(r"[ \t]*", lines[insert_at]).group(0)
    else:
        insert_at = first.end_lineno
        indent = re.match(r"[ \t]*", lines[first.lineno - 1]).group(0)
    hook = [f"{indent}{text}\n" if text else "\n" for text in HOOK_LINES]
    return "".join(lines[:insert_at] + hook + lines[insert_at:])


def hook_problems(source: str, upstream_source: str | None) -> list[str]:
    problems = []
    source = lf(source)
    marked = [line.strip() for line in source.splitlines() if HOOK_MARKER in line]
    expected = [text for text in HOOK_LINES if text]
    if marked != expected:
        problems.append(
            f"{HOOKED_FILE}: expected exactly these hook lines {expected}, found {marked}"
        )
    elif inject_hook(source) != source:
        problems.append(
            f"{HOOKED_FILE}: hook lines are not the first statements of {HOOK_FUNCTION}()"
        )
    if upstream_source is not None and strip_hook(source) != lf(upstream_source):
        problems.append(
            f"{HOOKED_FILE}: differs from upstream beyond the hook lines "
            "(run: python .fork/sync.py restore-hook)"
        )
    return problems


def write_hooked_from(ref: str) -> None:
    upstream_source = show(ref, HOOKED_FILE)
    if upstream_source is None:
        raise SyncError(
            f"{HOOKED_FILE} does not exist at {ref}; the entry point moved upstream -- "
            "needs a human (see .fork/AGENT.md)."
        )
    (ROOT / HOOKED_FILE).write_text(inject_hook(upstream_source), encoding="utf-8", newline="\n")


# -- boundary check --------------------------------------------------------------


def changed_vs(ref: str, worktree: bool) -> list[str]:
    """Tracked paths whose content differs from *ref* (working tree, or HEAD)."""
    args = ["diff", "--name-only", ref] if worktree else ["diff", "--name-only", ref, "HEAD"]
    return sorted({n for n in git(*args).stdout.splitlines() if n})


def boundary_problems(ref: str, worktree: bool = True) -> list[str]:
    problems = []
    for path in changed_vs(ref, worktree):
        if is_fork_owned(path) or path == HOOKED_FILE:
            continue
        problems.append(
            f"{path}: upstream-owned file differs from {ref} "
            f"(run: python .fork/sync.py restore-upstream {path})"
        )
    hooked = ROOT / HOOKED_FILE
    if not hooked.exists():
        problems.append(f"{HOOKED_FILE}: missing")
    else:
        problems += hook_problems(hooked.read_text(encoding="utf-8"), show(ref, HOOKED_FILE))
    return problems


def merged_base(args: argparse.Namespace) -> str:
    """The upstream commit the fork last merged: merge-base of HEAD and the upstream ref.

    The boundary is judged against this, not the upstream tip, so upstream
    commits that are simply not merged yet never count as fork changes.
    """
    ref = upstream_ref(args)
    proc = git("merge-base", "HEAD", ref, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SyncError(f"no merge-base between HEAD and {ref} (is the remote fetched?)")
    return proc.stdout.strip()


def cmd_check(args: argparse.Namespace) -> int:
    base = merged_base(args)
    short = base[:10]
    problems = boundary_problems(base)
    if problems:
        say(f"CHECK FAILED -- fork boundary violated (compared with upstream {short}):")
        for problem in problems:
            say(f"  - {problem}")
        return EXIT_FAIL
    say(
        f"CHECK OK -- only fork-owned paths and the {HOOK_MARKER} hook differ from upstream {short}."
    )
    return EXIT_OK


# -- status / merge ----------------------------------------------------------------


def fetch() -> None:
    say(f"Fetching {UPSTREAM_REMOTE} ...")
    git("fetch", UPSTREAM_REMOTE, "--prune")


def behind_count(ref: str) -> int:
    return int(git("rev-list", "--count", f"HEAD..{ref}").stdout.strip() or 0)


def cmd_status(args: argparse.Namespace) -> int:
    ref = upstream_ref(args)
    if not args.no_fetch:
        fetch()
    count = behind_count(ref)
    say(f"{count} upstream commit(s) not yet merged from {ref}.")
    if count:
        say(git("log", "--oneline", "--no-decorate", f"HEAD..{ref}").stdout.rstrip())
    return EXIT_OK


def require_clean() -> None:
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise SyncError(f"working tree has uncommitted changes:\n{dirty}")
    if Path(ROOT, git("rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()).exists():
        raise SyncError("a merge is already in progress (git merge --abort to start over)")


def unmerged_paths() -> list[str]:
    out = git("diff", "--name-only", "--diff-filter=U").stdout
    return [p for p in out.splitlines() if p]


def resolve_conflict(path: str, ref: str) -> str:
    """Resolve one conflicted path by the fork's ownership rules; returns the rule used."""
    if path == HOOKED_FILE:
        write_hooked_from(ref)
        git("add", "--", path)
        return "upstream version + re-inserted hook"
    if is_fork_owned(path):
        if git("checkout", "--ours", "--", path, check=False).returncode != 0:
            git("rm", "-q", "--", path)
            return "fork-owned, deleted on our side -> removed"
        git("add", "--", path)
        return "fork-owned -> kept ours"
    if git("checkout", "--theirs", "--", path, check=False).returncode != 0:
        git("rm", "-q", "--", path)
        return "upstream deleted it -> removed"
    git("add", "--", path)
    return "upstream-owned -> took upstream"


def cmd_merge(args: argparse.Namespace) -> int:
    require_clean()
    if not args.no_fetch:
        fetch()
    ref = git("rev-parse", upstream_ref(args)).stdout.strip()
    if behind_count(ref) == 0:
        say(f"Up to date with {ref}; nothing to merge.")
        return EXIT_OK

    say(f"Merging {ref} ...")
    merge = git("merge", "--no-ff", "--no-commit", ref, check=False)
    conflicted = unmerged_paths()
    if merge.returncode != 0 and not conflicted:
        git("merge", "--abort", check=False)
        say(merge.stdout + merge.stderr)
        say("MERGE FAILED without file conflicts -- needs a human (see .fork/AGENT.md).")
        return EXIT_HUMAN

    notes = []
    try:
        for path in conflicted:
            rule = resolve_conflict(path, ref)
            notes.append(f"conflict {path}: {rule}")
            say(f"  resolved {path}: {rule}")

        # Enforce the boundary even where git merged silently.
        write_hooked_from(ref)
        git("add", "--", HOOKED_FILE)
        for path in changed_vs(ref, worktree=True):
            if is_fork_owned(path) or path == HOOKED_FILE:
                continue
            if show(ref, path) is None:
                git("rm", "-q", "-f", "--", path)
            else:
                git("checkout", ref, "--", path)
            notes.append(f"reset {path} to upstream")
            say(f"  reset {path} to upstream")
    except SyncError as exc:
        git("merge", "--abort", check=False)
        say(f"MERGE ABORTED: {exc}")
        return EXIT_HUMAN

    remaining = unmerged_paths()
    if remaining:
        git("merge", "--abort", check=False)
        say(f"MERGE ABORTED: still unmerged: {remaining} -- needs a human.")
        return EXIT_HUMAN

    message = f"Merge {ref} into fork\n\nAutomated by .fork/sync.py merge."
    if notes:
        message += "\n\n" + "\n".join(f"- {note}" for note in notes)
    git("commit", "-q", "-m", message)
    say(f"Merged {ref} as {git('rev-parse', '--short', 'HEAD').stdout.strip()}.")
    return cmd_check(args)


# -- repairs ---------------------------------------------------------------------


def cmd_restore_hook(args: argparse.Namespace) -> int:
    write_hooked_from(merged_base(args))
    say(f"Rewrote {HOOKED_FILE} = upstream version + hook. Review with: git diff")
    return EXIT_OK


def cmd_restore_upstream(args: argparse.Namespace) -> int:
    ref = merged_base(args)
    for path in args.paths:
        path = path.replace("\\", "/")
        if is_fork_owned(path) or path == HOOKED_FILE:
            raise SyncError(f"{path} is not upstream-owned; refusing to reset it")
        if show(ref, path) is None:
            git("rm", "-q", "-f", "--ignore-unmatch", "--", path)
            target = ROOT / path
            if target.exists():
                target.unlink()
            say(f"removed {path} (absent upstream)")
        else:
            git("checkout", ref, "--", path)
            say(f"reset {path} to {ref}")
    return EXIT_OK


# -- verify --------------------------------------------------------------------------

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)")


def run_pytest(cwd: Path, extra_env: dict[str, str] | None = None) -> tuple[int, set[str], str]:
    env = dict(os.environ)
    env.pop("FXHOUDINIMCP_EXPOSURE", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-m",
            "not integration",
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = proc.stdout + proc.stderr
    failed = {m.group(1) for line in output.splitlines() if (m := _FAILED_RE.match(line))}
    return proc.returncode, failed, output


def baseline_failures(ref: str) -> set[str]:
    tmp = Path(tempfile.mkdtemp(prefix="fork-baseline-"))
    worktree = tmp / "upstream"
    git("worktree", "add", "--detach", str(worktree), ref)
    try:
        # Build-generated, untracked files the package needs to import.
        for generated in GENERATED_FILES:
            source = ROOT / generated
            if source.exists() and not (worktree / generated).exists():
                shutil.copy2(source, worktree / generated)
        say(f"Running upstream baseline tests at {ref[:10]} ...")
        _, failed, output = run_pytest(worktree, {"PYTHONPATH": str(worktree / "python")})
        if "error during collection" in output or "errors during collection" in output:
            say(output[-3000:])
            raise SyncError(
                "the upstream baseline test run could not even collect tests (see above); "
                "fix the environment (python -m pip install -e .[dev]) and rerun verify"
            )
        return failed
    finally:
        git("worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def run_ruff() -> list[str]:
    problems = []
    paths = [p for p in FORK_LINT_PATHS if (ROOT / p).exists()]
    paths += sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(FORK_TEST_GLOB))
    for cmd in (["check", *paths], ["format", "--check", *paths]):
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", *cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            problems.append(f"ruff {cmd[0]} failed:\n{(proc.stdout + proc.stderr).strip()}")
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    ref = merged_base(args)
    status = cmd_check(args)

    lint = run_ruff()
    for problem in lint:
        say(f"LINT FAILED -- {problem}")
    if lint:
        say("  fix: python -m ruff format " + " ".join(FORK_LINT_PATHS))
        say("       python -m ruff check --fix " + " ".join(FORK_LINT_PATHS))

    say("Running the test suite on the fork ...")
    code, failed, output = run_pytest(ROOT)
    summary = output.strip().splitlines()[-1:] or ["(no output)"]
    say(f"  fork: {summary[0]}")
    if code not in (0, 1) and not failed:
        say(output[-4000:])
        say("TESTS COULD NOT RUN (collection/import error) -- see output above.")
        return EXIT_FAIL

    regressions = set(failed)
    if failed and not args.no_baseline:
        regressions -= baseline_failures(ref)
        inherited = sorted(failed - regressions)
        if inherited:
            say("  failing on upstream too (not the fork's problem, ignore):")
            for test in inherited:
                say(f"    - {test}")

    if regressions:
        fork_tests = sorted(t for t in regressions if Path(t.split("::")[0]).match(FORK_TEST_GLOB))
        other = sorted(regressions - set(fork_tests))
        say("TESTS FAILED -- regressions introduced by the fork:")
        for test in fork_tests:
            say(f"  - [fork test]     {test}")
        for test in other:
            say(f"  - [upstream test] {test}")
        say("Failure details (tail):")
        say(output[-6000:])
        return EXIT_FAIL

    if status or lint:
        return EXIT_FAIL
    say("VERIFY OK -- boundary intact, fork code lint-clean, no test regressions.")
    return EXIT_OK


# -- auto / finish (the loop entry points) --------------------------------------------

SYNC_BRANCH_PREFIX = "fork-sync/"


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def refresh_dependencies(since: str) -> None:
    """Reinstall the package (dev extras) if the merge touched pyproject.toml."""
    if not git("diff", "--name-only", since, "HEAD", "--", "pyproject.toml").stdout.strip():
        return
    say("pyproject.toml changed upstream; reinstalling the package with dev extras ...")
    commands = [
        [sys.executable, "-m", "pip", "install", "-q", "-e", ".[dev]"],
        ["uv", "pip", "install", "-q", "--python", sys.executable, "-e", ".[dev]"],
    ]
    for cmd in commands:
        if cmd[0] == "uv" and shutil.which("uv") is None:
            continue
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return
    raise SyncError(
        "could not reinstall dependencies with pip or uv; run "
        "'python -m pip install -e .[dev]' (or 'uv pip install -e .[dev]') yourself"
    )


def land(branch: str, main_branch: str, push: bool) -> int:
    """Fast-forward *main_branch* to the verified sync *branch*, optionally push."""
    git("switch", main_branch)
    proc = git("merge", "--ff-only", branch, check=False)
    if proc.returncode != 0:
        git("switch", branch, check=False)
        say(f"LAND FAILED: {main_branch} cannot fast-forward to {branch} -- needs a human.")
        return EXIT_HUMAN
    git("branch", "-d", branch, check=False)
    say(f"Landed on {main_branch} at {git('rev-parse', '--short', 'HEAD').stdout.strip()}.")
    if push:
        push_proc = git("push", "origin", main_branch, check=False)
        if push_proc.returncode != 0:
            say(f"PUSH FAILED:\n{push_proc.stderr.strip()}")
            return EXIT_HUMAN
        say(f"Pushed {main_branch} to origin.")
    return EXIT_OK


def cmd_auto(args: argparse.Namespace) -> int:
    require_clean()
    main_branch = args.branch
    if current_branch() != main_branch:
        raise SyncError(f"run auto from '{main_branch}' (currently on '{current_branch()}')")

    say("Fetching origin and upstream ...")
    git("fetch", "origin", "--prune")
    fetch()
    if git("merge", "--ff-only", f"origin/{main_branch}", check=False).returncode != 0:
        say(f"AUTO STOPPED: local {main_branch} has diverged from origin -- needs a human.")
        return EXIT_HUMAN

    ref = upstream_ref(args)
    if behind_count(ref) == 0:
        say(f"UP TO DATE -- {main_branch} already contains {ref}. Nothing to do.")
        return EXIT_OK

    tip = git("rev-parse", "--short", ref).stdout.strip()
    branch = f"{SYNC_BRANCH_PREFIX}{tip}"
    if git("rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0:
        say(f"AUTO STOPPED: branch {branch} already exists from an earlier run.")
        say(
            f"  Continue it:  git switch {branch}  (fix, commit, then: python .fork/sync.py finish)"
        )
        say(f"  Or discard:   git branch -D {branch}  and rerun auto")
        return EXIT_HUMAN
    before = git("rev-parse", "HEAD").stdout.strip()
    git("switch", "-c", branch)
    say(f"Working on branch {branch}.")

    args.no_fetch = True
    code = cmd_merge(args)
    if code == EXIT_HUMAN and git("rev-parse", "HEAD").stdout.strip() == before:
        git("switch", main_branch)
        git("branch", "-D", branch, check=False)
        return code
    if code not in (EXIT_OK, EXIT_FAIL):
        return code

    refresh_dependencies(before)
    if cmd_verify(args) != EXIT_OK:
        say("")
        say(f"TRIAGE NEEDED -- you are on branch {branch} with the merge committed.")
        say("  Follow .fork/AGENT.md section 'Triage'; when fixed and committed run:")
        say(f"  python .fork/sync.py finish{' --push' if args.push else ''}")
        return EXIT_FAIL
    return land(branch, main_branch, args.push)


def cmd_finish(args: argparse.Namespace) -> int:
    branch = current_branch()
    if not branch.startswith(SYNC_BRANCH_PREFIX):
        raise SyncError(f"finish runs on a {SYNC_BRANCH_PREFIX}* branch (currently on '{branch}')")
    require_clean()
    if cmd_verify(args) != EXIT_OK:
        say("FINISH REFUSED -- verify still fails; keep triaging (see .fork/AGENT.md).")
        return EXIT_FAIL
    return land(branch, args.branch, args.push)


# -- main ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ref", help="upstream ref to compare/merge (default upstream/main)")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, text in (
        ("auto", cmd_auto, "the loop: fetch, branch, merge, verify, land on main"),
        ("finish", cmd_finish, "after triage on a fork-sync/* branch: verify, land on main"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--branch", default="main", help="the fork's main branch (default main)")
        p.add_argument("--push", action="store_true", help="push the landed branch to origin")
        p.add_argument("--no-baseline", action="store_true", help=argparse.SUPPRESS)
        p.set_defaults(func=func)

    p = sub.add_parser("status", help="list upstream commits not merged yet")
    p.add_argument("--no-fetch", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("merge", help="fetch, merge upstream, auto-resolve, commit, check")
    p.add_argument("--no-fetch", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("check", help="verify the fork boundary invariants")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("verify", help="check + ruff on fork code + pytest vs upstream baseline")
    p.add_argument("--no-baseline", action="store_true", help="do not subtract upstream failures")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore-hook", help=f"rewrite {HOOKED_FILE} as upstream + hook")
    p.set_defaults(func=cmd_restore_hook)

    p = sub.add_parser("restore-upstream", help="reset upstream-owned files to upstream")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_restore_upstream)
    return parser


def main(argv: list[str] | None = None) -> int:
    global ROOT
    args = build_parser().parse_args(argv)
    try:
        ROOT = repo_root()
        return args.func(args)
    except SyncError as exc:
        say(f"ERROR: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
