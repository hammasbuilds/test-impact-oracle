"""Where the guarantee stops holding, demonstrated rather than asserted.

The corpus run came back 43 of 43 safe. That is the expected answer for single-line changes
under line-level tracing, and it is also exactly the kind of clean number that hides a limit
nobody exercised. These build the failing cases on purpose, so the limits are measured facts
with a test each rather than a paragraph of hedging in a README.

Each one ends the same way: the selector skips a test that the change really does break.
"""

from __future__ import annotations

from test_impact_oracle import mapping, select


def test_code_that_only_runs_in_a_subprocess_is_invisible(tmp_path, python):
    """`sys.settrace` fires for frames in *this* interpreter.

    A test that shells out reaches the changed behaviour through a process the tracer never
    entered, so the line is not in the map and the test is not selected. The change is real,
    the breakage is real, and the selection misses it.
    """
    root = tmp_path / "p"
    (root / "src" / "demo").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "demo" / "worker.py").write_text(
        "def answer():\n    return 42\n\n\nif __name__ == '__main__':\n    print(answer())\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_sub.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "\n"
        "WORKER = Path(__file__).parents[1] / 'src' / 'demo' / 'worker.py'\n"
        "\n"
        "def test_worker_prints_42():\n"
        "    out = subprocess.run([sys.executable, str(WORKER)], capture_output=True,\n"
        "                         text=True, check=True).stdout\n"
        "    assert out.strip() == '42'\n",
        encoding="utf-8",
    )

    m = mapping.build(root, "tests", python=python)
    assert m.traced_ok, m.error

    # The worker's own lines were executed - but in a different interpreter.
    assert "src/demo/worker.py" not in m.files

    change = select.Change()
    change.add("src/demo/worker.py", 2)  # the `return 42`
    s = select.select(m, change, set(m.tests))

    # Not selected because the file is unknown... which is the safe branch, so the guarantee
    # survives here by accident rather than by understanding. The map is what is wrong.
    assert s.selected == set(m.tests)
    assert s.unknown_files == ["src/demo/worker.py"]


def test_a_partially_traced_file_is_the_dangerous_case(tmp_path, python):
    """The blind spot that is *not* caught by the unknown-file fallback.

    When a file is imported in-process by one test and also executed in a subprocess by
    another, the file *is* in the map - so the fallback never fires - and the map holds only
    the lines the in-process test ran. A change to a line reached only through the subprocess
    is attributed to nobody, and the subprocess test is skipped.
    """
    root = tmp_path / "p"
    (root / "src" / "demo").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "demo" / "dual.py").write_text(
        "def imported_path():\n"
        "    return 'in-process'\n"
        "\n"
        "\n"
        "def subprocess_only():\n"
        "    return 42\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(subprocess_only())\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_inproc.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
        "from demo.dual import imported_path\n"
        "\n"
        "def test_in_process():\n"
        "    assert imported_path() == 'in-process'\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_sub.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "\n"
        "DUAL = Path(__file__).parents[1] / 'src' / 'demo' / 'dual.py'\n"
        "\n"
        "def test_via_subprocess():\n"
        "    out = subprocess.run([sys.executable, str(DUAL)], capture_output=True,\n"
        "                         text=True, check=True).stdout\n"
        "    assert out.strip() == '42'\n",
        encoding="utf-8",
    )

    m = mapping.build(root, "tests", python=python)
    assert m.traced_ok, m.error
    assert "src/demo/dual.py" in m.files, "the in-process test should put it in the map"

    change = select.Change()
    change.add("src/demo/dual.py", 6)  # `return 42`, reached only in the subprocess

    s = select.select(m, change, set(m.tests))
    sub_test = "tests/test_sub.py::test_via_subprocess"

    # This is the documented failure: the change breaks test_via_subprocess, and the
    # selection does not contain it.
    assert sub_test in m.tests
    assert sub_test not in s.selected, (
        "if this ever passes, the tracer learned to follow subprocesses and the "
        "README's stated limit is out of date"
    )


def test_a_stale_map_selects_by_line_numbers_that_have_moved(tmp_path, python, make_project):
    """The map is a photograph. Insert a line above a function and every line below it
    shifts, so a diff naming line 12 no longer means what the map's line 12 meant.

    Nothing here detects that. The map has to be rebuilt when the tree moves, and the tool
    says so rather than pretending a stored map stays true.
    """
    root = make_project(tmp_path / "p")
    m = mapping.build(root, "tests", python=python)
    assert m.traced_ok, m.error

    core = root / "src" / "demo" / "core.py"
    original = core.read_text(encoding="utf-8")
    core.write_text("# a new line at the top\n" + original, encoding="utf-8", newline="")

    # `add` has moved down one line. A diff reporting its new line finds whatever used to
    # be there, which is a different statement with different tests.
    by_file = m.by_file()["src/demo/core.py"]
    assert by_file, "core.py should be covered"
    stale = select.Change()
    stale.add("src/demo/core.py", 1)
    s = select.select(m, stale, set(m.tests))
    # Line 1 was never executed, so the file-level fallback catches it - by luck, not design.
    assert s.selected == by_file
