"""Run from the project root:  python3 analyze_sample.py

For every sample commit: what kind it is, which files the patch changes, and
which test files contain the tests that actually fail. Use it to find the
patch -> failing-test relationships your selector should encode.
"""

import json
import re
from pathlib import Path

S = Path("sample")
tests = {t["number"]: t for t in json.loads((S / "tests.json").read_text())["tests"]}
answers = json.loads((S / "answers.json").read_text())["commits"]

for patch in sorted((S / "commits").glob("*.patch"), key=lambda p: answers[p.stem]["kind"]):
    commit = patch.stem
    info = answers[commit]
    changed = re.findall(r"^diff --git a/(\S+) b/", patch.read_text(), re.M)
    failing = info["failing"]
    fail_files = sorted({tests[n]["file"].removeprefix("tests/") for n in failing})
    print(f"{commit}  {info['kind']:8s} fails={len(failing):3d}  changed={changed}")
    if failing:
        shown = [tests[n]["id"].split("::", 1)[1] for n in failing[:4]]
        print(f"      in {fail_files[:5]}{' ...' if len(fail_files) > 5 else ''}")
        print(f"      e.g. {shown}")
