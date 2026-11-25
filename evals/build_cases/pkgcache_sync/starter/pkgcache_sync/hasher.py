"""SHA-256 helpers and domain errors for pkgcache_sync."""

from __future__ import annotations

import hashlib
from pathlib import Path


class CacheError(Exception):
    """Base error for package cache operations."""


class CacheCorruptionError(CacheError):
    """Raised when blob content fails SHA-256 verification or is missing on disk."""


class CacheCapacityError(CacheError):
    """Raised when an entry cannot fit within cache capacity even after eviction."""


class CacheKeyError(CacheError):
    """Raised when a requested cache key does not exist."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()
