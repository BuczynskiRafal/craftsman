"""Main content-addressed cache implementation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .gc import collect_garbage
from .hasher import (
    CacheError,
    CacheKeyError,
    sha256_bytes,
)
from .manifest import CacheEntry, CacheManifest


class PackageCache:
    def __init__(self, root: Path, *, max_bytes: int = 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise CacheError("max_bytes must be positive")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.blobs_dir = self.root / "blobs"
        self.tmp_dir = self.root / "tmp"
        self.manifest_path = self.root / "manifest.json"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = CacheManifest.load(self.manifest_path)

    def used_bytes(self) -> int:
        # Starter bug: sums per-entry sizes instead of deduplicated digests.
        return sum(e.size for e in self.manifest.entries.values())

    def put(
        self,
        key: str,
        data: bytes,
        *,
        expected_sha256: str | None = None,
        deps: Sequence[str] = (),
        pinned: bool = False,
    ) -> CacheEntry:
        del expected_sha256
        if not key.strip():
            raise CacheError("key must be non-empty")
        digest = sha256_bytes(data)
        blob_path = self.blobs_dir / digest
        blob_path.write_bytes(data)
        entry = CacheEntry(
            key=key,
            digest=digest,
            size=len(data),
            deps=tuple(deps),
            pinned=pinned,
            last_used_seq=self.manifest.allocate_seq(),
        )
        self.manifest.entries[key] = entry
        self.manifest.save(self.manifest_path)
        return entry

    def get(self, key: str) -> bytes:
        entry = self.manifest.entries.get(key)
        if entry is None:
            raise CacheKeyError(f"missing key: {key}")
        # Starter bug: skips SHA-256 verification on read.
        entry.last_used_seq = self.manifest.allocate_seq()
        self.manifest.save(self.manifest_path)
        return (self.blobs_dir / entry.digest).read_bytes()

    def delete(self, key: str) -> bool:
        entry = self.manifest.entries.pop(key, None)
        if entry is None:
            return False
        (self.blobs_dir / entry.digest).unlink(missing_ok=True)
        self.manifest.save(self.manifest_path)
        return True

    def pin(self, key: str) -> None:
        entry = self.manifest.entries.get(key)
        if entry is None:
            raise CacheKeyError(f"missing key: {key}")
        entry.pinned = True
        self.manifest.save(self.manifest_path)

    def unpin(self, key: str) -> None:
        entry = self.manifest.entries.get(key)
        if entry is None:
            raise CacheKeyError(f"missing key: {key}")
        entry.pinned = False
        self.manifest.save(self.manifest_path)

    def gc(self) -> list[str]:
        return collect_garbage(self.root, self.manifest)
