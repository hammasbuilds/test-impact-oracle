"""Selection, and every case where "it never ran that line" stops being enough.

Each of these is a way to skip a test that would have failed, which is the only failure mode
that matters here. Running too much wastes seconds; running too little ships a regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_impact_oracle.mapping import Mapping
from test_impact_oracle.select import Change, looks_like_test, select


def m(**tests: list[str]) -> Mapping:
    """Build a map directly. Keys use `__` for `/` and `..` for `::`."""
    return Mapping(
        root=Path("/repo"),
        tests={k.replace("__", "/").replace("..", "::"): set(v) for k, v in tests.items()},
    )


MAP = m(
    **{
        "tests__test_a.py..test_one": ["src/core.py:10", "src/core.py:11", "tests/test_a.py:4"],
        "tests__test_a.py..test_two": ["src/core.py:20", "tests/test_a.py:9"],
        "tests__test_b.py..test_three": ["src/util.py:5", "tests/test_b.py:4"],
    }
)
ALL = set(MAP.tests)


def test_a_changed_line_selects_only_what_ran_it():
    c = Change()
    c.add("src/core.py", 10)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_a.py::test_one"}
    assert s.reduction == pytest.approx(2 / 3)


def test_a_different_line_in_the_same_file_selects_a_different_test():
    c = Change()
    c.add("src/core.py", 20)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_a.py::test_two"}


def test_an_untouched_file_selects_nothing():
    c = Change()
    c.add("src/util.py", 5)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_b.py::test_three"}


def test_a_file_the_map_has_never_seen_selects_everything():
    """New, renamed, or the map is stale. Guessing which is how a regression gets through."""
    c = Change()
    c.add("src/brand_new.py", 3)
    s = select(MAP, c, ALL)
    assert s.selected == ALL
    assert s.unknown_files == ["src/brand_new.py"]
    assert s.reduction == 0.0


def test_a_test_the_map_has_never_seen_is_always_selected():
    c = Change()
    c.add("src/core.py", 10)
    everything = ALL | {"tests/test_new.py::test_fresh"}
    s = select(MAP, c, everything)
    assert "tests/test_new.py::test_fresh" in s.selected
    assert "not in the map" in s.reasons["tests/test_new.py::test_fresh"]


def test_changing_a_test_file_selects_its_own_tests():
    c = Change()
    c.add("tests/test_a.py", 4)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}


def test_changing_a_test_file_selects_tests_that_never_traced():
    """A test that errors during collection has no trace at all, so the map cannot list it."""
    c = Change()
    c.add("tests/test_a.py", 99)
    everything = ALL | {"tests/test_a.py::test_broken"}
    s = select(MAP, c, everything)
    assert "tests/test_a.py::test_broken" in s.selected


def test_a_covered_file_with_an_uncovered_line_selects_everything_touching_the_file():
    """Module-level code runs once, during the first import, and is credited to whichever
    test triggered it - so line-level attribution cannot be trusted there."""
    c = Change()
    c.add("src/core.py", 1)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}
    assert "though not the changed lines" in s.reasons["tests/test_a.py::test_one"]


def test_several_changed_files_take_the_union():
    c = Change()
    c.add("src/core.py", 10)
    c.add("src/util.py", 5)
    s = select(MAP, c, ALL)
    assert s.selected == {"tests/test_a.py::test_one", "tests/test_b.py::test_three"}


def test_an_empty_change_selects_nothing():
    s = select(MAP, Change(), ALL)
    assert s.selected == set()
    assert s.reduction == 1.0


def test_every_selection_carries_a_reason():
    c = Change()
    c.add("src/core.py", 10)
    s = select(MAP, c, ALL)
    assert all(s.reasons.get(t) for t in s.selected)


def test_an_empty_map_selects_everything():
    """Nothing is known, so nothing can be ruled out."""
    c = Change()
    c.add("src/core.py", 10)
    s = select(Mapping(root=Path("/repo")), c, ALL)
    assert s.selected == ALL


@pytest.mark.parametrize(
    "path,is_test",
    [
        ("tests/test_a.py", True),
        ("src/pkg/tests/helpers.py", True),
        ("test_top.py", True),
        ("conftest.py", True),
        ("src/core.py", False),
        ("src/latest.py", False),
        ("src/contest.py", False),
    ],
)
def test_recognising_test_files(path, is_test):
    assert looks_like_test(path) is is_test
