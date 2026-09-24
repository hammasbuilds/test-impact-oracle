"""Show what test-impact-oracle does, in one command, with nothing to set up.

    python demo.py

Traces this repository's own suite once to build the file-to-test map that
makes selection possible. This runs the whole suite, so it is the slow step -
about 20 seconds here - and it only has to happen once.

Pointed at this repository itself, so the output below is a real run against
real code rather than a fixture built to flatter the tool.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    print('test-impact-oracle: which tests actually exercise which files?', flush=True)
    print(flush=True)
    result = subprocess.run(
        [sys.executable, "-m", 'test_impact_oracle.cli', "map", "."],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    print(flush=True)
    print("Point it at your own code with:", flush=True)
    for line in (
        'test-impact-oracle map <repo>',
        'test-impact-oracle select <repo> --changed <file>',
    ):
        print("    " + line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
