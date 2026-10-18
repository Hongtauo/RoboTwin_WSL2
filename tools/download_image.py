"""Download the exact public base image without HTTP content compression."""

import concurrent.futures
import hashlib
import json
from pathlib import Path
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "pytorch/pytorch"
TAG = "2.4.1-cuda12.1-cudnn9-devel"
TARGET = f"docker.1ms.run/{REPOSITORY}:{TAG}"
EXPECTED_CONFIG = "sha256:2641894d738f3cf1ff80f74a77a10d6601ce09e8f37813094ad1a289763f4658"
ROOT = (Path(__file__).resolve().parent / "downloads").resolve()
CHUNK_SIZE = 32 * 1024 * 1024
WORKERS = 8
LOCK = threading.Lock()
AUTH_LOCK = threading.Lock()
PROGRESS = {"bytes": 0, "verified_bytes": 0}
AUTH = {"token": "", "expires": 0.0}


def emit(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def read_small(url, headers=None):
    request = urllib.request.Request(
        url, headers={"Accept-Encoding": "identity", **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.headers.get("Content-Encoding") not in (None, "identity"):
            raise RuntimeError("Unexpected HTTP content encoding")
        return response.read(4 * 1024 * 1024)


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    blob_root = ROOT / "blobs" / "sha256"
    blob_root.mkdir(parents=True, exist_ok=True)
    token_url = "https://auth.docker.io/token?" + urllib.parse.urlencode(
        {"service": "registry.docker.io", "scope": f"repository:{REPOSITORY}:pull"}
    )
    def authorization():
        with AUTH_LOCK:
            if time.monotonic() >= AUTH["expires"]:
                reply = json.loads(read_small(token_url))
                AUTH["token"] = reply["token"]
                AUTH["expires"] = time.monotonic() + max(10, reply.get("expires_in", 300) - 30)
            return {"Authorization": f"Bearer {AUTH['token']}"}

    manifest_raw = read_small(
        f"https://registry-1.docker.io/v2/{REPOSITORY}/manifests/{TAG}",
        {**authorization(), "Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    manifest = json.loads(manifest_raw)
    if manifest["config"]["digest"] != EXPECTED_CONFIG:
        raise RuntimeError("The remote tag no longer matches the inspected image")
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    (blob_root / manifest_hash).write_bytes(manifest_raw)
    descriptors = [manifest["config"], *manifest["layers"]]
    total = sum(item["size"] for item in descriptors)
    jobs = []
    pending_blobs = []
    started = time.monotonic()

    for item in descriptors:
        algorithm, digest = item["digest"].split(":", 1)
        if algorithm != "sha256" or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise RuntimeError("Invalid blob descriptor")
        destination = blob_root / digest
        if destination.is_file() and destination.stat().st_size == item["size"]:
            if checksum(destination) == digest:
                PROGRESS["bytes"] += item["size"]
                PROGRESS["verified_bytes"] += item["size"]
                continue
        part_root = ROOT / "parts" / digest
        part_root.mkdir(parents=True, exist_ok=True)
        pending_blobs.append((item, destination, part_root))
        for offset in range(0, item["size"], CHUNK_SIZE):
            length = min(CHUNK_SIZE, item["size"] - offset)
            part = part_root / f"{offset:012d}.part"
            if part.is_file() and part.stat().st_size == length:
                PROGRESS["bytes"] += length
                continue
            jobs.append((item, digest, offset, length, part))

    emit(stage="download", total_bytes=total, workers=WORKERS, chunks=len(jobs))

    def download(job):
        item, digest, offset, length, part = job
        temporary = part.with_suffix(".partial")
        for attempt in range(1, 5):
            counted = 0
            try:
                request = urllib.request.Request(
                    f"https://registry-1.docker.io/v2/{REPOSITORY}/blobs/sha256:{digest}",
                    headers={
                        **authorization(),
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={offset}-{offset + length - 1}",
                    },
                )
                with urllib.request.urlopen(request, timeout=40) as response:
                    encoding = response.headers.get("Content-Encoding")
                    if encoding not in (None, "identity"):
                        raise RuntimeError("Unexpected HTTP content encoding")
                    if response.status == 206:
                        wanted = f"bytes {offset}-{offset + length - 1}/{item['size']}"
                        if response.headers.get("Content-Range") != wanted:
                            raise RuntimeError("The server returned a different byte range")
                    elif response.status != 200 or offset != 0 or length != item["size"]:
                        raise RuntimeError("The server did not honor the byte range")
                    with temporary.open("wb") as output:
                        while counted < length:
                            block = response.read(min(256 * 1024, length - counted))
                            if not block:
                                raise RuntimeError("Incomplete blob range")
                            output.write(block)
                            counted += len(block)
                            with LOCK:
                                PROGRESS["bytes"] += len(block)
                temporary.replace(part)
                return
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError) and error.code == 401:
                    with AUTH_LOCK:
                        AUTH["expires"] = 0.0
                with LOCK:
                    PROGRESS["bytes"] -= counted
                emit(stage="retry", layer=digest[:12], offset=offset,
                     attempt=attempt, error_type=type(error).__name__)
                if attempt == 4:
                    raise RuntimeError(f"Blob range failed: {digest[:12]} at {offset}") from None
                time.sleep(attempt * 2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        remaining = {executor.submit(download, job) for job in jobs}
        while remaining:
            completed, remaining = concurrent.futures.wait(
                remaining, timeout=10, return_when=concurrent.futures.FIRST_EXCEPTION
            )
            for future in completed:
                future.result()
            elapsed = time.monotonic() - started
            with LOCK:
                received = PROGRESS["bytes"]
            emit(stage="progress", downloaded_bytes=received, total_bytes=total,
                 percent=round(received / total * 100, 1),
                 elapsed_seconds=round(elapsed, 1))

    for item, destination, part_root in pending_blobs:
        digest = item["digest"].split(":", 1)[1]
        temporary = destination.with_suffix(".partial")
        result = hashlib.sha256()
        with temporary.open("wb") as output:
            for offset in range(0, item["size"], CHUNK_SIZE):
                part = part_root / f"{offset:012d}.part"
                with part.open("rb") as source:
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                        result.update(block)
                        output.write(block)
        if result.hexdigest() != digest or temporary.stat().st_size != item["size"]:
            raise RuntimeError(f"Blob SHA-256 verification failed: {digest[:12]}")
        temporary.replace(destination)
        PROGRESS["verified_bytes"] += item["size"]
        emit(stage="verified", layer=digest[:12], bytes=item["size"])
        # Delete only this downloader's verified, explicitly named chunk files.
        for offset in range(0, item["size"], CHUNK_SIZE):
            part = part_root / f"{offset:012d}.part"
            if not part.resolve().is_relative_to(ROOT):
                raise RuntimeError("Chunk path escaped the experiment directory")
            part.unlink()

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": manifest["mediaType"],
            "digest": f"sha256:{manifest_hash}",
            "size": len(manifest_raw),
            "platform": {"architecture": "amd64", "os": "linux"},
            "annotations": {
                "io.containerd.image.name": TARGET,
                "org.opencontainers.image.ref.name": TARGET,
            },
        }],
    }
    (ROOT / "index.json").write_text(json.dumps(index), encoding="utf-8")
    (ROOT / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    archive = ROOT / "pytorch-2.4.1-cu121.oci.tar"
    emit(stage="archive", output=str(archive))
    with tarfile.open(archive, "w") as output:
        for metadata in ("index.json", "oci-layout"):
            output.add(ROOT / metadata, arcname=metadata, recursive=False)
        for digest in [manifest_hash, *[item["digest"].split(":", 1)[1] for item in descriptors]]:
            output.add(blob_root / digest, arcname=f"blobs/sha256/{digest}", recursive=False)
    emit(stage="complete", archive=str(archive), image=TARGET,
         manifest_digest=f"sha256:{manifest_hash}", verified_bytes=PROGRESS["verified_bytes"])


if __name__ == "__main__":
    main()
