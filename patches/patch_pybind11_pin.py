#!/usr/bin/env python3
"""Pin both SAPIEN 3.0.0b1 bindings to the archived smart_holder source.

This only edits the two FetchContent declarations when explicitly executed.
It does not download dependencies, clear caches, or start a build.
The commit retains smart_holder.h and PYBIND11_USE_SMART_HOLDER_AS_DEFAULT.
Its version label is 3.0.0.dev1; it predates the released pybind11 3 API changes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


COMMIT = "68a11bb65b774df161c898204d08e85ac2cbac5f"
ARCHIVE_URL = f"https://github.com/pybind/pybind11/archive/{COMMIT}.tar.gz"
ARCHIVE_SHA256 = "cc03b749e1e69faf0ef20927d3e9caa793009f87ed5e0ca8b164c82e0a5bd081"
ARCHIVE_SIZE = 874_379
DECLARATION = (
    "FetchContent_Declare(\n"
    "  pybind11\n"
    f"  URL {ARCHIVE_URL}\n"
    f"  URL_HASH SHA256={ARCHIVE_SHA256}\n"
    "  DOWNLOAD_EXTRACT_TIMESTAMP FALSE\n"
    ")"
)
ORIGINAL = re.compile(
    r"FetchContent_Declare\(\s*pybind11\s+"
    r"GIT_REPOSITORY\s+https://github\.com/pybind/pybind11\.git\s+"
    r"GIT_TAG\s+smart_holder\s+GIT_SHALLOW\s+TRUE\s+GIT_PROGRESS\s+TRUE\s*\)"
)
FILES = (Path("cmake/pybind11.cmake"), Path("pinocchio/cmake/pybind11.cmake"))


def transform(source: str) -> str:
    newline = "\r\n" if "\r\n" in source else "\n"
    replacement = DECLARATION.replace("\n", newline)
    count = len(ORIGINAL.findall(source))
    if count == 0 and replacement in source:
        return source
    if count != 1:
        raise ValueError(f"Expected one original smart_holder FetchContent declaration; found {count}")
    return ORIGINAL.sub(lambda _: replacement, source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    root = arguments.source_root.resolve(strict=True)
    plans = []
    for relative in FILES:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError(f"Build declaration escaped source root: {path}")
        original = path.read_bytes()
        patched = transform(original.decode("utf-8")).encode("utf-8")
        plans.append((path, relative, original, patched))

    # Check both declarations before writing either one.
    report = []
    for path, relative, original, patched in plans:
        changed = original != patched
        if changed and not arguments.dry_run:
            path.write_bytes(patched)
        report.append({"path": relative.as_posix(), "changed": changed,
                       "written": changed and not arguments.dry_run})
    print(json.dumps({"source_root": str(root), "commit": COMMIT,
                      "archive_url": ARCHIVE_URL, "sha256": ARCHIVE_SHA256,
                      "archive_size": ARCHIVE_SIZE, "dry_run": arguments.dry_run,
                      "files": report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
