"""Command line: build a map, select tests for a diff, or measure the selector."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from test_impact_oracle import bench as bench_mod
from test_impact_oracle import mapping, select, suite


def _say(msg: str) -> None:
    print(f"  .. {msg}", file=sys.stderr, flush=True)


def changed_from_git(repo: Path, ref: str) -> select.Change:
    """Lines touched since `ref`, read from `git diff`.

    Only the *new* side matters. A line deleted from a file no longer exists, so no test can
    execute it - but the file is still recorded, which puts every test that touched that file
    into the selection. Deletions are exactly where line-level reasoning stops working.
    """
    change = select.Change()
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--unified=0", ref, "--", "*.py"],
        capture_output=True,
        text=True,
        check=False,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()[:200]}")

    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            if current == "/dev/null":
                current = None
            elif current:
                change.lines.setdefault(current, set())
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
    b.add_argument("--out", type=Path, default=Path(".tio-map.json"))
    b.add_argument("--quiet", action="store_true")

    s = sub.add_parser("select", help="which tests a change could affect")
    s.add_argument("repo", type=Path)
    s.add_argument("--map", type=Path, default=Path(".tio-map.json"))
    s.add_argument("--since", default="HEAD", help="git ref to diff against")
    s.add_argument("--target", default="tests")
    s.add_argument("--python", default="")
    s.add_argument("--run", action="store_true", help="run the selected tests")
    s.add_argument("--why", action="store_true", help="print why each test was selected")

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
        result.save(a.out)
        s_ = result.summary()
        print(
            f"{s_['tests']} tests, {s_['files']} files, {s_['lines']} lines, "
            f"{s_['seconds']}s -> {a.out}"
        )
        return 0

    if a.cmd == "select":
        if not a.map.exists():
            print(f"no map at {a.map}; run `tio map` first", file=sys.stderr)
            return 2
        m_ = mapping.Mapping.load(a.map)
        try:
            change = changed_from_git(a.repo, a.since)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not change.lines:
            print(f"no Python changes since {a.since}")
            return 0

        all_tests = set(suite.collect(a.repo, a.target, python=a.python))
        sel = select.select(m_, change, all_tests)
        info = sel.summary()
        print(
            f"{info['selected']}/{info['total']} tests selected "
            f"({info['reduction']:.0%} of the suite skipped)"
        )
        if sel.unknown_files:
            print(
                "  everything was selected because these files are not in the map: "
                + ", ".join(sel.unknown_files[:5])
            )
        for t in sorted(sel.selected):
            print(f"  {t}" + (f"   <- {sel.reasons.get(t, '')}" if a.why else ""))

        if a.run:
            r = suite.run(a.repo, a.target, tests=sorted(sel.selected), python=a.python)
            if not r.ran:
                print(f"\nselected tests could not be run: {r.error}", file=sys.stderr)
                return 2
            print(f"\n{len(r.failed)} failed in {r.seconds:.1f}s")
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
