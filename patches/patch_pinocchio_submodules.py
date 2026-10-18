#!/usr/bin/env python3
"""Keep only Pinocchio v2.6.18's jrl CMake submodule when tests are off.

Only edits the SAPIEN FetchContent declaration. No downloads, cache cleanup,
process termination, or build commands are executed. Supports --dry-run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def transform(source: str) -> str:
    if not re.search(r"set\(\s*BUILD_TESTING\s+OFF\s*\)", source):
        raise ValueError("Expected BUILD_TESTING OFF before omitting robot test models")
    pattern = re.compile(r"FetchContent_Declare\(\s*pinocchio\s+.*?^\)", re.S | re.M)
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError(f"Expected one Pinocchio FetchContent declaration, found {len(matches)}")
    match = matches[0]
    block = match.group()
    if "https://github.com/stack-of-tasks/pinocchio.git" not in block:
        raise ValueError("Unexpected Pinocchio repository")
    if not re.search(r"GIT_TAG\s+v2\.6\.18\b", block):
        raise ValueError("Expected the inspected Pinocchio v2.6.18 tag")
    expected = "    GIT_SUBMODULES cmake\n    GIT_SUBMODULES_RECURSE FALSE\n"
    newline = "\r\n" if "\r\n" in source else "\n"
    expected = expected.replace("\n", newline)
    if re.search(r"\bGIT_SUBMODULES(?:_RECURSE)?\b", block):
        if expected in block:
            return source
        raise ValueError("Different submodule selection already exists; refusing to replace it")
    patched = block[:-1] + expected + ")"
    return source[:match.start()] + patched + source[match.end():]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve(strict=True)
    relative = Path("pinocchio/cmake/pinocchio.cmake")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Build declaration resolves outside the requested source checkout")
    original = path.read_bytes()
    patched = transform(original.decode("utf-8")).encode("utf-8")
    changed = patched != original
    if changed and not args.dry_run:
        path.write_bytes(patched)
    print(json.dumps({"path": str(path), "changed": changed,
                      "written": changed and not args.dry_run,
                      "git_submodules": ["cmake"],
                      "git_submodules_recurse": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
