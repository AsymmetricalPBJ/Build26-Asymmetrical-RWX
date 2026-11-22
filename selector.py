"""Blast Radius selector, v2.1: simple and conservative.

Idea: a miss costs 300 s, a full run about 37 s. Two tests (test_max_nodes at
28.8 s and test_execute_tpcds at 21.5 s) are most of that 37 s, and everything
else is cheap. So we only ever skip those two, and only when the patch clearly
cannot reach them. No lookup tables from patches to tests.

Rules, per changed file (a patch's tests are the union over its files):
  1. Pure documentation (.md / .rst)            -> nothing
  2. tests/fixtures/optimizer/X.sql             -> optimizer tests named like X
  3. A tests/**/test_*.py file                  -> the tests in that file
  4. sqlglot/{dialects,parsers,generators,typing}/NAME.py (one dialect)
                                                -> ALL tests except the two
                                                   slow ones; the TPC-DS executor
                                                   test comes back for duckdb,
                                                   which that test transpiles to
  5. Anything else (core code, unknown files,
     comment-only edits to any of it)           -> ALL tests
Comment-only edits get no special treatment: the README says a comment is not
promised to be harmless.
"""

import json
import os
import re
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
MAX_WORKERS = 8
WORKER_MS = 2000

# The two tests that dominate runtime (ids as in tests.json).
SLOW_PARSER = "tests/test_parser.py::TestParser::test_max_nodes"      # parses with the default dialect only
SLOW_EXECUTOR = "tests/test_executor.py::TestExecutor::test_execute_tpcds"  # compares against duckdb

# Files that change behaviour for every dialect: no shortcut.
BROAD_STEMS = {"dialect", "base", "__init__"}


def changed_paths(patch_text: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)", patch_text, re.M)


def select(path: str, tests: list[dict], by_file: dict[str, list[int]], slow: dict[str, int]):
    """Test numbers for one changed file, or None meaning 'run all'."""
    if path.endswith((".md", ".rst")):
        return set()

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
                or name == "test_optimize"  # cheap safety net
            )
        }
        return chosen if len(chosen) > 1 else None

    if re.fullmatch(r"tests/(.+/)?test_[^/]+\.py", path):
        return set(by_file[path]) if path in by_file else None

    m = re.fullmatch(r"sqlglot/(dialects|parsers|generators|typing)/([^/]+)\.py", path)
    if m and m.group(2) not in BROAD_STEMS:
        chosen = {t["number"] for t in tests} - set(slow.values())
        if m.group(2) == "duckdb":
            chosen.add(slow[SLOW_EXECUTOR])
        return chosen

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
    """Try 1..8 workers; keep the one minimizing busiest load + 2 s per worker."""
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
    ids: dict[str, int] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
        ids[t["id"]] = t["number"]
    slow = {SLOW_PARSER: ids.get(SLOW_PARSER), SLOW_EXECUTOR: ids.get(SLOW_EXECUTOR)}
    all_numbers = frozenset(ms)
    plans: dict[frozenset, list[list[int]]] = {}

    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen: set[int] | None = set()
        for path in changed_paths(patch.read_text(errors="replace")):
            if None in slow.values():  # test list changed shape: stay safe
                chosen = None
                break
            part = select(path, tests, by_file, slow)
            if part is None:
                chosen = None
                break
            chosen |= part
        key = all_numbers if chosen is None else frozenset(chosen)
        if key not in plans:
            plans[key] = best_plan(key, ms)
        print(json.dumps({"commit": patch.stem, "workers": plans[key]}))


if __name__ == "__main__":
    main()