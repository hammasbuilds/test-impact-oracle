# Results

Two numbers, and they pull against each other. Quoting either alone is how a test-selection
tool gets adopted and then quietly lets a regression through.

    safety      of the changes that broke something, how many had EVERY broken test selected
    reduction   of the suite, what share was skipped

A selector that returns everything scores 100% safety and 0% reduction. One that returns
nothing scores the reverse.

Reproduce with:

```bash
tio bench <repo> [<repo> ...] --mutants 12 --json bench.json
```

Raw output is in `bench.json` beside this file.

---

## The corpus

Seven repositories, 287 tests between them. All are Python libraries with `src` layouts and
zero runtime dependencies, which is a real limit on what the numbers generalise to — see
§4.

Everything runs on a **copy**, with the copy's `src` ahead of the editable install on
`PYTHONPATH`. The repositories being measured are never modified.

---

## 1. Safety against reduction

```
7 repositories, 287 tests, 84 mutants, 596s

     41  mutants broke nothing - the suite cannot tell the versions apart
     43  mutants broke at least one test
     43  of those had EVERY broken test selected  (100.0% safe)

        mean reduction: 90.1% of the suite skipped
        wall clock: 92s of selected runs against 241s of full runs (62% saved)
```

| repo | tests | mutants | broke something | survived | safe | reduction |
|---|---:|---:|---:|---:|---:|---:|
| repo-surgeon | 47 | 12 | 4 | 8 | 100% | 94% |
| pr-referee | 37 | 12 | 8 | 4 | 100% | 91% |
| trace-to-patch | 30 | 12 | 6 | 6 | 100% | 92% |
| suite-auditor | 24 | 12 | 4 | 8 | 100% | 94% |
| blast-radius | 28 | 12 | 6 | 6 | 100% | 87% |
| flake-detective | 55 | 12 | 8 | 4 | 100% | 87% |
| cartographer | 66 | 12 | 7 | 5 | 100% | 89% |

**The 41 surviving mutants are not a result about the selector.** They are a result about the
suites: a line was changed and nothing noticed. They are excluded from safety because there
was no failure to catch or to miss — including them would let a weak suite inflate the score,
since a change nothing detects is one no selection can get wrong.

**Reduction is measured per change**, and it is high because these mutations are single lines
in library code. A change to a widely-imported module selects far more; a change to a
package `__init__.py` selects everything that touched it.

**The 62% wall-clock saving is lower than the 90% test-count reduction**, and the gap is
pytest's own startup. On a 2-second suite, interpreter start and collection dominate. The
saving approaches the reduction only as the suite gets slow enough for the tests themselves
to be the cost — which is also the only case where anybody wants this tool.

---

## 2. The result before the parser was fixed

The first run of this benchmark reported **97.7% safe**, with one unsafe change:

```
  blast-radius  src/blast_radius/diff.py:47 int: 1 -> 2
      selected 8/28, missed: tests/test_blast_radius.py::test_parameter_counting[(self,
```

That missed test does not exist. `pytest -rf` prints
`FAILED path::test[id] - AssertionError: ...`, and the parser matched `\S+`, which stops at
the first space — so `test_parameter_counting[(self, a, b)-2]` was captured as
`test_parameter_counting[(self,`. A truncated id matches nothing in the selection, so it read
as a miss, and the selector was blamed for a regex.

The selection had been right all along. It is recorded here because "97.7% safe" is exactly
the kind of nearly-perfect number that gets published without being chased down.

---

## 3. Where the guarantee stops, demonstrated

100% safety is the *expected* answer for single-line changes under line-level tracing, which
makes it weak evidence on its own. The limits are therefore built as failing cases and
asserted in [`tests/test_blind_spots.py`](../tests/test_blind_spots.py).

### A file reached both in-process and by subprocess

The dangerous shape, because the unknown-file fallback never fires:

| | |
|---|---|
| `dual.py` is imported by `test_in_process` | so the file **is** in the map |
| `dual.py` is also run as a script by `test_via_subprocess` | but `settrace` never entered that interpreter |
| the change is on a line only the subprocess reaches | so it belongs to no test |
| **`test_via_subprocess` is not selected, and the change breaks it** | |

```python
assert sub_test not in s.selected, (
    "if this ever passes, the tracer learned to follow subprocesses and the "
    "README's stated limit is out of date"
)
```

That assertion passes today. It is a real miss, and it is the honest answer to "is 100%
safety believable".

### A subprocess-only file

Safe, but by luck rather than by understanding: the file never appears in the map at all, so
the unknown-file rule selects everything. The map is still wrong; the fallback covers for it.

### A stale map

Insert a line at the top of a file and every line below shifts. A diff naming line 12 no
longer means what the map's line 12 meant. Nothing here detects it, and nothing here pretends
a stored map stays true.

---

## 4. What these numbers do not say

- **Seven repositories, all mine.** Same author, same style, all libraries, none over 66
  tests. 90% reduction on a 300-test suite is not evidence about a 30,000-test monorepo,
  where the interesting behaviour is a change to a module everything imports.
- **Single-line mutations are the easy case.** A real commit touches several files, and the
  selection is the union — so reduction falls as the diff grows. This measures the floor of
  difficulty, not the average.
- **Twelve mutants per repository is a small sample.** Four repositories had only four to six
  mutants that broke anything, so their per-repo safety rests on very few observations. The
  pooled figure of 43 is the one worth reading.
- **Order effects are untested.** Running a subset changes what ran before what. A suite with
  order-dependent tests can fail differently under selection, and nothing here would notice.
- **`survived` says the suite is weak, not that the selector is good.** Nearly half the
  mutants changed a line and broke nothing at all.

## Bugs these runs caught

| what it reported | what was true |
|---|---|
| 51 files and 1021 lines per test on a 4-file project | `.venv` sits inside the repo, so `site-packages` passed the "inside the root" filter |
| a successfully built, empty map | tracing a src-layout copy without its `src` on the path traces the *installed* original, whose files are then outside the traced root and discarded |
| one unsafe change out of 43 | `\S+` truncated a parametrised node id at its first space |
| a one-line mutation as a whole-file diff | `ast.unparse` on the module reformats everything and drops every comment |
