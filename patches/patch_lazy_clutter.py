#!/usr/bin/env python3
"""Defer RoboTwin clutter-asset metadata reads until clutter is requested.

Target: RoboTwin ea8b21121ebb3cd201ff5b3fe361944ac94eda3f.
The upstream utils package imports rand_create_cluttered_actor even for clean
tasks. This patch delays its metadata initialization until the first real
get_available_cluttered_objects call. That call still reads the official asset
JSONs and propagates missing/corrupt-asset errors; no data is fabricated.

Default --dry-run validates only; --diff prints the exact patch. --apply changes
only envs/utils/rand_create_cluttered_actor.py after a complete source-hash and
replacement preflight. Repeated application recognizes the exact patched file.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
from pathlib import Path
import sys


REVISION = "ea8b21121ebb3cd201ff5b3fe361944ac94eda3f"
RELATIVE = "envs/utils/rand_create_cluttered_actor.py"
SOURCE_BLOB = "15cd18dcfbffa095fbf9ac549b693c9f0dcea268"

EAGER = '''cluttered_objects_info, cluttered_objects_list, same_obj = get_all_cluttered_objects()
'''
LAZY = '''# Uninitialized sentinels: clean tasks do not need the clutter asset library.
cluttered_objects_info, cluttered_objects_list, same_obj = None, None, None


def _ensure_cluttered_objects_loaded():
    global cluttered_objects_info, cluttered_objects_list, same_obj
    if cluttered_objects_info is None:
        # Evaluate the complete load before updating globals. Missing/corrupt
        # official assets still fail when clutter is actually requested.
        cluttered_objects_info, cluttered_objects_list, same_obj = get_all_cluttered_objects()
'''
BEFORE_CALL = '''def get_available_cluttered_objects(entity_on_scene: list):
    global cluttered_objects_info, cluttered_objects_list, same_obj

    model_in_use = []
'''
AFTER_CALL = '''def get_available_cluttered_objects(entity_on_scene: list):
    global cluttered_objects_info, cluttered_objects_list, same_obj

    _ensure_cluttered_objects_loaded()
    model_in_use = []
'''
EDITS = ((EAGER, LAZY), (BEFORE_CALL, AFTER_CALL))


def blob_hash(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def substitute(text: str, reverse: bool = False) -> str:
    for old, new in reversed(EDITS) if reverse else EDITS:
        if reverse:
            old, new = new, old
        count = text.count(old)
        if count != 1:
            raise ValueError(f"expected one exact patch fragment, found {count}")
        text = text.replace(old, new, 1)
    return text


def make_plan(root: Path) -> tuple[Path, bytes, bytes, str]:
    target = (root / RELATIVE).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("source resolves outside the requested RoboTwin root")
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    if blob_hash(raw) == SOURCE_BLOB:
        result = substitute(text).encode("utf-8")
        if blob_hash(substitute(result.decode("utf-8"), reverse=True).encode("utf-8")) != SOURCE_BLOB:
            raise ValueError("patch round-trip failed")
        state = "unpatched"
    else:
        try:
            original = substitute(text, reverse=True).encode("utf-8")
        except ValueError:
            raise ValueError("wrong source revision or unrelated modifications; no files written") from None
        if blob_hash(original) != SOURCE_BLOB:
            raise ValueError("wrong source revision or unrelated modifications; no files written")
        result, state = raw, "already_patched"
    ast.parse(result, filename=str(target))
    return target, raw, result, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("robotwin_root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--diff", action="store_true", help="show patch without writing")
    mode.add_argument("--apply", action="store_true", help="apply only after full preflight")
    args = parser.parse_args()
    try:
        root = args.robotwin_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("RoboTwin root must be a directory")
        target, before, after, state = make_plan(root)
        if args.diff:
            sys.stdout.writelines(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile="a/" + RELATIVE, tofile="b/" + RELATIVE,
            ))
        elif args.apply:
            if target.read_bytes() != before:
                raise ValueError("source changed during preflight; no files written")
            if before != after:
                target.write_bytes(after)
            print(f"PASS {state}: {target}", flush=True)
        else:
            print(f"PASS {state}: {target}; pinned {REVISION}; no files written", flush=True)
        return 0
    except (OSError, ValueError, UnicodeError, SyntaxError) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
