#!/usr/bin/env python3
"""Add opt-in raster camera shaders to the pinned RoboTwin checkout.

Target: RoboTwin ea8b21121ebb3cd201ff5b3fe361944ac94eda3f.
Default --dry-run only checks and reports. --diff prints a unified patch;
--apply modifies the two checked source files. Already-patched files are
recognized by reversing the exact substitutions and checking the upstream
Git blob hash, so repeating this script is safe.

The patched application retains its original RT behavior when
ROBOTWIN_CAMERA_SHADER is unset or 'rt'. Setting it to 'default' configures
the raster shader before creating the renderer and skips the RT options.
For the DZN experiment also export SAPIEN_DISABLE_RAY_TRACING=1 before Python.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
from pathlib import Path
import sys


REVISION = "ea8b21121ebb3cd201ff5b3fe361944ac94eda3f"
SOURCE_BLOBS = {
    "envs/_base_task.py": "ace13e6c80f911ac32fb05332ac2e9896e37547c",
    "scripts/test_render.py": "3897dc00936542f118b2dc9c9275c154f80690c8",
}

BEFORE_RENDERER = '''        set_global_config(max_num_materials=50000, max_num_textures=50000)
        self.renderer = sapien.SapienRenderer()
'''
AFTER_RENDERER = '''        set_global_config(max_num_materials=50000, max_num_textures=50000)
        camera_shader = os.environ.get("ROBOTWIN_CAMERA_SHADER", "rt")
        if camera_shader not in ("rt", "default"):
            raise ValueError("ROBOTWIN_CAMERA_SHADER must be 'rt' or 'default'")
        if camera_shader == "default":
            # Set the raster shader before the renderer is initialized.
            sapien.render.set_camera_shader_dir("default")
        self.renderer = sapien.SapienRenderer()
'''
BEFORE_RT = '''        sapien.render.set_camera_shader_dir("rt")
        sapien.render.set_ray_tracing_samples_per_pixel(32)
        sapien.render.set_ray_tracing_path_depth(8)
        sapien.render.set_ray_tracing_denoiser("oidn")
'''
AFTER_RT = '''        if camera_shader == "rt":
            sapien.render.set_camera_shader_dir("rt")
            sapien.render.set_ray_tracing_samples_per_pixel(32)
            sapien.render.set_ray_tracing_path_depth(8)
            sapien.render.set_ray_tracing_denoiser("oidn")
'''
EDITS = ((BEFORE_RENDERER, AFTER_RENDERER), (BEFORE_RT, AFTER_RT))


def git_blob_hash(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def substitute(text: str, *, reverse: bool = False) -> str:
    edits = reversed(EDITS) if reverse else EDITS
    for old, new in edits:
        if reverse:
            old, new = new, old
        count = text.count(old)
        if count != 1:
            raise ValueError(f"expected one exact source fragment, found {count}")
        text = text.replace(old, new, 1)
    return text


def make_plan(root: Path) -> list[tuple[str, Path, bytes, bytes, str]]:
    plan = []
    for relative, expected in SOURCE_BLOBS.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{relative}: source resolves outside the requested root")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if git_blob_hash(raw) == expected:
            result = substitute(text).encode("utf-8")
            # Check that the substitutions are reversible before accepting them.
            if git_blob_hash(substitute(result.decode("utf-8"), reverse=True).encode("utf-8")) != expected:
                raise ValueError(f"{relative}: edit round-trip failed")
            state = "unpatched"
        else:
            try:
                reverted = substitute(text, reverse=True).encode("utf-8")
            except ValueError:
                raise ValueError(f"{relative}: wrong revision or unrelated modifications; no files written") from None
            if git_blob_hash(reverted) != expected:
                raise ValueError(f"{relative}: wrong revision or unrelated modifications; no files written")
            result = raw
            state = "already_patched"
        plan.append((relative, path, raw, result, state))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("robotwin_root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="check only (default)")
    mode.add_argument("--diff", action="store_true", help="print unified diff without writing")
    mode.add_argument("--apply", action="store_true", help="apply only after all files pass preflight")
    args = parser.parse_args()
    try:
        root = args.robotwin_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"not a source directory: {root}")
        plan = make_plan(root)
        if args.diff:
            for relative, _, before, after, _ in plan:
                sys.stdout.writelines(difflib.unified_diff(
                    before.decode("utf-8").splitlines(keepends=True),
                    after.decode("utf-8").splitlines(keepends=True),
                    fromfile="a/" + relative,
                    tofile="b/" + relative,
                ))
        elif args.apply:
            # Verify every source again before changing any source.
            for relative, path, before, _, _ in plan:
                if path.read_bytes() != before:
                    raise ValueError(f"{relative}: changed during preflight; no files written")
            changed = 0
            for _, path, before, after, _ in plan:
                if before != after:
                    path.write_bytes(after)
                    changed += 1
            print(f"Applied raster opt-in to {changed} files; {len(plan)-changed} already patched")
        else:
            for relative, _, _, _, state in plan:
                print(f"PASS {relative}: {state}")
            print(f"Pinned source {REVISION}; no files written")
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
