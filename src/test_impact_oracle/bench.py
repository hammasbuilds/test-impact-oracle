"""Score the selector on changes whose consequences are known.

For each mutant: apply it, run the **whole** suite to find out what it really broke, then ask
whether the selection contained every one of those tests.

Two numbers, and they pull against each other:

    safety      of the changes that broke something, how many had *every* broken test selected
    reduction   of the suite, what share was skipped

A selector that returns everything scores 100% safety and 0% reduction. One that returns
nothing scores the reverse. Neither is worth shipping, and quoting either alone is how a
test-selection tool gets adopted and then silently lets a regression through.

**Safety is all-or-nothing per change, not an average of recalls.** A change where nine of
ten failing tests were selected is a change whose regression reached production. Averaging
recall across changes would report that as 90% and make it sound like a near miss.

Mutants that break nothing are excluded from safety and counted separately. They say
something about the suite - it cannot tell that version from this one - and nothing about
the selector, since there was no failure to catch or miss.

Everything runs on a **copy**, with the copy's `src` ahead of the editable install on
PYTHONPATH. The repositories being measured are not touched.
"""

from __future__ import annotations

import json
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from test_impact_oracle import mapping, mutate, select, suite

IGNORE = shutil.ignore_patterns(
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "*.egg-info",
    ".tox",
    ".nox",
)


@dataclass
class Trial:
    mutant: str
    path: str
    line: int
    broke: list[str] = field(default_factory=list)
    selected: int = 0
    total: int = 0
    missed: list[str] = field(default_factory=list)
    reduction: float = 0.0
    full_seconds: float = 0.0
    subset_seconds: float = 0.0
    note: str = ""

    @property
    def safe(self) -> bool:
        return not self.missed


@dataclass
class RepoResult:
    repo: str
    tests: int = 0
    map_seconds: float = 0.0
    baseline_green: bool = True
    trials: list[Trial] = field(default_factory=list)
    error: str = ""

    @property
    def killing(self) -> list[Trial]:
        """Mutants that broke at least one test - the only ones safety can be read from."""
        return [t for t in self.trials if t.broke]

    @property
    def survived(self) -> int:
        return len(self.trials) - len(self.killing)

    def summary(self) -> dict:
        killing = self.killing
        safe = [t for t in killing if t.safe]
        return {
            "repo": self.repo,
            "tests": self.tests,
            "map_seconds": round(self.map_seconds, 1),
            "baseline_green": self.baseline_green,
            "mutants": len(self.trials),
            "broke_something": len(killing),
            "survived": self.survived,
            "safe": len(safe),
            "safety": round(len(safe) / len(killing), 4) if killing else None,
            "mean_reduction": round(sum(t.reduction for t in killing) / len(killing), 4)
            if killing
            else None,
            "mean_full_seconds": round(sum(t.full_seconds for t in killing) / len(killing), 2)
            if killing
            else None,
            "mean_subset_seconds": round(sum(t.subset_seconds for t in killing) / len(killing), 2)
            if killing
            else None,
            "error": self.error,
        }


def source_files(m: mapping.Mapping) -> list[str]:
    """Files worth mutating: covered by the suite, and not themselves tests."""
    return sorted(f for f in m.files if not select.looks_like_test(f) and f.endswith(".py"))


def run_repo(
    repo: Path,
    python: str = "",
    target: str = "tests",
    mutants: int = 12,
    seed: int = 0,
    workdir: Path | None = None,
    timeout: float = 600.0,
    progress=None,
) -> RepoResult:
    say = progress or (lambda *_: None)
    r = RepoResult(repo=repo.name)
    rng = random.Random(seed)

    base = workdir or Path(shutil.os.environ.get("TEMP", "/tmp"))
    work = base / f"tio-{repo.name}-{seed}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    say(f"{repo.name}: copying")
    shutil.copytree(repo, work, ignore=IGNORE)

    try:
        say(f"{repo.name}: tracing the suite")
        m = mapping.build(work, target, python=python, timeout=timeout)
        if not m.traced_ok:
            r.error = m.error
            return r
        r.map_seconds = m.seconds
        r.tests = len(m.tests)

        all_tests = set(suite.collect(work, target, python=python))
        if not all_tests:
            r.error = "collected no tests"
            return r

        say(f"{repo.name}: baseline")
        baseline = suite.run(work, target, python=python, timeout=timeout)
        r.baseline_green = baseline.green
        if not baseline.green:
            # Every later "failure" would be indistinguishable from one that was already
            # there, so the ground truth is not trustworthy and the run is abandoned.
            r.error = f"suite is not green before mutating: {sorted(baseline.failed)[:3]}"
            return r

        files = source_files(m)
        if not files:
            r.error = "no covered non-test source files to mutate"
            return r

        made = 0
        attempts = 0
        # The same site can be drawn twice, and a repeated mutant is a repeated trial: it
        # would count once as a success or a failure for every time it came up, quietly
        # weighting the score towards whichever line happened to be picked often.
        seen: set[tuple[str, int, str, str]] = set()
        while made < mutants and attempts < mutants * 8:
            attempts += 1
            path = rng.choice(files)
            mut = mutate.make(work, path, rng)
            if mut is None:
                continue
            key = (mut.path, mut.line, mut.before, mut.after)
            if key in seen:
                mutate.restore(work, mut)
                continue
            seen.add(key)
            try:
                full = suite.run(work, target, python=python, timeout=timeout)
                if not full.ran:
                    continue
                broke = sorted(full.failed)

                change = select.Change()
                change.add(mut.path, mut.line)
                sel = select.select(m, change, all_tests)

                t = Trial(
                    mutant=mut.describe(),
                    path=mut.path,
                    line=mut.line,
                    broke=broke,
                    selected=len(sel.selected),
                    total=sel.total,
                    missed=sorted(set(broke) - sel.selected),
                    reduction=sel.reduction,
                    full_seconds=full.seconds,
                )
                if broke:
                    subset = suite.run(
                        work,
                        target,
                        tests=sorted(sel.selected),
                        python=python,
                        timeout=timeout,
                    )
                    t.subset_seconds = subset.seconds
                    if subset.ran:
                        # The proof, rather than set arithmetic: run only what was selected
                        # and see whether the breakage actually shows up.
                        unseen = set(broke) - subset.failed - set(t.missed)
                        if unseen:
                            t.note = (
                                f"{len(unseen)} tests failed in the full run and passed in "
                                f"the subset despite being selected"
                            )
                r.trials.append(t)
                made += 1
                say(
                    f"{repo.name}: {made}/{mutants} {mut.describe()[:60]} "
                    f"broke={len(broke)} missed={len(t.missed)}"
                )
            finally:
                mutate.restore(work, mut)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return r


def run(
    repos: list[tuple[Path, str]],
    mutants: int = 12,
    seed: int = 0,
    workdir: Path | None = None,
    progress=None,
) -> dict:
    started = time.time()
    results = [
        run_repo(
            repo,
            python=py,
            mutants=mutants,
            seed=seed,
            workdir=workdir,
            progress=progress,
        )
        for repo, py in repos
    ]

    killing = [t for r in results for t in r.killing]
    safe = [t for t in killing if t.safe]
    return {
        "seconds": round(time.time() - started, 1),
        "repos": [r.summary() for r in results],
        "totals": {
            "repos": len(results),
            "tests": sum(r.tests for r in results),
            "mutants": sum(len(r.trials) for r in results),
            "broke_something": len(killing),
            "survived": sum(r.survived for r in results),
            "safe": len(safe),
            "safety": round(len(safe) / len(killing), 4) if killing else None,
            "mean_reduction": round(sum(t.reduction for t in killing) / len(killing), 4)
            if killing
            else None,
            "full_seconds": round(sum(t.full_seconds for t in killing), 1),
            "subset_seconds": round(sum(t.subset_seconds for t in killing), 1),
        },
        "unsafe": [
            {
                "repo": r.repo,
                "mutant": t.mutant,
                "broke": t.broke,
                "missed": t.missed,
                "selected": t.selected,
                "total": t.total,
            }
            for r in results
            for t in r.killing
            if not t.safe
        ],
        "notes": [
            {"repo": r.repo, "mutant": t.mutant, "note": t.note}
            for r in results
            for t in r.trials
            if t.note
        ],
    }


def text(res: dict) -> str:
    tot = res["totals"]
    out = ["=" * 78, "TEST SELECTION - safety against reduction", "=" * 78]
    out.append(
        f"{tot['repos']} repositories, {tot['tests']} tests, {tot['mutants']} mutants, "
        f"{res['seconds']}s"
    )
    out.append("")
    out.append(
        f"  {tot['survived']:>5}  mutants broke nothing - the suite cannot tell the "
        f"versions apart, so there was nothing to catch"
    )
    out.append(f"  {tot['broke_something']:>5}  mutants broke at least one test")
    if tot["broke_something"]:
        out.append(
            f"  {tot['safe']:>5}  of those had EVERY broken test selected "
            f"({tot['safety']:.1%} safe)"
        )
        out.append(f"        mean reduction: {tot['mean_reduction']:.1%} of the suite skipped")
        if tot["full_seconds"]:
            saved = 1 - tot["subset_seconds"] / tot["full_seconds"]
            out.append(
                f"        wall clock: {tot['subset_seconds']:.0f}s of selected runs against "
                f"{tot['full_seconds']:.0f}s of full runs ({saved:.0%} saved)"
            )
    out.append("")
    out.append(f"{'repo':<22}{'tests':>7}{'mutants':>9}{'broke':>7}{'safe':>7}{'reduction':>11}")
    out.append("-" * 78)
    for r in res["repos"]:
        if r["error"]:
            out.append(f"{r['repo']:<22}  {r['error'][:50]}")
            continue
        safety = f"{r['safety']:.0%}" if r["safety"] is not None else "-"
        red = f"{r['mean_reduction']:.0%}" if r["mean_reduction"] is not None else "-"
        out.append(
            f"{r['repo']:<22}{r['tests']:>7}{r['mutants']:>9}"
            f"{r['broke_something']:>7}{safety:>7}{red:>11}"
        )

    if res["unsafe"]:
        out.append("")
        out.append("-" * 78)
        out.append(f"CHANGES WHERE A BROKEN TEST WAS NOT SELECTED ({len(res['unsafe'])})")
        out.append("Each of these is a regression the selection would have let through.")
        out.append("-" * 78)
        for u in res["unsafe"][:10]:
            out.append(f"  {u['repo']}  {u['mutant']}")
            out.append(
                f"      selected {u['selected']}/{u['total']}, missed: {', '.join(u['missed'][:4])}"
            )
    if res["notes"]:
        out.append("")
        for n in res["notes"][:5]:
            out.append(f"  note: {n['repo']} {n['mutant'][:50]} - {n['note']}")
    return "\n".join(out)


def write_json(res: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, indent=2), encoding="utf-8")
