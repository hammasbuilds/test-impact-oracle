"""Given a change, choose the tests that could possibly be affected.

The rule is one line: **a test that never executed the line you changed cannot have been
affected by changing it.** Everything else here is the list of cases where that rule does not
apply, and each of them resolves the same way - *select the test*.

That asymmetry is the whole design. Skipping a test that would have failed is a bug reaching
production. Running a test that was never going to fail costs a few seconds. So every
uncertainty is spent on running more:

**A file the map has never seen** selects everything. It is new, it is renamed, or the map
is stale. Not knowing which is exactly the case where guessing is worst.

**A test the map has never seen** is always selected. It is new since the map was built, and
nothing is known about what it touches.

**A changed test file** selects every test in it, whatever the line numbers say. Editing a
test changes the test.

**A changed line in a file, but not one any test executed**, still selects every test that
touched that *file*. Module-level code runs once, on whichever test imported it first, so
line-level attribution is unreliable there - and `__init__.py`, constants and decorators all
live at module level.

The consequence is stated in the benchmark rather than hidden: the fraction of the suite this
skips is the number that has to justify the tool, and it is reported next to the fraction of
real failures it caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from test_impact_oracle.mapping import Mapping


@dataclass
class Change:
    """Lines that changed, by repo-relative path. An empty set means "somewhere in it"."""

    lines: dict[str, set[int]] = field(default_factory=dict)

    @property
    def files(self) -> set[str]:
        return set(self.lines)

    def add(self, path: str, line: int | None = None) -> None:
        s = self.lines.setdefault(path, set())
        if line is not None:
            s.add(line)


@dataclass
class Selection:
    selected: set[str] = field(default_factory=set)
    total: int = 0
    reasons: dict[str, str] = field(default_factory=dict)
    """test -> why it was selected. A selection nobody can explain is not usable."""

    unknown_files: list[str] = field(default_factory=list)
    """Changed files absent from the map, which forced everything to be selected."""

    @property
    def skipped(self) -> int:
        return self.total - len(self.selected)

    @property
    def reduction(self) -> float:
        return self.skipped / self.total if self.total else 0.0

    def summary(self) -> dict:
        return {
            "selected": len(self.selected),
            "total": self.total,
            "skipped": self.skipped,
            "reduction": round(self.reduction, 4),
            "unknown_files": self.unknown_files,
        }


def looks_like_test(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test", "testing") for p in parts[:-1]) or parts[-1].startswith(
        ("test_", "conftest")
    )


def select(m: Mapping, change: Change, all_tests: set[str] | None = None) -> Selection:
    known = set(m.tests)
    every = set(all_tests) if all_tests is not None else known
    s = Selection(total=len(every))

    # Tests the map has never seen are new. Nothing is known about them, so they run.
    for t in every - known:
        s.selected.add(t)
        s.reasons[t] = "not in the map: new since it was built"

    by_line = m.by_line()
    by_file = m.by_file()
    mapped_files = set(by_file)

    for path, lines in change.lines.items():
        if path not in mapped_files:
            # Never executed by any test: new, renamed, or the map is stale. Which of those
            # it is cannot be told from here, and the wrong guess skips a real failure.
            s.unknown_files.append(path)
            for t in every:
                s.selected.add(t)
                s.reasons.setdefault(t, f"{path} is not in the map")
            continue

        if looks_like_test(path):
            for t in by_file[path]:
                s.selected.add(t)
                s.reasons.setdefault(t, f"its own file changed ({path})")
            # Also select by nodeid prefix: a test that errored during collection has no
            # trace at all, so `by_file` would not list it.
            for t in every:
                if t.split("::")[0] == path:
                    s.selected.add(t)
                    s.reasons.setdefault(t, f"its own file changed ({path})")
            continue

        hit = set()
        for line in lines:
            hit |= by_line.get(f"{path}:{line}", set())

        if hit:
            for t in hit:
                s.selected.add(t)
                s.reasons.setdefault(t, f"executed {path}:{min(lines)}")
        else:
            # The file is covered but these particular lines are not. Module-level code is
            # the usual reason - it runs once, during the first import, and is credited to
            # whichever test happened to trigger it.
            for t in by_file[path]:
                s.selected.add(t)
                s.reasons.setdefault(t, f"touched {path}, though not the changed lines")

    return s
