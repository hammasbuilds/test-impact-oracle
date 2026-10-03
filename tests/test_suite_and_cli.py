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


# --- round-1 user-task regressions -------------------------------------------------------


def _commit(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")


def _select_json(project: Path, python: str, capsys, *extra: str) -> dict:
    import json

    capsys.readouterr()
    rc = main(["select", str(project), "--python", python, "--json", *extra])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_a_project_in_a_git_subdirectory_keeps_its_reduction(tmp_path, python, make_project):
    """git diff paths are toplevel-relative; the map's are project-relative. Without
    `--relative` every change looked unknown and the selector ran everything."""
    pkg = make_project(tmp_path / "mono" / "pkg")
    _commit(tmp_path / "mono")
    assert main(["map", str(pkg), "--python", python, "--quiet"]) == 0
    util = pkg / "src" / "demo" / "util.py"
    util.write_text(util.read_text(encoding="utf-8").replace('"negative"', '"NEG"'), "utf-8")
    change = changed_from_git(pkg, "HEAD")
    assert set(change.lines) == {"src/demo/util.py"}


def test_a_changed_data_file_selects_everything(project, python, capsys):
    _commit(project)
    assert main(["map", str(project), "--python", python, "--quiet"]) == 0
    (project / "tests" / "fixture.json").write_text('{"x": 2}', encoding="utf-8")
    info = _select_json(project, python, capsys)
    assert info["non_python_files"] == ["tests/fixture.json"]
    assert info["selected"] == info["total"] == 3


def test_docs_and_the_map_itself_are_not_changes(project, python, capsys):
    _commit(project)
    assert main(["map", str(project), "--python", python, "--quiet"]) == 0
    assert (project / ".tio-map.json").exists(), "default map lives in the repo"
    (project / "README.md").write_text("# new\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["select", str(project), "--python", python]) == 0
    assert "no code or data changes" in capsys.readouterr().out


def test_a_deleted_module_is_not_silently_ignored(project):
    _commit(project)
    (project / "src" / "demo" / "util.py").unlink()
    assert "src/demo/util.py" in changed_from_git(project, "HEAD").lines


def test_select_json_lists_tests_and_reasons(project, python, capsys):
    _commit(project)
    assert main(["map", str(project), "--python", python, "--quiet"]) == 0
    util = project / "src" / "demo" / "util.py"
    util.write_text(util.read_text(encoding="utf-8").replace('"negative"', '"NEG"'), "utf-8")
    info = _select_json(project, python, capsys)
    assert info["tests"] == ["tests/test_util.py::test_label"]
    assert "util.py" in info["reasons"]["tests/test_util.py::test_label"]


def test_a_stale_map_is_reported_and_the_file_counted_as_changed(project, python, capsys):
    assert main(["map", str(project), "--python", python, "--quiet"]) == 0
    util = project / "src" / "demo" / "util.py"
    util.write_text(util.read_text(encoding="utf-8").replace('"negative"', '"NEG"'), "utf-8")
    _commit(project)  # the edit is now in HEAD, so the diff against HEAD is empty
    capsys.readouterr()
    assert main(["select", str(project), "--python", python, "--why"]) == 0
    captured = capsys.readouterr()
    assert "stale" in captured.err
    assert "tests/test_util.py::test_label" in captured.out


def test_an_interpreter_without_pytest_is_an_error_not_zero_tests(project, python, capsys):
    _commit(project)
    assert main(["map", str(project), "--python", python, "--quiet"]) == 0
    util = project / "src" / "demo" / "util.py"
    util.write_text(util.read_text(encoding="utf-8") + "\n", "utf-8")
    capsys.readouterr()
    rc = main(["select", str(project), "--python", str(project / "no-such-python")])
    assert rc == 2
    assert "could not collect tests" in capsys.readouterr().err
