"""Photo storage with a switchable backend: local disk or an S3-compatible store.

    FACEMARK_STORAGE=local   (default) photos under DATA_DIR, where they have
                             always lived. Nothing to configure.
    FACEMARK_STORAGE=s3      photos in a bucket - AWS S3, Cloudflare R2, MinIO,
                             any S3 API. Survives the container being replaced.

WHY KEYS, NOT PATHS
-------------------
Every photo is addressed as (prefix, name), where `prefix` is "students" or
"uploads" and `name` is a bare filename. That is already what the database
holds for most rows, because save_image() only ever returned `path.name`.

The exception is students.photo_path, which older code wrote as a full absolute
path. Callers therefore pass `Path(value).name`, which is correct for both the
old absolute paths and the new bare names - so no data migration is needed to
switch a deployment over.

Nothing here caches. A miss returns None and the caller decides whether that is
a 404 or a skip; raising would turn one deleted crop into a broken page.
"""
from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import config

log = logging.getLogger("storage")

# The only two prefixes in use. Validated on every call so a caller cannot
# reach outside them by passing "../".
PREFIXES = ("students", "uploads")


def _check(prefix: str, name: str) -> tuple[str, str]:
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown storage prefix: {prefix!r}")
    # Path(...).name strips any directory component, so "../../etc/passwd"
    # collapses to "passwd" and cannot escape the prefix.
    safe = Path(name).name
    if not safe:
        raise ValueError("Empty storage key")
    return prefix, safe


def _media_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


class LocalStorage:
    """Photos on the local filesystem, under DATA_DIR/<prefix>/."""

    name = "local"

    def _path(self, prefix: str, name: str) -> Path:
        prefix, name = _check(prefix, name)
        return config.DATA_DIR / prefix / name

    def put(self, prefix: str, name: str, data: bytes) -> str:
        p = self._path(prefix, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return Path(name).name

    def get(self, prefix: str, name: str) -> Optional[bytes]:
        p = self._path(prefix, name)
        try:
            return p.read_bytes()
        except OSError:
            return None

    def exists(self, prefix: str, name: str) -> bool:
        return self._path(prefix, name).exists()

    def delete(self, prefix: str, name: str) -> None:
        try:
            self._path(prefix, name).unlink(missing_ok=True)
        except OSError:
            pass

    def response(self, prefix: str, name: str):
        p = self._path(prefix, name)
        if not p.exists():
            raise HTTPException(404, "Not found")
        # FileResponse streams from disk without loading the file into memory.
        return FileResponse(p, media_type=_media_type(name))


class S3Storage:
    """Photos in an S3-compatible bucket.

    `endpoint_url` is what makes this work with Cloudflare R2 and MinIO as well
    as AWS; left unset, boto3 talks to AWS S3.
    """

    name = "s3"

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    def _s3(self):
        # Imported lazily so a local-storage deployment never needs boto3
        # installed, and an import error surfaces only to whoever asked for S3.
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        import boto3
                        from botocore.config import Config as BotoConfig
                    except ImportError as e:
                        raise RuntimeError(
                            "FACEMARK_STORAGE=s3 needs boto3. Install it with:\n"
                            "    pip install boto3"
                        ) from e
                    if not config.S3_BUCKET:
                        raise RuntimeError(
                            "FACEMARK_STORAGE=s3 needs S3_BUCKET to be set"
                        )
                    self._client = boto3.client(
                        "s3",
                        endpoint_url=config.S3_ENDPOINT_URL or None,
                        region_name=config.S3_REGION or None,
                        aws_access_key_id=config.S3_ACCESS_KEY_ID or None,
                        aws_secret_access_key=config.S3_SECRET_ACCESS_KEY or None,
                        config=BotoConfig(
                            signature_version="s3v4",
                            retries={"max_attempts": 3, "mode": "standard"},
                        ),
                    )
                    log.info(
                        "S3 storage ready (bucket=%s endpoint=%s)",
                        config.S3_BUCKET, config.S3_ENDPOINT_URL or "aws",
                    )
        return self._client

    def _key(self, prefix: str, name: str) -> str:
        prefix, name = _check(prefix, name)
        root = config.S3_PREFIX.strip("/")
        return f"{root}/{prefix}/{name}" if root else f"{prefix}/{name}"

    def put(self, prefix: str, name: str, data: bytes) -> str:
        self._s3().put_object(
            Bucket=config.S3_BUCKET,
            Key=self._key(prefix, name),
            Body=data,
            ContentType=_media_type(name),
        )
        return Path(name).name

    def get(self, prefix: str, name: str) -> Optional[bytes]:
        try:
            obj = self._s3().get_object(
                Bucket=config.S3_BUCKET, Key=self._key(prefix, name)
            )
            return obj["Body"].read()
        except Exception as e:  # noqa: BLE001 - NoSuchKey and transport errors alike
            log.debug("S3 get miss for %s/%s: %s", prefix, name, e)
            return None

    def exists(self, prefix: str, name: str) -> bool:
        try:
            self._s3().head_object(
                Bucket=config.S3_BUCKET, Key=self._key(prefix, name)
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, prefix: str, name: str) -> None:
        try:
            self._s3().delete_object(
                Bucket=config.S3_BUCKET, Key=self._key(prefix, name)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("S3 delete failed for %s/%s: %s", prefix, name, e)

    def response(self, prefix: str, name: str):
        data = self.get(prefix, name)
        if data is None:
            raise HTTPException(404, "Not found")
        # Streamed through the app rather than redirected to a presigned URL,
        # so the existing session check still gates access to a photo of a minor.
        return StreamingResponse(io.BytesIO(data), media_type=_media_type(name))


_backend = None
_backend_lock = threading.Lock()


def get_storage():
    """The configured backend, built once per process."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                choice = (config.STORAGE_BACKEND or "local").strip().lower()
                if choice == "s3":
                    _backend = S3Storage()
                elif choice == "local":
                    _backend = LocalStorage()
                else:
                    raise RuntimeError(
                        f"FACEMARK_STORAGE={choice!r} is not valid. "
                        f"Use 'local' or 's3'."
                    )
                log.info("Photo storage backend: %s", _backend.name)
    return _backend


# --- module-level convenience ------------------------------------------------
# Callers say storage.put(...) rather than storage.get_storage().put(...).

def put(prefix: str, name: str, data: bytes) -> str:
    return get_storage().put(prefix, name, data)


def get(prefix: str, name: str) -> Optional[bytes]:
    return get_storage().get(prefix, name)


def exists(prefix: str, name: str) -> bool:
    return get_storage().exists(prefix, name)


def delete(prefix: str, name: str) -> None:
    get_storage().delete(prefix, name)


def response(prefix: str, name: str):
    return get_storage().response(prefix, name)


def backend_name() -> str:
    return get_storage().name
