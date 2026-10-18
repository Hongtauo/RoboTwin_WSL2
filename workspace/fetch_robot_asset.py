#!/usr/bin/env python3
"""Fetch only the official RoboTwin aloha-agilex robot asset.

Defaults: verified ZIP in /workspace/cache, assets in /workspace/RoboTwin/assets,
and provenance in /workspace/diagnostics/robot_asset.json. No authentication is
used. API metadata pins the revision and LFS SHA256 before downloading. Signed
redirect URLs are never printed. Other RoboTwin asset archives are not fetched.

Existing robot files are skipped only when their size and CRC32 match the ZIP;
different files, symlinks and unsafe paths are rejected without overwriting.
Every selected ZIP member is read to EOF so zipfile checks its CRC, including
members whose destination already exists. New files are staged before publish.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib


REPO = "TianxingChen/RoboTwin2.0"
EXPECTED_REVISION = "3dc3b798668feb99ac61cc9086d84cbcc3d79186"
EXPECTED_SHA256 = "85ffeff55a5066def5931224a85cfa3f8abaa1fbf779bd17789a7fb3f85bc789"
ARCHIVE = "embodiments.zip"
EXPECTED_SIZE = 219_847_741
PREFIX = "embodiments/aloha-agilex/"
API = f"https://huggingface.co/api/datasets/{REPO}/revision/{EXPECTED_REVISION}?blobs=true"
BLOCK = 4 * 1024 * 1024


class AssetError(Exception):
    pass


def open_public(url: str):
    """Anonymous HTTPS only; hide redirect locations from error messages."""
    request = urllib.request.Request(url, headers={"User-Agent": "RoboTwin-minimal-asset/1"})
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise AssetError(f"official asset request failed with HTTP {code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AssetError("official asset request failed (network/TLS); no URL or credentials logged") from None
    if response.status != 200:
        response.close()
        raise AssetError("official asset request returned an unexpected status")
    if not response.geturl().startswith("https://"):
        response.close()
        raise AssetError("refusing a non-HTTPS redirect")
    return response


def pin_archive() -> dict:
    with open_public(API) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise AssetError("repository metadata response exceeds safety limit")
    metadata = json.loads(raw)
    if not isinstance(metadata, dict):
        raise AssetError("repository metadata has an unexpected structure")
    revision = metadata.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise AssetError("API did not provide a full immutable revision")
    if revision != EXPECTED_REVISION:
        raise AssetError("asset API revision differs from the recorded reproduction")
    matches = [entry for entry in metadata.get("siblings", []) if entry.get("rfilename") == ARCHIVE]
    if len(matches) != 1:
        raise AssetError("API did not identify exactly one embodiments.zip")
    entry = matches[0]
    lfs = entry.get("lfs") or {}
    digest = lfs.get("sha256", "")
    size = lfs.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AssetError("API did not provide an LFS SHA256")
    if digest != EXPECTED_SHA256:
        raise AssetError("asset SHA256 differs from the recorded reproduction")
    if size != EXPECTED_SIZE or entry.get("size", EXPECTED_SIZE) != EXPECTED_SIZE:
        raise AssetError(f"unexpected official archive size; expected {EXPECTED_SIZE} bytes")
    return {"repo": REPO, "revision": revision, "archive": ARCHIVE,
            "sha256": digest, "size": size}


def safe_absolute(path: Path) -> Path:
    """Normalize a requested root without traversing an existing symlink."""
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise AssetError(f"symlink is forbidden in destination path: {current}")
        if current != absolute and current.exists() and not current.is_dir():
            raise AssetError(f"non-directory path component: {current}")
    return absolute


def file_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def publish_no_replace(source: Path, destination: Path) -> None:
    """Publish without clobbering; DrvFs may require an exclusive-copy fallback."""
    try:
        os.link(source, destination, follow_symlinks=False)
        return
    except OSError as exc:
        # EEXIST deliberately propagates. Other failures also remain failures
        # unless the filesystem cannot create the requested hard link.
        fallback_errors = {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOTSUP}
        if exc.errno not in fallback_errors:
            raise

    identity = None
    try:
        # O_EXCL guarantees that neither a file nor a symlink is overwritten.
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "wb") as output:
            own_stat = os.fstat(output.fileno())
            identity = (own_stat.st_dev, own_stat.st_ino)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, BLOCK)
    except BaseException:
        if identity is not None:
            try:
                if file_identity(destination) == identity:
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise


def regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise AssetError(f"not a regular file: {path}")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def obtain_archive(cache: Path, pin: dict) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    if not cache.is_dir():
        raise AssetError("cache path is not a directory")
    archive = safe_absolute(cache / f"embodiments-{pin['sha256']}.zip")
    if archive.exists():
        regular(archive)
        if archive.stat().st_size != pin["size"] or hash_file(archive) != pin["sha256"]:
            raise AssetError("existing cache archive failed SHA256/size; refusing to overwrite")
        print("Verified existing official archive cache", flush=True)
        return archive

    url = f"https://huggingface.co/datasets/{REPO}/resolve/{pin['revision']}/{ARCHIVE}"
    handle, temporary = tempfile.mkstemp(prefix=".robotwin-embodiments-", suffix=".part", dir=cache)
    temporary = Path(temporary)
    try:
        total = 0
        digest = hashlib.sha256()
        with os.fdopen(handle, "wb") as output, open_public(url) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != pin["size"]:
                raise AssetError("download Content-Length differs from pinned size")
            while chunk := response.read(min(BLOCK, pin["size"] - total + 1)):
                total += len(chunk)
                if total > pin["size"]:
                    raise AssetError("download exceeds pinned archive size")
                digest.update(chunk)
                output.write(chunk)
        if total != pin["size"] or digest.hexdigest() != pin["sha256"]:
            raise AssetError("download failed pinned SHA256/size verification")
        # Publish without replacing another cache file, including on DrvFs.
        try:
            publish_no_replace(temporary, archive)
        except FileExistsError:
            regular(archive)
            if archive.stat().st_size != pin["size"] or hash_file(archive) != pin["sha256"]:
                raise AssetError("another cache file appeared with different content")
        print(f"Downloaded and SHA256-verified {total:,} bytes", flush=True)
        return archive
    finally:
        temporary.unlink(missing_ok=True)


def member_path(info: zipfile.ZipInfo) -> PurePosixPath | None:
    if not info.filename.startswith(PREFIX):
        return None
    name = info.filename.rstrip("/") if info.is_dir() else info.filename
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or "\\" in name or ":" in name or "\0" in name
            or relative.as_posix() != name or ".." in relative.parts):
        raise AssetError(f"unsafe ZIP member name: {info.filename!r}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG):
        raise AssetError(f"symlink or special ZIP member is forbidden: {info.filename}")
    if info.flag_bits & 1:
        raise AssetError("encrypted robot ZIP members are unsupported")
    return relative


def crc_file(path: Path) -> tuple[int, int]:
    crc = size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b""):
            size += len(chunk)
            crc = zlib.crc32(chunk, crc)
    return size, crc & 0xFFFFFFFF


def matching_file(path: Path, info: zipfile.ZipInfo) -> bool:
    regular(path)
    return path.stat().st_size == info.file_size and crc_file(path) == (info.file_size, info.CRC)


def read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, output=None) -> None:
    size = 0
    with archive.open(info) as source:
        for chunk in iter(lambda: source.read(BLOCK), b""):
            size += len(chunk)
            if output is not None:
                output.write(chunk)
    if size != info.file_size:
        raise AssetError(f"ZIP member size mismatch: {info.filename}")
    # zipfile validates the expected CRC while consuming the complete stream.


def ensure_parents(root: Path, target: Path, created: list[Path]) -> None:
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current = current / part
        try:
            current.mkdir()
            created.append(current)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise AssetError(f"unsafe extraction directory: {current}")


def extract_robot(archive_path: Path, assets: Path) -> dict:
    if assets.exists() and not assets.is_dir():
        raise AssetError("assets root is not a directory")
    with zipfile.ZipFile(archive_path) as archive:
        files: list[tuple[zipfile.ZipInfo, Path, bool]] = []
        seen: set[str] = set()
        directory_names: set[str] = set()
        for info in archive.infolist():
            relative = member_path(info)
            if relative is None:
                continue
            if str(relative) in seen:
                raise AssetError(f"duplicate ZIP member: {info.filename}")
            seen.add(str(relative))
            target = safe_absolute(assets / relative)
            if not target.is_relative_to(assets):
                raise AssetError("ZIP destination escaped assets root")
            if info.is_dir():
                if target.exists() and not target.is_dir():
                    raise AssetError(f"existing extraction directory conflicts: {target}")
                directory_names.add(str(relative))
                read_member(archive, info)
                continue
            exists = target.exists()
            if exists and not matching_file(target, info):
                raise AssetError(f"existing robot file differs; refusing to overwrite: {target}")
            files.append((info, target, exists))
        if not files or not any(info.filename == PREFIX + "config.yml" for info, _, _ in files):
            raise AssetError("official archive lacks the expected aloha-agilex config.yml")
        file_names = {info.filename for info, _, _ in files}
        for filename in file_names:
            parents = PurePosixPath(filename).parents
            if filename in directory_names or any(str(parent) in file_names for parent in parents):
                raise AssetError("ZIP contains file/directory path conflicts")

        assets.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        published: list[tuple[Path, tuple[int, int]]] = []
        with tempfile.TemporaryDirectory(prefix=".robotwin-asset-", dir=assets) as temporary:
            staging = Path(temporary).resolve()
            if staging.parent != assets:
                raise AssetError("staging directory escaped assets root")
            for info, target, exists in files:
                if exists:
                    read_member(archive, info)  # Validate source CRC even when skipping.
                else:
                    staged = staging / target.relative_to(assets)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with staged.open("xb") as output:
                        read_member(archive, info, output)

            # No final robot file has been changed before all selected CRCs pass.
            for info, target, existed in files:
                safe_absolute(target)
                if existed:
                    if not target.exists() or not matching_file(target, info):
                        raise AssetError("existing robot file changed during extraction preflight")
                elif target.exists():
                    raise AssetError("destination appeared during extraction; refusing to overwrite")
            try:
                for info, target, exists in files:
                    if exists:
                        continue
                    safe_absolute(target)
                    ensure_parents(assets, target, created)
                    publish_no_replace(staging / target.relative_to(assets), target)
                    published.append((target, file_identity(target)))
            except (OSError, AssetError):
                for target, identity in reversed(published):
                    try:
                        if file_identity(target) == identity:
                            target.unlink()
                    except FileNotFoundError:
                        pass
                for directory in reversed(created):
                    try:
                        directory.rmdir()  # Only our empty directories, never recursive.
                    except OSError:
                        pass
                raise
    return {"selected_prefix": PREFIX, "selected_files": len(files),
            "extracted_files": sum(not exists for _, _, exists in files),
            "skipped_matching_files": sum(exists for _, _, exists in files),
            "selected_uncompressed_bytes": sum(info.file_size for info, _, _ in files),
            "crc_checked_files": len(files), "assets_root": str(assets)}


def check_diagnostics(path: Path) -> None:
    if path.exists():
        regular(path)
        try:
            previous = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise AssetError("diagnostics path contains unrelated content; refusing to overwrite") from None
        if (not isinstance(previous, dict) or previous.get("tool") != "fetch_robot_asset"
                or previous.get("repo") != REPO):
            raise AssetError("diagnostics path contains unrelated content; refusing to overwrite")


def save_diagnostics(path: Path, data: dict) -> None:
    safe_absolute(path)
    check_diagnostics(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".robot-asset-diagnostics-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, sort_keys=True)
            output.write("\n")
        safe_absolute(path)
        check_diagnostics(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("/workspace/cache"))
    parser.add_argument("--assets-dir", type=Path, default=Path("/workspace/RoboTwin/assets"))
    parser.add_argument("--diagnostics", type=Path, default=Path("/workspace/diagnostics/robot_asset.json"))
    args = parser.parse_args()
    try:
        cache, assets, diagnostics = map(safe_absolute, (args.cache_dir, args.assets_dir, args.diagnostics))
        check_diagnostics(diagnostics)
        pin = pin_archive()
        print(f"Pinned official revision {pin['revision']}, {pin['size']:,} bytes, SHA256 {pin['sha256']}", flush=True)
        archive = obtain_archive(cache, pin)
        result = extract_robot(archive, assets)
        report = {"tool": "fetch_robot_asset", "status": "success", **pin, **result,
                  "verified_archive_path": str(archive)}
        save_diagnostics(diagnostics, report)
        print(f"Robot assets ready: {result['extracted_files']} files extracted, "
              f"{result['skipped_matching_files']} matching files skipped; all selected CRCs checked", flush=True)
        print(f"Provenance saved to {diagnostics}", flush=True)
        return 0
    except (AssetError, OSError, ValueError, zipfile.BadZipFile, RuntimeError,
            EOFError, zlib.error, http.client.HTTPException) as exc:
        # urllib errors can carry signed URLs. Network messages are normalized in
        # open_public; body-read errors are hidden instead of rendering the URL.
        if isinstance(exc, (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException)):
            message = "network read failed; no URL or credentials logged"
        else:
            message = str(exc)
        print(f"ERROR: {message}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
