"""v2 selector: read each patch, run only the tests it can plausibly break.

Rules (anything not covered by a rule falls back to running ALL tests, because
a miss costs 300 s while a full run costs about 37 s):

  1. Comment-only / docs-only patch            -> run nothing.
  2. tests/fixtures/optimizer/X.sql            -> test_optimizer.py tests named like X.
  3. A tests/**/test_*.py file                 -> the tests in that file.
  4. sqlglot/{dialects,parsers,generators,typing}/X.py -> test_X.py, its child dialects,
                                                  test_dialect.py, and the optimizer/
                                                  expressions/parser test files.
  5. Everything else (core parser, optimizer, ...) -> all tests.

Then the chosen tests are split over the number of workers (1..8) that
minimizes: busiest worker's seconds + 2 s per worker.
"""

import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
MAX_WORKERS = 8
WORKER_MS = 2000

# Dialects whose tests could also break when the key dialect changes (they
# inherit from it). Conservative on purpose; verify with: grep -rn "^class " sqlglot/dialects
CHILDREN = {
    "hive": ["spark", "databricks"],
    "spark": ["databricks"],
    "postgres": ["redshift", "risingwave", "materialize"],
    "mysql": ["doris", "starrocks", "singlestore", "solr", "druid"],
    "presto": ["trino", "athena", "dune"],
    "trino": ["athena", "dune"],
    "tsql": ["fabric"],
}
# A dialect edit can break generic tests too (v1 missed 3 commits this way):
# the optimizer, expression and parser tests all parse/generate across dialects.
GENERIC_FILES = ["tests/test_optimizer.py", "tests/test_expressions.py", "tests/test_parser.py"]
# 28.8 s and never failed on a dialect edit in the sample. A deliberate gamble:
# read the test; if it can depend on dialect code, delete this line.
SLOW_UNLIKELY = ("::TestParser::test_max_nodes",)
# Files whose change can affect every dialect -> no shortcut.
BROAD_STEMS = {"dialect", "base", "__init__"}


def parse_patch(text: str) -> dict[str, list[str]]:
    """path -> list of changed lines (without the +/- marker)."""
    files: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if m:
            current = m.group(2)
            files[current] = []
        elif current and line[:1] in "+-" and not line.startswith(("+++", "---")):
            files[current].append(line[1:])
    return files


def is_trivial(path: str, lines: list[str]) -> bool:
    if path.endswith((".md", ".rst", ".txt")) or path.startswith("docs/"):
        return True
    if path.endswith(".py"):
        return all(not l.strip() or l.strip().startswith("#") for l in lines)
    return False


def pick(path: str, tests: list[dict], by_file: dict[str, list[int]]):
    """Test numbers to run for one changed file, or None meaning 'run all'."""
    m = re.fullmatch(r"tests/fixtures/optimizer/([^/]+)\.sql", path)
    if m:
        stem = "test_" + m.group(1).replace("-", "_")
        chosen = {
            t["number"]
            for t in tests
            if t["file"] == "tests/test_optimizer.py"
            and (
                (name := t["id"].split("::")[-1]).startswith(stem)
                or stem.startswith(name)
                or name == "test_optimize"  # cheap safety net (0.4 s)
            )
        }
        return chosen if len(chosen) > 1 else None

    if re.fullmatch(r"tests/(.+/)?test_[^/]+\.py", path):
        return set(by_file[path]) if path in by_file else None

    m = re.fullmatch(r"sqlglot/(dialects|parsers|generators|typing)/([^/]+)\.py", path)
    if m and m.group(2) not in BROAD_STEMS:
        own = f"tests/dialects/test_{m.group(2)}.py"
        if own in by_file:
            names = [m.group(2)] + CHILDREN.get(m.group(2), [])
            files = [f"tests/dialects/test_{n}.py" for n in names]
            files += ["tests/dialects/test_dialect.py"] + GENERIC_FILES
            skip = {t["number"] for t in tests if t["id"].endswith(SLOW_UNLIKELY)}
            return {n for f in files for n in by_file.get(f, [])} - skip
    return None


def split(numbers, ms, worker_count):
    """Longest test first, each onto the currently lightest worker."""
    workers = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for n in sorted(numbers, key=lambda n: ms[n], reverse=True):
        i = loads.index(min(loads))
        workers[i].append(n)
        loads[i] += ms[n]
    return [w for w in workers if w], loads


def best_plan(numbers, ms):
    if not numbers:
        return []
    best, best_cost = None, None
    for w in range(1, min(MAX_WORKERS, len(numbers)) + 1):
        plan, loads = split(numbers, ms, w)
        cost = max(loads) + WORKER_MS * len(plan)
        if best_cost is None or cost < best_cost:
            best, best_cost = plan, cost
    return best


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    by_file: dict[str, list[int]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    all_numbers = frozenset(ms)
    cache: dict[frozenset, list[list[int]]] = {}

    for patch in sorted((DATA / "commits").glob("*.patch")):
        files = parse_patch(patch.read_text(errors="replace"))
        chosen: set[int] | None = set()
        for path, lines in files.items():
            if is_trivial(path, lines):
                continue
            part = pick(path, tests, by_file)
            if part is None:
                chosen = None
                break
            chosen |= part
        key = all_numbers if chosen is None else frozenset(chosen)
        if key not in cache:
            cache[key] = best_plan(key, ms)
        print(json.dumps({"commit": patch.stem, "workers": cache[key]}))


if __name__ == "__main__":
    main()