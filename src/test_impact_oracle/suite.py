"""Run a suite, or part of one, and report which tests failed.

Separated from everything else because the benchmark needs it three ways: the whole suite to
establish what a change really broke, the selected subset to prove the selection would have
caught it, and a clean baseline to be sure the suite passed before anything was touched.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# `-rf` prints "FAILED path::test[id] - AssertionError: ...". Matching `\S+` stops at the
# first space - and a parametrised node id contains them: `test_counting[(x, y)-2]` is
# captured as `test_counting[(x,`. A truncated id matches nothing in the selection, so the
# benchmark reported a *missed* test and blamed the selector for a regex. Take everything
# up to pytest's " - " separator instead.
_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s.*)?$", re.MULTILINE)


@dataclass
class Result:
    ran: bool = False
    failed: set[str] = field(default_factory=set)
    seconds: float = 0.0
    collected: int = 0
    error: str = ""

    @property
    def green(self) -> bool:
        return self.ran and not self.failed


def env_for(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The copy's own source must win over an editable install pointing at the original,
    # or a mutation applied to the copy would have no effect and every mutant would
    # "survive" - a clean, meaningless 100%.
    src = repo / "src"
    head = str(src if src.is_dir() else repo)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = head + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def collect(
    repo: Path, target: str = "tests", python: str = "", timeout: float = 300.0
) -> list[str]:
    cmd = [
        python or sys.executable,
        "-m",
        "pytest",
        target,
        "--collect-only",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env_for(repo),
            check=False,
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "-", "ERROR", "FAILED")):
            out.append(line)
    return out


def run(
    repo: Path,
    target: str = "tests",
    tests: list[str] | None = None,
    python: str = "",
    timeout: float = 900.0,
) -> Result:
    """Run the suite, or exactly `tests` if given."""
    cmd = [
        python or sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--tb=no",
        "-rf",
    ]
    if tests is not None:
        if not tests:
            # An empty selection is a real answer - "nothing could be affected" - and it
            # must not be turned into "run everything" by passing no arguments to pytest.
            return Result(ran=True, failed=set(), seconds=0.0, collected=0)
        cmd += tests
    else:
        cmd.append(target)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env_for(repo),
            check=False,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Result(ran=False, error=f"timed out after {timeout:g}s")
    except OSError as e:
        return Result(ran=False, error=f"could not start pytest: {e}")

    elapsed = time.time() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 5:
        return Result(ran=False, seconds=elapsed, error="nothing collected")
    if proc.returncode not in (0, 1):
        # 2 is an internal error, 3 an interrupt. A run that fell over is not evidence that
        # every test in it failed, and counting it that way would invent a perfect recall.
        return Result(ran=False, seconds=elapsed, error=f"pytest exited {proc.returncode}")

    return Result(ran=True, failed=set(_SUMMARY.findall(out)), seconds=elapsed)
