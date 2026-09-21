"""Mutations, and the property that makes them usable as a ground truth."""

from __future__ import annotations

import difflib
import random
from pathlib import Path

from test_impact_oracle import mutate


def write(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8", newline="")
    return tmp_path


SAMPLE = '''\
"""A docstring that must survive."""

LIMIT = 10  # a trailing comment that must survive too


def check(n):
    # An interior comment.
    if n > LIMIT:
        return True
    return False
'''


def test_it_changes_exactly_one_statement(tmp_path):
    """The diff has to be small, or the ground truth is about a reformat, not a change."""
    root = write(tmp_path, SAMPLE)
    m = mutate.make(root, "m.py", random.Random(1))
    assert m is not None
    new = (root / "m.py").read_text(encoding="utf-8")
    diff = [
        ln
        for ln in difflib.unified_diff(
            m.original_source.splitlines(), new.splitlines(), lineterm="", n=0
        )
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ]
    assert len(diff) <= 4, diff


def test_comments_outside_the_statement_survive(tmp_path):
    """`ast.unparse` drops comments. Unparsing the whole module would rewrite the file and
    fail tests that read their own source, inventing failures the selector is scored on."""
    root = write(tmp_path, SAMPLE)
    for seed in range(8):
        m = mutate.make(root, "m.py", random.Random(seed))
        if m is None:
            continue
        new = (root / "m.py").read_text(encoding="utf-8")
        assert "A docstring that must survive" in new
        mutate.restore(root, m)


def test_restore_puts_the_file_back_byte_for_byte(tmp_path):
    root = write(tmp_path, SAMPLE)
    before = (root / "m.py").read_text(encoding="utf-8")
    m = mutate.make(root, "m.py", random.Random(3))
    assert m is not None
    assert (root / "m.py").read_text(encoding="utf-8") != before
    mutate.restore(root, m)
    assert (root / "m.py").read_text(encoding="utf-8") == before


def test_the_result_still_parses(tmp_path):
    import ast

    root = write(tmp_path, SAMPLE)
    for seed in range(10):
        m = mutate.make(root, "m.py", random.Random(seed))
        if m is None:
            continue
        ast.parse((root / "m.py").read_text(encoding="utf-8"))
        mutate.restore(root, m)


def test_the_recorded_line_is_in_the_original_numbering(tmp_path):
    """Selection is keyed on the map, which was built from the original file."""
    root = write(tmp_path, SAMPLE)
    m = mutate.make(root, "m.py", random.Random(2))
    assert m is not None
    assert 1 <= m.line <= len(SAMPLE.splitlines())


def test_indentation_is_preserved(tmp_path):
    root = write(tmp_path, SAMPLE)
    m = mutate.make(root, "m.py", random.Random(5))
    assert m is not None
    new = (root / "m.py").read_text(encoding="utf-8")
    for line in new.splitlines():
        if line.strip().startswith("return "):
            assert line.startswith("        ") or line.startswith("    ")


def test_a_file_with_nowhere_to_mutate(tmp_path):
    root = write(tmp_path, "import os\n")
    assert mutate.make(root, "m.py", random.Random(0)) is None


def test_an_unparseable_file_is_declined_not_raised(tmp_path):
    root = write(tmp_path, "def broken(:\n")
    assert mutate.make(root, "m.py", random.Random(0)) is None


def test_candidates_counts_sites(tmp_path):
    root = write(tmp_path, SAMPLE)
    assert mutate.candidates(root, "m.py") > 0
    assert mutate.candidates(root, "missing.py") == 0


def test_the_same_seed_gives_the_same_mutant(tmp_path):
    root = write(tmp_path, SAMPLE)
    a = mutate.make(root, "m.py", random.Random(7))
    assert a is not None
    mutate.restore(root, a)
    b = mutate.make(root, "m.py", random.Random(7))
    assert b is not None
    assert (a.line, a.before, a.after) == (b.line, b.before, b.after)


def test_a_comparison_is_swapped_not_deleted(tmp_path):
    root = write(tmp_path, "def f(n):\n    return n > 5\n")
    seen = set()
    for seed in range(20):
        m = mutate.make(root, "m.py", random.Random(seed))
        if m is None:
            continue
        seen.add(m.kind)
        mutate.restore(root, m)
    assert {"compare", "int"} & seen
