"""A tiny real project with a real suite, built on disk.

The tracer only exists to observe a running pytest, so the tests run one. A mock of the
plugin would confirm the mock; the interesting failures - a virtualenv inside the repo, a
fixture whose lines belong to setup rather than call, a test that errors during collection
and so traces nothing - only appear when pytest is actually driving it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG_INIT = "from demo.core import add, is_big\nfrom demo.util import label\n"

CORE = '''\
"""Two functions with distinct call sites, so selection has something to separate."""

THRESHOLD = 10


def add(a, b):
    return a + b


def is_big(n):
    return n > THRESHOLD
'''

UTIL = """\
def label(n):
    if n < 0:
        return "negative"
    return "positive"
"""

LONELY = '''\
def never_called():
    """Covered by no test. Changing it must select nothing."""
    return 41 + 1
'''

TEST_CORE = """\
from demo.core import add, is_big


def test_add():
    assert add(2, 3) == 5


def test_is_big():
    assert is_big(11)
    assert not is_big(9)
"""

TEST_UTIL = """\
from demo.util import label


def test_label():
    assert label(1) == "positive"
    assert label(-1) == "negative"
"""

FILES = {
    "src/demo/__init__.py": PKG_INIT,
    "src/demo/core.py": CORE,
    "src/demo/util.py": UTIL,
    "src/demo/lonely.py": LONELY,
    "tests/test_core.py": TEST_CORE,
    "tests/test_util.py": TEST_UTIL,
}


def build_project(root: Path, extra: dict[str, str] | None = None) -> Path:
    for rel, body in {**FILES, **(extra or {})}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="")
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return build_project(tmp_path / "proj")


@pytest.fixture
def python() -> str:
    """This interpreter. It has pytest, which is all the fixture project needs."""
    return sys.executable
