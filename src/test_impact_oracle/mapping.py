"""Build and hold the test-to-line map.

Building it means running the whole suite once under the tracer, which is slow - `settrace`
fires on every executed line. That cost is paid once and amortised over every later
selection, which is the trade the tool is making: one slow run to make many runs fast.

The map is stored keyed by test, not by line. A diff touches a handful of lines and the
question is always "which tests reach these", so the inverted index is built on load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from test_impact_oracle import suite as suite_mod
from test_impact_oracle import trace as trace_mod


@dataclass
class Mapping:
    root: Path
    tests: dict[str, set[str]] = field(default_factory=dict)
    """test nodeid -> {"path/file.py:12", ...}"""

    seconds: float = 0.0
    traced_ok: bool = True
    error: str = ""

    _by_line: dict[str, set[str]] | None = field(default=None, repr=False)
    _by_file: dict[str, set[str]] | None = field(default=None, repr=False)

    def by_line(self) -> dict[str, set[str]]:
        if self._by_line is None:
            index: dict[str, set[str]] = defaultdict(set)
            for test, lines in self.tests.items():
                for line in lines:
                    index[line].add(test)
            self._by_line = index
        return self._by_line

    def by_file(self) -> dict[str, set[str]]:
        """Every test that executed *any* line of a file.

        The fallback when a change cannot be pinned to lines - a new file, a deleted one, a
        rename, or a module-level constant whose execution was attributed to whichever test
        imported it first.
        """
        if self._by_file is None:
            index: dict[str, set[str]] = defaultdict(set)
            for test, lines in self.tests.items():
                for line in lines:
                    index[line.rsplit(":", 1)[0]].add(test)
            self._by_file = index
        return self._by_file

    @property
    def files(self) -> set[str]:
        return set(self.by_file())

    def summary(self) -> dict:
        lines = {line for s in self.tests.values() for line in s}
        return {
            "tests": len(self.tests),
            "files": len(self.files),
            "lines": len(lines),
            "mean_lines_per_test": round(
                sum(len(s) for s in self.tests.values()) / len(self.tests), 1
            )
            if self.tests
            else 0.0,
            "seconds": round(self.seconds, 1),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "root": str(self.root).replace(os.sep, "/"),
                    "seconds": self.seconds,
                    "tests": {k: sorted(v) for k, v in self.tests.items()},
                },
                indent=0,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Mapping:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            root=Path(data["root"]),
            tests={k: set(v) for k, v in data["tests"].items()},
            seconds=data.get("seconds", 0.0),
        )


def _plugin_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="tio-"))
    (d / f"{trace_mod.PLUGIN_NAME}.py").write_text(trace_mod.PLUGIN_SOURCE, encoding="utf-8")
    return d


def build(
    repo: Path,
    target: str = "tests",
    python: str = "",
    timeout: float = 3600.0,
) -> Mapping:
    """Run the suite once under the tracer and read back the map."""
    import time

    repo = repo.resolve()
    python = python or sys.executable
    started = time.time()

    plugin_dir = _plugin_dir()
    out_fd, out_path = tempfile.mkstemp(suffix=".json", prefix="tio-map-")
    os.close(out_fd)
    out = Path(out_path)

    # Start from the same environment the suite runs under, so the repo's own `src` comes
    # ahead of any editable install pointing somewhere else. Without it, tracing a *copy*
    # of a src-layout project silently traces the original: the imported files then sit
    # outside the traced root, the tracer discards them, and the map comes back holding
    # nothing but the test files - while still reporting success.
    env = suite_mod.env_for(repo)
    env[trace_mod.ENV_OUT] = str(out)
    env[trace_mod.ENV_ROOT] = str(repo)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(plugin_dir) + (os.pathsep + existing if existing else "")

    cmd = [
        python,
        "-m",
        "pytest",
        target,
        "-p",
        trace_mod.PLUGIN_NAME,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--tb=no",
    ]
    m = Mapping(root=repo)
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        m.traced_ok = False
        m.error = f"{type(e).__name__}: {e}"
        return m

    if not out.exists() or not out.stat().st_size:
        # An empty map and a suite with no coverage look identical from the outside, and
        # only one of them is a result. Say which.
        m.traced_ok = False
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        m.error = "tracer wrote no map: " + (tail[-1][:200] if tail else "no output")
        return m

    data = json.loads(out.read_text(encoding="utf-8"))
    m.tests = {k: set(v) for k, v in data["tests"].items()}
    if not m.tests:
        # The plugin wrote a map and recorded nothing. Usually the suite could not import
        # the project. "No tests" is not a map, and calling it one would hand every later
        # step a clean, empty, confident answer.
        m.traced_ok = False
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        m.error = "tracer recorded no tests: " + (tail[-1][:200] if tail else "no output")
        return m
    m.seconds = time.time() - started
    out.unlink(missing_ok=True)
    return m
