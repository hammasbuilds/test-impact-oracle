"""Command line: build a map, select tests for a diff, or measure the selector."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from test_impact_oracle import bench as bench_mod
from test_impact_oracle import mapping, select, suite

MAP_NAME = ".tio-map.json"


def _say(msg: str) -> None:
    print(f"  .. {msg}", file=sys.stderr, flush=True)


# Changes to these cannot alter what a test does, so they never force a full run.
_DOC_SUFFIXES = (".md", ".rst", ".adoc")
_DOC_NAMES = ("LICENSE", "LICENCE", "COPYING", "AUTHORS", "CHANGELOG", ".gitignore")


# Tool output and environments, which an un-ignored working tree may still contain.
_NOISE_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".tox", ".nox", ".git"}


def _is_doc(path: str) -> bool:
    """True for files whose change cannot alter a test: docs, caches, this tool's own map."""
    parts = path.split("/")
    name = parts[-1]
    if name == MAP_NAME or name.endswith((".pyc", ".pyo")) or _NOISE_DIRS & set(parts[:-1]):
        return True
    return (
        path.lower().endswith(_DOC_SUFFIXES)
        or name.upper().startswith(tuple(n.upper() for n in _DOC_NAMES))
        or path.startswith("docs/")
    )


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def changed_from_git(repo: Path, ref: str) -> select.Change:
    """Files and lines touched since `ref` (working tree included, untracked files too).

    Paths are relative to `repo`, which need not be the top of the git repository - a package
    in a monorepo subdirectory is mapped with paths relative to itself, and `--relative`
    makes the diff agree.

    Only the *new* side carries line numbers. A deleted file, a binary file and a non-Python
    file are recorded as "somewhere in it", which the selector resolves conservatively.
    Documentation files are dropped: they cannot change what a test does.
    """
    change = select.Change()
    names = _git_out(repo, "diff", "--name-only", "--relative", ref, "--").splitlines()
    names += _git_out(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    for name in names:
        name = name.strip()
        if name and not _is_doc(name):
            change.add(name)

    diff = _git_out(repo, "diff", "--unified=0", "--relative", ref, "--", "*.py")
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None
        elif line.startswith("@@") and current:
            # @@ -old,n +new,m @@
            try:
                new_part = line.split("+", 1)[1].split("@@")[0].strip()
                start, _, count = new_part.partition(",")
                start_i = int(start)
                count_i = int(count) if count else 1
            except (ValueError, IndexError):
                continue
            for i in range(start_i, start_i + max(count_i, 1)):
                change.add(current, i)
    return change


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="test-impact-oracle",
        description="Run only the tests a change could affect, and measure what that skips.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("map", help="trace the suite once and write the test-to-line map")
    b.add_argument("repo", type=Path)
    b.add_argument("--target", default="tests")
    b.add_argument("--python", default="", help="interpreter for the target suite")
    b.add_argument("--out", type=Path, default=None, help="default: <repo>/.tio-map.json")
    b.add_argument("--quiet", action="store_true")

    s = sub.add_parser("select", help="which tests a change could affect")
    s.add_argument("repo", type=Path)
    s.add_argument("--map", type=Path, default=None, help="default: <repo>/.tio-map.json")
    s.add_argument("--since", default="HEAD", help="git ref to diff against")
    s.add_argument("--target", default="tests")
    s.add_argument("--python", default="")
    s.add_argument("--run", action="store_true", help="run the selected tests")
    s.add_argument("--why", action="store_true", help="print why each test was selected")
    s.add_argument("--json", action="store_true", help="print the selection as JSON")

    m = sub.add_parser("bench", help="score the selector against mutations")
    m.add_argument("repos", nargs="+", type=Path)
    m.add_argument("--mutants", type=int, default=12)
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--python", default="", help="one interpreter for all of them")
    m.add_argument("--workdir", type=Path, default=None)
    m.add_argument("--json", type=Path)
    m.add_argument("--quiet", action="store_true")

    a = p.parse_args(argv)
    say = None if getattr(a, "quiet", False) else _say

    if a.cmd == "map":
        if not a.repo.exists():
            print(f"no such path: {a.repo}", file=sys.stderr)
            return 2
        if say:
            say(f"tracing {a.repo} - this runs the whole suite once and is slow")
        result = mapping.build(a.repo, a.target, python=a.python)
        if not result.traced_ok:
            print(f"could not build a map: {result.error}", file=sys.stderr)
            return 1
        a.out = a.out or a.repo / MAP_NAME
        result.save(a.out)
        s_ = result.summary()
        print(
            f"{s_['tests']} tests, {s_['files']} files, {s_['lines']} lines, "
            f"{s_['seconds']}s -> {a.out}"
        )
        return 0

    if a.cmd == "select":
        a.map = a.map or a.repo / MAP_NAME
        if not a.map.exists():
            print(f"no map at {a.map}; run `tio map` first", file=sys.stderr)
            return 2
        m_ = mapping.Mapping.load(a.map)
        try:
            change = changed_from_git(a.repo, a.since)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2

        # A mapped file that changed but is not in the diff means the map was traced on a
        # different tree than `--since`. Its line numbers cannot be trusted, so the whole
        # file counts as changed.
        stale = [f for f in m_.stale_files(a.repo) if f not in change.lines]
        for f in stale:
            change.add(f)
        if stale:
            print(
                f"warning: the map is stale for {len(stale)} file(s) "
                f"({', '.join(stale[:3])}); treating them as changed. Rebuild with `tio map`.",
                file=sys.stderr,
            )

        if not change.lines:
            if a.json:
                print(json.dumps({"selected": 0, "total": None, "tests": [], "since": a.since}))
            else:
                print(f"no code or data changes since {a.since}")
            return 0

        all_ids, err = suite.collect_with_error(a.repo, a.target, python=a.python)
        if not all_ids:
            print(f"could not collect tests in {a.repo / a.target}: {err}", file=sys.stderr)
            return 2
        sel = select.select(m_, change, set(all_ids))
        info = sel.summary()
        if a.json:
            info["since"] = a.since
            info["stale_files"] = stale
            print(json.dumps(info, indent=2))
        else:
            print(
                f"{info['selected']}/{info['total']} tests selected "
                f"({info['reduction']:.0%} of the suite skipped)"
            )
            if sel.unknown_files:
                print(
                    "  everything was selected because these files are not in the map: "
                    + ", ".join(sel.unknown_files[:5])
                )
            if sel.non_python_files:
                print(
                    "  everything was selected because non-Python files changed: "
                    + ", ".join(sel.non_python_files[:5])
                )
            for t in sorted(sel.selected):
                print(f"  {t}" + (f"   <- {sel.reasons.get(t, '')}" if a.why else ""))

        if a.run:
            r = suite.run(a.repo, a.target, tests=sorted(sel.selected), python=a.python)
            if not r.ran:
                print(f"\nselected tests could not be run: {r.error}", file=sys.stderr)
                return 2
            print(
                f"\n{len(r.failed)} failed in {r.seconds:.1f}s",
                file=sys.stderr if a.json else sys.stdout,
            )
            return 1 if r.failed else 0
        return 0

    repos = [(r, a.python) for r in a.repos]
    missing = [r for r, _ in repos if not r.exists()]
    if missing:
        print(f"no such path: {missing[0]}", file=sys.stderr)
        return 2
    res = bench_mod.run(repos, mutants=a.mutants, seed=a.seed, workdir=a.workdir, progress=say)
    print(bench_mod.text(res))
    if a.json:
        bench_mod.write_json(res, a.json)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
