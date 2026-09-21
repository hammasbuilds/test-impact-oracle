"""Running suites, reading diffs, and the end-to-end commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_impact_oracle import suite
from test_impact_oracle.cli import changed_from_git, main


def test_a_green_suite_reports_no_failures(project, python):
    r = suite.run(project, "tests", python=python)
    assert r.green and r.failed == set()


def test_a_failing_test_is_named(project, python):
    (project / "tests" / "test_bad.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    r = suite.run(project, "tests", python=python)
    assert "tests/test_bad.py::test_fails" in r.failed


def test_running_a_subset_runs_only_that_subset(project, python):
    (project / "tests" / "test_bad.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    r = suite.run(project, tests=["tests/test_core.py::test_add"], python=python)
    assert r.green


def test_an_empty_selection_runs_nothing_rather_than_everything(project, python):
    """Passing no arguments to pytest means "run the whole suite" - the opposite of what an
    empty selection says. A selection tool that inverted this would be worse than useless."""
    (project / "tests" / "test_bad.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    r = suite.run(project, tests=[], python=python)
    assert r.ran and r.failed == set() and r.seconds == 0.0


def test_a_directory_with_no_tests_is_not_a_pass(tmp_path, python):
    (tmp_path / "tests").mkdir()
    r = suite.run(tmp_path, "tests", python=python)
    assert not r.ran
    assert "collected" in r.error


def test_collect_lists_every_test(project, python):
    ids = suite.collect(project, "tests", python=python)
    assert "tests/test_core.py::test_add" in ids
    assert len(ids) == 3


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def test_reading_changed_lines_from_git(project):
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.invalid")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "first")

    core = project / "src" / "demo" / "core.py"
    text = core.read_text(encoding="utf-8").replace("return a + b", "return a + b + 0")
    core.write_text(text, encoding="utf-8", newline="")

    change = changed_from_git(project, "HEAD")
    assert "src/demo/core.py" in change.lines
    assert change.lines["src/demo/core.py"]


def test_map_then_select_end_to_end(project, python, tmp_path, capsys):
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.invalid")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "first")

    out = tmp_path / "map.json"
    assert main(["map", str(project), "--python", python, "--out", str(out), "--quiet"]) == 0
    assert out.exists()

    util = project / "src" / "demo" / "util.py"
    util.write_text(
        util.read_text(encoding="utf-8").replace('"negative"', '"NEGATIVE"'),
        encoding="utf-8",
        newline="",
    )
    capsys.readouterr()
    assert (
        main(
            [
                "select",
                str(project),
                "--map",
                str(out),
                "--since",
                "HEAD",
                "--python",
                python,
                "--why",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    # Only the test that runs util.py, not the ones that run core.py.
    assert "tests/test_util.py::test_label" in printed
    assert "tests/test_core.py::test_add" not in printed


def test_select_without_a_map_is_refused(project, tmp_path, capsys):
    assert main(["select", str(project), "--map", str(tmp_path / "nope.json")]) == 2
    assert "run `tio map` first" in capsys.readouterr().err


def test_map_on_a_missing_path_is_refused(tmp_path, capsys):
    assert main(["map", str(tmp_path / "nope")]) == 2
    assert "no such path" in capsys.readouterr().err


def test_map_on_a_repo_that_cannot_be_traced_reports_it(tmp_path, python, capsys):
    (tmp_path / "tests").mkdir()
    assert main(["map", str(tmp_path), "--python", python, "--quiet"]) == 1
    assert "could not build a map" in capsys.readouterr().err


def test_a_parametrised_failure_id_is_not_truncated(project, python):
    r"""Node ids contain spaces, and `\S+` stops at the first one.

    `test_counting[(x, y)-2]` came back as `test_counting[(x,` - an id that matches nothing
    in the selection, so the benchmark reported a missed test and blamed the selector for a
    regex. It was the only unsafe result in the whole corpus.
    """
    (project / "tests" / "test_param.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.parametrize('sig,n', [('(x, y)', 2), ('(self, a, b)', 2)])\n"
        "def test_counting(sig, n):\n"
        "    assert len(sig.split(',')) == n + 5\n",
        encoding="utf-8",
    )
    r = suite.run(project, "tests", python=python)
    assert r.failed
    for nodeid in r.failed:
        assert nodeid.endswith("]"), nodeid
        assert "::" in nodeid


def test_a_failure_reason_is_not_part_of_the_id(project, python):
    (project / "tests" / "test_bad.py").write_text(
        "def test_fails():\n    assert 1 == 2, 'a message with - a dash'\n", encoding="utf-8"
    )
    r = suite.run(project, "tests", python=python)
    assert r.failed == {"tests/test_bad.py::test_fails"}
