#!/usr/bin/env python3
"""Reuse only PyTorch 2.4.1+cu121's ABI-independent NVIDIA distributions.

Usage (site-packages paths, not environment roots):
  python reuse_cuda_wheels.py SOURCE DESTINATION             # read-only
  python reuse_cuda_wheels.py SOURCE DESTINATION --dry-run    # read-only
  python reuse_cuda_wheels.py SOURCE DESTINATION --apply

Payload files are hard-linked where possible, with copy2 fallback. Every
dist-info file is privately copied; RECORD is rebuilt without Python bytecode.
All source hashes and destination conflicts are checked before --apply writes.
Existing destination files are always rejected, including an earlier run.
Torch and Triton binaries are never copied. No package installer is invoked.

Hard-linked payloads must not be edited in place: unlink/replacement is safe,
but an in-place write would also alter the base environment's copy.
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
from email import message_from_bytes
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile


# The twelve distributions installed with the base torch 2.4.1+cu121 wheel.
# nvjitlink is a transitive dependency; the other eleven are Torch's exact pins.
VERSIONS = {
    "nvidia-cublas-cu12": "12.1.3.1",
    "nvidia-cuda-cupti-cu12": "12.1.105",
    "nvidia-cuda-nvrtc-cu12": "12.1.105",
    "nvidia-cuda-runtime-cu12": "12.1.105",
    "nvidia-cudnn-cu12": "9.1.0.70",
    "nvidia-cufft-cu12": "11.0.2.54",
    "nvidia-curand-cu12": "10.3.2.106",
    "nvidia-cusolver-cu12": "11.4.5.107",
    "nvidia-cusparse-cu12": "12.1.0.106",
    "nvidia-nccl-cu12": "2.20.5",
    "nvidia-nvjitlink-cu12": "12.1.105",
    "nvidia-nvtx-cu12": "12.1.105",
}
TAGS = {"py3-none-manylinux1_x86_64", "py3-none-manylinux2014_x86_64"}


@dataclass(frozen=True)
class Entry:
    relative: str
    source: Path
    digest: str
    size: int
    metadata: bool
    replacement: bytes | None = None


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")


def path_for(root: Path, relative: str) -> Path:
    """Reject traversal, noncanonical names, and symlinks in every component."""
    rel = PurePosixPath(relative)
    if (not relative or rel.is_absolute() or "\\" in relative or ":" in relative
            or rel.as_posix() != relative or any(p in (".", "..") for p in rel.parts)):
        raise ValueError(f"unsafe RECORD path: {relative!r}")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink is not allowed: {current}")
        if current != root / rel and current.exists() and not current.is_dir():
            raise ValueError(f"non-directory path component: {current}")
    return current


def regular_file(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"not a regular file: {path}")


def root_path(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"site-packages root must not be a symlink: {path}")
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    return root


def verify_base_torch(src: Path) -> None:
    path = path_for(src, "torch-2.4.1+cu121.dist-info/METADATA")
    regular_file(path)
    metadata = message_from_bytes(path.read_bytes())
    if metadata.get("Name") != "torch" or metadata.get("Version") != "2.4.1+cu121":
        raise ValueError("source must contain the base torch 2.4.1+cu121 metadata")
    requirements = metadata.get_all("Requires-Dist", [])
    for name, version in VERSIONS.items():
        if name == "nvidia-nvjitlink-cu12":
            continue
        pattern = rf"^{re.escape(name)}\s*\(?\s*==\s*{re.escape(version)}\s*\)?(?:\s*;|\s*$)"
        if not any(re.match(pattern, requirement) for requirement in requirements):
            raise ValueError(f"base Torch does not declare expected pin {name}=={version}")


def build_plan(src: Path) -> tuple[list[Entry], set[str]]:
    verify_base_torch(src)
    entries: dict[str, Entry] = {}
    skipped: set[str] = set()
    for name, version in VERSIONS.items():
        dirname = f"{name.replace('-', '_')}-{version}.dist-info"
        metadata_path = path_for(src, f"{dirname}/METADATA")
        wheel_path = path_for(src, f"{dirname}/WHEEL")
        record_path = path_for(src, f"{dirname}/RECORD")
        for path in (metadata_path, wheel_path, record_path):
            regular_file(path)
        metadata = message_from_bytes(metadata_path.read_bytes())
        wheel = message_from_bytes(wheel_path.read_bytes())
        if canonical(metadata.get("Name", "")) != name or metadata.get("Version") != version:
            raise ValueError(f"unexpected package identity: {dirname}")
        if metadata.get("Requires-Python") not in (">=3", ">=3.0"):
            raise ValueError(f"unapproved Requires-Python in {dirname}")
        tags = wheel.get_all("Tag", [])
        if (wheel.get("Root-Is-Purelib", "").lower() != "true"
                or not tags or not set(tags).issubset(TAGS)):
            raise ValueError(f"wheel is not an approved py3-none NVIDIA wheel: {dirname}")

        rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))))
        rebuilt: list[list[str]] = []
        recorded: set[str] = set()
        for row in rows:
            if len(row) != 3:
                raise ValueError(f"invalid RECORD row in {dirname}: {row!r}")
            relative, recorded_hash, recorded_size = row
            source = path_for(src, relative)
            if not (relative.startswith("nvidia/") or relative.startswith(dirname + "/")):
                raise ValueError(f"RECORD points outside this NVIDIA package: {relative}")
            if relative in recorded:
                raise ValueError(f"duplicate RECORD row: {relative}")
            recorded.add(relative)
            regular_file(source)
            if "__pycache__" in PurePosixPath(relative).parts or relative.endswith(".pyc"):
                skipped.add(relative)
                continue
            if "cpython-" in relative or re.search(r"(?:^|[._-])cp311(?:[._-]|$)", relative):
                raise ValueError(f"Python 3.11 ABI file is forbidden: {relative}")
            rebuilt.append(row)
            if source == record_path:
                if recorded_hash or recorded_size:
                    raise ValueError(f"RECORD must have an empty self-hash: {dirname}")
                continue
            if not recorded_hash.startswith("sha256=") or not recorded_size.isdigit():
                raise ValueError(f"missing SHA256/size for {relative}")
            size = int(recorded_size)
            digest = recorded_hash.removeprefix("sha256=")
            if source.stat().st_size != size:
                raise ValueError(f"source size mismatch: {relative}")
            entry = Entry(relative, source, digest, size, relative.startswith(dirname + "/"))
            if relative in entries:
                previous = entries[relative]
                if (previous.digest, previous.size, previous.metadata) != (digest, size, entry.metadata):
                    raise ValueError(f"conflicting package RECORDs: {relative}")
            else:
                if sha256(source) != digest:
                    raise ValueError(f"source SHA256 mismatch: {relative}")
                entries[relative] = entry

        required = {f"{dirname}/{filename}" for filename in ("METADATA", "WHEEL", "RECORD")}
        if not required.issubset(recorded):
            raise ValueError(f"RECORD omits its own package metadata: {dirname}")
        buffer = io.StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerows(rebuilt)
        data = buffer.getvalue().encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        relative = f"{dirname}/RECORD"
        entries[relative] = Entry(relative, record_path, digest, len(data), True, data)
    return sorted(entries.values(), key=lambda entry: entry.relative), skipped


def check_destinations(dst: Path, entries: list[Entry]) -> None:
    for name, version in VERSIONS.items():
        path = path_for(dst, f"{name.replace('-', '_')}-{version}.dist-info")
        if path.exists():
            raise ValueError(f"destination distribution already exists; refusing to overwrite: {path}")
    # Reject other versions too: installing two dist-info versions is ambiguous.
    for path in dst.glob("nvidia*.dist-info"):
        if any(path.name.startswith(name.replace("-", "_") + "-") for name in VERSIONS):
            raise ValueError(f"destination already has an NVIDIA distribution: {path}")
    for entry in entries:
        target = path_for(dst, entry.relative)
        if target.exists():
            raise ValueError(f"destination file already exists; refusing to overwrite: {target}")


def ensure_parents(dst: Path, target: Path, created: list[Path]) -> None:
    current = dst
    for part in target.relative_to(dst).parts[:-1]:
        current = current / part
        try:
            current.mkdir()
            created.append(current)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise ValueError(f"unsafe destination directory: {current}")


def apply_plan(src: Path, dst: Path, entries: list[Entry]) -> tuple[int, int]:
    linked = copied = 0
    published: list[tuple[Path, tuple[int, int]]] = []
    created: list[Path] = []
    same_fs = src.stat().st_dev == dst.stat().st_dev
    # This directory is created only in --apply and stays inside the requested dst.
    with tempfile.TemporaryDirectory(prefix=".reuse-cuda-wheels-", dir=dst) as temp:
        staging = Path(temp).resolve()
        if staging.parent != dst:
            raise ValueError("temporary directory resolved outside destination")
        for entry in entries:
            source = path_for(src, entry.relative)
            regular_file(source)
            staged = staging / entry.relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            if entry.replacement is not None:
                staged.write_bytes(entry.replacement)
            elif entry.metadata:
                shutil.copy2(source, staged)
            else:
                try:
                    if not same_fs:
                        raise OSError("different source/destination filesystems")
                    os.link(source, staged, follow_symlinks=False)
                    linked += 1
                except OSError:
                    shutil.copy2(source, staged)
                    copied += 1
            if staged.stat().st_size != entry.size or sha256(staged) != entry.digest:
                raise ValueError(f"source changed during preflight/staging: {entry.relative}")

        check_destinations(dst, entries)
        try:
            for entry in entries:
                target = path_for(dst, entry.relative)
                ensure_parents(dst, target, created)
                staged = staging / entry.relative
                # Atomic no-clobber publication. Metadata links to our PRIVATE
                # staged copy, never to the source environment's dist-info.
                os.link(staged, target, follow_symlinks=False)
                identity = target.stat()
                published.append((target, (identity.st_dev, identity.st_ino)))
        except (OSError, ValueError):
            # Remove only files still referring to the inode we just published.
            for path, expected in reversed(published):
                try:
                    actual = path.lstat()
                    if (actual.st_dev, actual.st_ino) == expected:
                        path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created):
                try:
                    path.rmdir()  # Empty directories only; never recursive.
                except OSError:
                    pass
            raise
    return linked, copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="base Python 3.11 site-packages")
    parser.add_argument("destination", type=Path, help="new Python 3.10 site-packages")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--apply", action="store_true", help="reuse after all checks pass")
    args = parser.parse_args()
    try:
        src, dst = root_path(args.source), root_path(args.destination)
        if src == dst or src.is_relative_to(dst) or dst.is_relative_to(src):
            raise ValueError("source and destination must be distinct, non-nested directories")
        entries, skipped = build_plan(src)
        check_destinations(dst, entries)
        payload = [entry for entry in entries if not entry.metadata]
        metadata = [entry for entry in entries if entry.metadata]
        print(f"Validated {len(VERSIONS)} NVIDIA py3-none distributions; {len(skipped)} bytecode files skipped")
        print(f"Payload: {len(payload)} files, {sum(e.size for e in payload):,} bytes")
        print(f"Private dist-info: {len(metadata)} files, {sum(e.size for e in metadata):,} bytes")
        print(f"Same filesystem: {src.stat().st_dev == dst.stat().st_dev}")
        if args.apply:
            linked, copied = apply_plan(src, dst, entries)
            print(f"APPLIED: {linked} payload files hard-linked, {copied} copied; all metadata private")
        else:
            print("DRY RUN: no files or directories written; use --apply to reuse")
        return 0
    except (OSError, ValueError, UnicodeError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
