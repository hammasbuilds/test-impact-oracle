"""Tracing a real suite, and the noise that has to stay out of the map."""

from __future__ import annotations

from pathlib import Path

from test_impact_oracle import mapping


def test_every_test_appears(project, python):
    m = mapping.build(project, "tests", python=python)
    assert m.traced_ok, m.error
    assert set(m.tests) == {
        "tests/test_core.py::test_add",
        "tests/test_core.py::test_is_big",
        "tests/test_util.py::test_label",
    }


def test_a_test_records_the_source_it_ran(project, python):
    m = mapping.build(project, "tests", python=python)
    files = {line.rsplit(":", 1)[0] for line in m.tests["tests/test_core.py::test_add"]}
    assert "src/demo/core.py" in files


def test_a_test_does_not_record_source_it_never_ran(project, python):
    """The whole basis of the tool. If this is wrong, nothing above it means anything."""
    m = mapping.build(project, "tests", python=python)
    files = {line.rsplit(":", 1)[0] for line in m.tests["tests/test_util.py::test_label"]}
    assert "src/demo/lonely.py" not in files


def test_uncovered_files_are_absent_from_the_map(project, python):
    m = mapping.build(project, "tests", python=python)
    assert "src/demo/lonely.py" not in m.files


def test_a_virtualenv_inside_the_repo_is_not_traced(project, python):
    """`.venv` sits under the root, so "is it inside the repo?" is not enough.

    Tracing blast-radius without this filter gave 51 files and 1021 lines per test, and all
    but a handful were pytest tracing itself.
    """
    fake = project / ".venv" / "Lib" / "site-packages" / "thing.py"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("def helper():\n    return 1\n", encoding="utf-8")
    (project / "tests" / "test_venv.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parents[1] / '.venv/Lib/site-packages'))\n"
        "import thing\n"
        "\n"
        "def test_uses_it():\n"
        "    assert thing.helper() == 1\n",
        encoding="utf-8",
    )
    m = mapping.build(project, "tests", python=python)
    assert not any(".venv" in f for f in m.files)


def test_only_python_files_are_recorded(project, python):
    m = mapping.build(project, "tests", python=python)
    assert all(f.endswith(".py") for f in m.files)


def test_by_line_inverts_the_map(project, python):
    m = mapping.build(project, "tests", python=python)
    line = next(iter(m.tests["tests/test_util.py::test_label"]))
    assert "tests/test_util.py::test_label" in m.by_line()[line]


def test_by_file_collapses_lines(project, python):
    m = mapping.build(project, "tests", python=python)
    assert "tests/test_core.py::test_add" in m.by_file()["src/demo/core.py"]


def test_a_missing_directory_is_reported_not_raised(tmp_path, python):
    m = mapping.build(tmp_path / "nope", "tests", python=python)
    assert not m.traced_ok
    assert m.error


def test_a_suite_with_no_tests_is_reported_not_silently_empty(tmp_path, python):
    """An empty map and a suite with no coverage look identical from outside."""
    (tmp_path / "tests").mkdir(parents=True)
    m = mapping.build(tmp_path, "tests", python=python)
    assert not m.traced_ok
    assert "no map" in m.error or m.error


def test_the_map_round_trips(project, python, tmp_path):
    m = mapping.build(project, "tests", python=python)
    dest = tmp_path / "out" / "map.json"
    m.save(dest)
    back = mapping.Mapping.load(dest)
    assert back.tests == m.tests


def test_summary_counts_what_it_says(project, python):
    m = mapping.build(project, "tests", python=python)
    s = m.summary()
    assert s["tests"] == 3
    assert s["files"] >= 3
    assert s["mean_lines_per_test"] > 0


def test_fixture_work_is_attributed_to_the_test_that_uses_it(tmp_path, python, make_project):
    """Setup runs before the call phase; tracing only the call would miss all of it.

    A change inside a fixture has to select every test that uses it, and missing those
    would be silent - which is the failure mode that matters for a selection tool.
    """
    root = make_project(tmp_path / "p")
    (root / "src" / "demo" / "fixtures.py").write_text(
        "def expensive_setup():\n    return [1, 2, 3]\n", encoding="utf-8"
    )
    (root / "tests" / "test_fix.py").write_text(
        "import pytest\n"
        "from demo.fixtures import expensive_setup\n"
        "\n"
        "@pytest.fixture\n"
        "def data():\n"
        "    return expensive_setup()\n"
        "\n"
        "def test_uses_fixture(data):\n"
        "    assert len(data) == 3\n",
        encoding="utf-8",
    )
    m = mapping.build(root, "tests", python=python)
    touched = {line.rsplit(":", 1)[0] for line in m.tests["tests/test_fix.py::test_uses_fixture"]}
    assert "src/demo/fixtures.py" in touched


def test_the_map_is_written_to_an_absolute_path(project, python, tmp_path, monkeypatch):
    """A relative output path plus cwd=repo writes the map inside the repo and finds
    nothing - the mistake that made an earlier tool of mine report a suite as uncovered."""
    monkeypatch.chdir(tmp_path)
    m = mapping.build(Path(project), "tests", python=python)
    assert m.traced_ok, m.error
    assert m.tests
