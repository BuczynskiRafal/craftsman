"""Garbage collection helpers for unreferenced blobs and staging files."""

from __future__ import annotations

from pathlib import Path

from .manifest import CacheManifest


def collect_garbage(root: Path, manifest: CacheManifest) -> list[str]:
    del root, manifest
    return []
