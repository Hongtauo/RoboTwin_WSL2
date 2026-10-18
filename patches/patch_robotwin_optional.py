#!/usr/bin/env python3
"""Check or opt in to the pinned RoboTwin dependency source adjustments.

Default: dry-run; no files are written. Run in the intended Python environment:
    python patch_robotwin_optional.py
    python patch_robotwin_optional.py --components mplib --diff
    python patch_robotwin_optional.py --apply

Installed files are located with importlib.metadata, without importing SAPIEN or
MPLib. --urdf-loader and --mplib-planner select explicit source files instead.
For example, the container's SAPIEN source file is:
    /opt/robotwin-build/SAPIEN/python/py_package/wrapper/urdf_loader.py

Every selected file must match its pinned whole-file SHA256 after reversing only
the recognized edits. All files are checked before --apply writes any file.
This script does not install packages or build/reinstall a modified source tree.

The SAPIEN changes add UTF-8 reads. Its original [:-4] + "srdf" already produces
robot.srdf from robot.urdf and is left intact. The known double-dot variant is
repaired to [:-5] + ".srdf". The MPLib change follows RoboTwin/scripts/_install.sh:
remove only `or collide` from the screw failure condition, keeping both the
remaining condition and its failure return. It does not disable that guard.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import difflib
import hashlib
from importlib import metadata
from pathlib import Path
import sys


SAPIEN_SHA256 = "eabba86fe7d244ce8d469010f96914010a8883ea5835c86a8bc58be310510683"
MPLIB_SHA256 = "785f77233baf4159f3327b664fae2507572c189223a6075f42546c1133ddfa0e"
SAPIEN_VERSION = "3.0.0b1"
MPLIB_VERSION = "0.2.1"

URDF_READ = 'with open(urdf_file, "r") as f:'
SRDF_READ = 'with open(srdf_file, "r") as f:'
SRDF_PATH = 'srdf_file = urdf_file[:-4] + "srdf"'
SCREW_BEFORE = (
    '            if np.linalg.norm(delta_twist) < 1e-4 or collide or not within_joint_limit:\n'
    '                return {"status": "screw plan failed"}'
)
SCREW_AFTER = (
    '            if np.linalg.norm(delta_twist) < 1e-4 or not within_joint_limit:\n'
    '                return {"status": "screw plan failed"}'
)


@dataclass(frozen=True)
class Plan:
    component: str
    path: Path
    origin: str
    before: bytes
    after: bytes
    expected_sha256: str
    details: tuple[str, ...]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def one_variant(text: str, variants: tuple[str, ...], label: str) -> str:
    """Accept one exact known fragment, including partial/already-patched states."""
    counts = [(variant, text.count(variant)) for variant in variants]
    if sum(count for _, count in counts) != 1:
        raise ValueError(f"{label}: expected exactly one recognized source fragment")
    return next(variant for variant, count in counts if count == 1)


def prepare_sapien(text: str) -> tuple[str, str, tuple[str, ...]]:
    canonical = text
    patched = text
    details = []
    for variable, original in (("urdf_file", URDF_READ), ("srdf_file", SRDF_READ)):
        desired = f'with open({variable}, "r", encoding="utf-8") as f:'
        alternate = f'with open({variable}, "r", encoding=\'utf-8\') as f:'
        found = one_variant(text, (original, desired, alternate), f"{variable} UTF-8 read")
        canonical = canonical.replace(found, original, 1)
        # Preserve the recognized quote style on already-patched files.
        if found == original:
            patched = patched.replace(found, desired, 1)
        details.append(f"{variable} UTF-8: {'pending' if found == original else 'already'}")

    # The original suffix is correct: removing 'urdf' leaves the existing dot.
    correct_forms = (
        SRDF_PATH,
        "srdf_file = urdf_file[:-4] + 'srdf'",
        'srdf_file = urdf_file[:-5] + ".srdf"',
        "srdf_file = urdf_file[:-5] + '.srdf'",
    )
    double_dot_forms = (
        'srdf_file = urdf_file[:-4] + ".srdf"',
        "srdf_file = urdf_file[:-4] + '.srdf'",
    )
    found = one_variant(text, correct_forms + double_dot_forms, "SRDF filename")
    canonical = canonical.replace(found, SRDF_PATH, 1)
    if found in double_dot_forms:
        patched = patched.replace(found, 'srdf_file = urdf_file[:-5] + ".srdf"', 1)
        details.append("SRDF filename: pending double-dot repair")
    else:
        details.append("SRDF filename: already correct")
    return canonical, patched, tuple(details)


def prepare_mplib(text: str) -> tuple[str, str, tuple[str, ...]]:
    found = one_variant(text, (SCREW_BEFORE, SCREW_AFTER), "MPLib screw failure guard")
    canonical = text.replace(found, SCREW_BEFORE, 1)
    patched = text.replace(found, SCREW_AFTER, 1)
    state = "pending" if found == SCREW_BEFORE else "already"
    return canonical, patched, (f"remove only 'or collide': {state}; failure guard and return preserved",)


def installed_path(component: str) -> tuple[Path, str]:
    """Inspect distribution metadata only; native packages must not be imported."""
    try:
        distribution = metadata.distribution(component)
    except metadata.PackageNotFoundError:
        flag = "--urdf-loader" if component == "sapien" else "--mplib-planner"
        raise ValueError(
            f"{component}: no installed distribution in {sys.executable}; "
            f"run in the intended environment or select a pinned source file with {flag}"
        ) from None
    version = distribution.version
    if component == "sapien":
        if not version.startswith(SAPIEN_VERSION):
            raise ValueError(f"sapien: expected version starting {SAPIEN_VERSION}, found {version}")
        relative = "sapien/wrapper/urdf_loader.py"
    else:
        if version != MPLIB_VERSION:
            raise ValueError(f"mplib: expected version {MPLIB_VERSION}, found {version}")
        relative = "mplib/planner.py"
    return Path(distribution.locate_file(relative)), f"installed {component} {version}"


def make_plan(component: str, explicit_path: Path | None) -> Plan:
    if explicit_path is None:
        path, origin = installed_path(component)
    else:
        path = explicit_path
        origin = "explicit file; pinned version verified by normalized whole-file hash"
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{component}: not a regular file: {path}")
    before = path.read_bytes()
    source = before.decode("utf-8")
    expected = SAPIEN_SHA256 if component == "sapien" else MPLIB_SHA256
    transform = prepare_sapien if component == "sapien" else prepare_mplib
    canonical, patched, details = transform(source)
    actual = sha256(canonical.encode("utf-8"))
    if actual != expected:
        raise ValueError(
            f"{component}: wrong source revision or unrelated modifications in {path}; "
            f"normalized SHA256 {actual}, expected {expected}"
        )
    # Syntax-check without executing imports. Reversing the result must retain
    # the exact original whole-file hash, including untouched bytes/newlines.
    ast.parse(patched, filename=str(path))
    normalized_after, unchanged_after, _ = transform(patched)
    if sha256(normalized_after.encode("utf-8")) != expected or unchanged_after != patched:
        raise ValueError(f"{component}: patch round-trip or repeat-safety check failed")
    return Plan(component, path, origin, before, patched.encode("utf-8"), expected, details)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--components", choices=("both", "sapien", "mplib"), default="both")
    parser.add_argument("--urdf-loader", type=Path, help="explicit pinned SAPIEN source/installed file")
    parser.add_argument("--mplib-planner", type=Path, help="explicit pinned MPLib source/installed file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="check/report only (default)")
    mode.add_argument("--diff", action="store_true", help="report and show unified diffs; no writes")
    mode.add_argument("--apply", action="store_true", help="write selected files after all preflight checks pass")
    args = parser.parse_args(argv)
    if args.components == "sapien" and args.mplib_planner is not None:
        parser.error("--mplib-planner requires --components mplib or both")
    if args.components == "mplib" and args.urdf_loader is not None:
        parser.error("--urdf-loader requires --components sapien or both")

    try:
        selected = ("sapien", "mplib") if args.components == "both" else (args.components,)
        explicit = {"sapien": args.urdf_loader, "mplib": args.mplib_planner}
        # Build and validate the complete plan before producing any writes.
        plans = [make_plan(component, explicit[component]) for component in selected]
        if len({plan.path for plan in plans}) != len(plans):
            raise ValueError("selected dependency paths must be distinct")
        print(f"Python environment: {sys.executable}")
        for plan in plans:
            state = "already_patched" if plan.before == plan.after else "pending"
            print(f"PASS {plan.component}: {state}: {plan.path}")
            print(f"  {plan.origin}; normalized SHA256 {plan.expected_sha256}")
            for detail in plan.details:
                print(f"  {detail}")
        if args.diff:
            for plan in plans:
                sys.stdout.writelines(difflib.unified_diff(
                    plan.before.decode("utf-8").splitlines(keepends=True),
                    plan.after.decode("utf-8").splitlines(keepends=True),
                    fromfile=str(plan.path),
                    tofile=str(plan.path) + " (patched)",
                ))
        if args.apply:
            # Recheck every input before the first write, including files whose
            # recognized changes are already present.
            for plan in plans:
                if plan.path.read_bytes() != plan.before:
                    raise ValueError(f"{plan.path}: changed during preflight; no files written")
            sys.stdout.flush()
            changed = 0
            for plan in plans:
                if plan.before != plan.after:
                    plan.path.write_bytes(plan.after)
                    changed += 1
            print(f"Applied to {changed} selected file(s); {len(plans) - changed} already patched")
        else:
            print("Dry-run complete; no files written. Use --apply to opt in.")
        return 0
    except (OSError, ValueError, UnicodeError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
