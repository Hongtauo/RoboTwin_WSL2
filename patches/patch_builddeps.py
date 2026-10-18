#!/usr/bin/env python3
"""Patch SAPIEN 3.0.0b1 build inputs without changing OIDN.

Run inside the build container after both SAPIEN submodules are initialized:
    python /workspace/patches/patch_builddeps.py /opt/robotwin-build/SAPIEN

Use --dry-run to validate and report changes without writing source files.
Repeated runs are safe. The TinyXML version and original MD5 remain unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


TINYXML_MD5 = "c1b864c96804a10526540c664ade67f0"
TINYXML_OLD_URL = (
    "https://cytranet.dl.sourceforge.net/project/tinyxml/"
    "tinyxml/2.6.2/tinyxml_2_6_2.tar.gz"
)
TINYXML_NEW_URL = (
    "https://downloads.sourceforge.net/project/tinyxml/"
    "tinyxml/2.6.2/tinyxml_2_6_2.tar.gz"
)


def patch_architecture(source: str, target: str, architecture: str) -> str:
    pattern = re.compile(
        r"(set_target_properties\(\s*"
        + re.escape(target)
        + r'\s+PROPERTIES\s+CUDA_ARCHITECTURES\s+")[^"]*("\s*\))'
    )
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError(
            f"Expected one {target} CUDA_ARCHITECTURES assignment, found {len(matches)}"
        )
    return pattern.sub(lambda match: match[1] + architecture + match[2], source)


def patch_tinyxml(source: str) -> str:
    if not re.search(r"URL_HASH\s+MD5=" + TINYXML_MD5 + r"\b", source):
        raise ValueError("The expected original TinyXML 2.6.2 MD5 is missing")
    old_count = source.count(TINYXML_OLD_URL)
    new_count = source.count(TINYXML_NEW_URL)
    if old_count + new_count != 1:
        raise ValueError("Expected exactly one old or patched TinyXML source URL")
    patched = source.replace(TINYXML_OLD_URL, TINYXML_NEW_URL)
    assert TINYXML_MD5 in patched
    return patched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--cuda-arch", default="89")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9]+", arguments.cuda_arch):
        parser.error("--cuda-arch must be one numeric compute capability, such as 89")

    root = arguments.source_root.resolve(strict=True)
    specifications = (
        (
            Path("CMakeLists.txt"),
            lambda source: patch_architecture(source, "sapien", arguments.cuda_arch),
        ),
        (
            Path("3rd_party/simsense/CMakeLists.txt"),
            lambda source: patch_architecture(source, "simsense", arguments.cuda_arch),
        ),
        (Path("pinocchio/cmake/pinocchio.cmake"), patch_tinyxml),
    )

    # Validate every expected source pattern before writing any file.
    planned = []
    for relative_path, transform in specifications:
        path = (root / relative_path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError(f"Source path resolves outside the requested checkout: {path}")
        original = path.read_bytes()
        patched = transform(original.decode("utf-8")).encode("utf-8")
        planned.append((path, relative_path, original, patched))

    report = []
    for path, relative_path, original, patched in planned:
        changed = original != patched
        if changed and not arguments.dry_run:
            path.write_bytes(patched)
        report.append(
            {"path": relative_path.as_posix(), "changed": changed, "written": changed and not arguments.dry_run}
        )
    print(
        json.dumps(
            {
                "source_root": str(root),
                "cuda_arch": arguments.cuda_arch,
                "tinyxml_url": TINYXML_NEW_URL,
                "tinyxml_md5": TINYXML_MD5,
                "oidn": "unchanged; upstream source build retained",
                "dry_run": arguments.dry_run,
                "files": report,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
