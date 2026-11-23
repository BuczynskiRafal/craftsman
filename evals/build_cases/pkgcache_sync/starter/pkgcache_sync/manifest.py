"""Cache manifest metadata persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    key: str
    digest: str
    size: int
    deps: tuple[str, ...] = ()
    pinned: bool = False
    last_used_seq: int = 0


@dataclass
class CacheManifest:
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    next_access_seq: int = 1

    def allocate_seq(self) -> int:
        seq = self.next_access_seq
        self.next_access_seq += 1
        return seq

    def referenced_digests(self) -> set[str]:
        return {e.digest for e in self.entries.values()}

    def save(self, path: Path) -> None:
        payload: dict[str, Any] = {
            "next_access_seq": self.next_access_seq,
            "entries": {
                k: {
                    "digest": e.digest,
                    "size": e.size,
                    "deps": list(e.deps),
                    "pinned": e.pinned,
                    "last_used_seq": e.last_used_seq,
                }
                for k, e in self.entries.items()
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> CacheManifest:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, CacheEntry] = {}
        for key, item in raw.get("entries", {}).items():
            entries[key] = CacheEntry(
                key=key,
                digest=item["digest"],
                size=int(item["size"]),
                deps=tuple(item.get("deps", ())),
                pinned=bool(item.get("pinned", False)),
                last_used_seq=int(item.get("last_used_seq", 0)),
            )
        return cls(entries=entries, next_access_seq=int(raw.get("next_access_seq", 1)))
