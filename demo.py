"""Show what test-impact-oracle does, in one command, with nothing to set up.

    python demo.py

Builds a small throwaway project in a temp directory (needs `git` on PATH), traces its
suite once, edits one line, and asks which tests that edit could affect. Then edits a data
file the tests read, which no tracer can see, and shows the selector running everything.
Takes about ten seconds. Nothing outside the temp directory is touched.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from test_impact_oracle.cli import main as tio  # noqa: E402

FILES = {
    "src/shop/__init__.py": "",
    "src/shop/price.py": (
        "def total(items):\n"
        "    return sum(p * q for p, q in items)\n"
        "\n"
        "\n"
        "def discount(amount, pct):\n"
        "    return round(amount * (1 - pct / 100), 2)\n"
    ),
    "src/shop/text.py": ("def label(n):\n    return 'one item' if n == 1 else f'{n} items'\n"),
    "tests/test_price.py": (
        "from shop.price import discount, total\n"
        "\n"
        "def test_total():\n    assert total([(2.0, 3), (1.5, 2)]) == 9.0\n"
        "\n"
        "def test_discount():\n    assert discount(100, 15) == 85.0\n"
    ),
    "tests/test_text.py": (
        "import json, pathlib\n"
        "from shop.text import label\n"
        "\n"
        "def test_label():\n    assert label(1) == 'one item' and label(3) == '3 items'\n"
        "\n"
        "def test_rates():\n"
        "    data = json.loads((pathlib.Path(__file__).parent / 'rates.json').read_text())\n"
        "    assert data['vat'] == 20\n"
    ),
    "tests/rates.json": '{"vat": 20}\n',
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def edit(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tio-demo-") as tmp:
        repo = Path(tmp) / "shop"
        for rel, body in FILES.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body, encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=demo", "-c", "user.email=demo@example.invalid",
            "commit", "-qm", "start")  # fmt: skip

        print("$ tio map shop            # trace the suite once", flush=True)
        if tio(["map", str(repo), "--quiet"]) != 0:
            return 1

        print("\n# edit one line of discount() in src/shop/price.py", flush=True)
        edit(repo / "src/shop/price.py", "1 - pct / 100", "1 - pct / 100.0")
        print("$ tio select shop --why", flush=True)
        tio(["select", str(repo), "--why"])

        print("\n# now change only tests/rates.json - read by open(), invisible to a tracer")
        git(repo, "checkout", "-q", "--", "src/shop/price.py")
        edit(repo / "tests/rates.json", "20", "21")
        print("$ tio select shop --run", flush=True)
        tio(["select", str(repo), "--run"])

    print("\nPoint it at your own code with:")
    print("    tio map <repo>")
    print("    tio select <repo> --since origin/main --why   (add --run, or --json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
