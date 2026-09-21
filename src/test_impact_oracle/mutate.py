"""Make a real change to a source file, so that "affected" has a ground truth.

A selector can only be checked against changes whose consequences are known, and nobody
knows the consequences of a real commit without running the suite. So the benchmark makes
its own: change one line, run the whole suite to see which tests actually fail, and ask
whether the selector picked them.

The operators are chosen to be **plausible mistakes**, not maximally destructive ones. A
change that breaks an import breaks every test and proves nothing - any selector that
returns anything at all would score perfectly on it. What has to be caught is the narrow
change: one comparison flipped, one boundary moved, one default altered.

Every mutation is a single point in a single file, because that is what makes the answer
attributable. A two-line change would leave it unclear which line the selection was right
about.
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from pathlib import Path

COMPARE_SWAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BINOP_SWAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}


@dataclass
class Mutant:
    path: str
    """Repo-relative."""

    line: int
    kind: str
    before: str
    after: str
    original_source: str

    def describe(self) -> str:
        return f"{self.path}:{self.line} {self.kind}: {self.before} -> {self.after}"


class _Finder(ast.NodeVisitor):
    def __init__(self):
        self.sites: list[tuple[ast.AST, str]] = []

    def visit_Compare(self, node: ast.Compare):
        if len(node.ops) == 1 and type(node.ops[0]) in COMPARE_SWAP:
            self.sites.append((node, "compare"))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp):
        if type(node.op) in BINOP_SWAP:
            self.sites.append((node, "binop"))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp):
        self.sites.append((node, "boolop"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, bool):
            self.sites.append((node, "bool"))
        elif isinstance(node.value, int) and not isinstance(node.value, bool):
            self.sites.append((node, "int"))
        self.generic_visit(node)


def _apply(tree: ast.AST, node: ast.AST, kind: str) -> tuple[str, str]:
    """Mutate `node` in place. Returns (before, after) as short text."""
    if kind == "compare":
        old = type(node.ops[0])  # type: ignore[attr-defined]
        node.ops[0] = COMPARE_SWAP[old]()  # type: ignore[attr-defined]
        return old.__name__, COMPARE_SWAP[old].__name__
    if kind == "binop":
        old = type(node.op)  # type: ignore[attr-defined]
        node.op = BINOP_SWAP[old]()  # type: ignore[attr-defined]
        return old.__name__, BINOP_SWAP[old].__name__
    if kind == "boolop":
        old = type(node.op)  # type: ignore[attr-defined]
        new = ast.Or if isinstance(node.op, ast.And) else ast.And  # type: ignore[attr-defined]
        node.op = new()  # type: ignore[attr-defined]
        return old.__name__, new.__name__
    if kind == "bool":
        old = node.value  # type: ignore[attr-defined]
        node.value = not old  # type: ignore[attr-defined]
        return repr(old), repr(not old)
    old = node.value  # type: ignore[attr-defined]
    node.value = old + 1  # type: ignore[attr-defined]
    return repr(old), repr(old + 1)


def candidates(root: Path, path: str) -> int:
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return 0
    f = _Finder()
    f.visit(tree)
    return len(f.sites)


def make(root: Path, path: str, rng: random.Random) -> Mutant | None:
    """One single-point mutation of `path`, or None if there is nowhere to put one."""
    src = (root / path).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    finder = _Finder()
    finder.visit(tree)
    if not finder.sites:
        return None

    parents = _parents(tree)
    rng.shuffle(finder.sites)
    for node, kind in finder.sites:
        stmt = _enclosing_statement(node, parents)
        if stmt is None or not hasattr(stmt, "end_lineno"):
            continue
        line = getattr(node, "lineno", stmt.lineno)
        before, after = _apply(tree, node, kind)
        spliced = _splice(src, stmt)
        if spliced is None or spliced == src:
            continue
        (root / path).write_text(spliced, encoding="utf-8", newline="")
        return Mutant(path, line, kind, before, after, src)
    return None


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    out: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            out[id(child)] = parent
    return out


def _enclosing_statement(node: ast.AST, parents: dict[int, ast.AST]) -> ast.stmt | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.stmt):
            return cur
        cur = parents.get(id(cur))
    return None


def _splice(src: str, stmt: ast.stmt) -> str | None:
    """Rewrite only the statement's own lines, leaving the rest of the file byte-identical.

    Unparsing the whole module would work and would be wrong: `ast.unparse` reformats
    everything and drops every comment, so a one-line change would arrive as a diff
    touching every line of the file. Several repositories in the corpus read their own
    source, and a reformat would fail their tests for reasons the mutation had nothing to
    do with - inventing failures the selector would then be scored against.
    """
    lines = src.splitlines(keepends=True)
    start, end = stmt.lineno - 1, stmt.end_lineno
    if start < 0 or end > len(lines):
        return None

    indent = len(lines[start]) - len(lines[start].lstrip())
    pad = lines[start][:indent]
    try:
        body = ast.unparse(stmt)
    except (ValueError, RecursionError, AttributeError):
        return None

    rendered = "".join(pad + ln + "\n" if ln.strip() else "\n" for ln in body.splitlines())
    return "".join(lines[:start]) + rendered + "".join(lines[end:])


def restore(root: Path, m: Mutant) -> None:
    (root / m.path).write_text(m.original_source, encoding="utf-8", newline="")
