"""Attachments stored on disk, one directory per ticket."""

from __future__ import annotations

import re
from pathlib import Path

TICKET_RE = re.compile(r"[A-Z]{2,5}-\d{1,8}")


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, ticket: str) -> Path:
        if not TICKET_RE.fullmatch(ticket):
            raise ValueError(f"bad ticket id: {ticket!r}")
        return self.root / ticket

    def _file(self, ticket: str, filename: str) -> Path:
        name = Path(filename).name
        if not name:
            raise ValueError("empty file name")
        return self._dir(ticket) / name

    def save(self, ticket: str, filename: str, data: bytes) -> Path:
        target = self._file(ticket, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def read(self, ticket: str, filename: str) -> bytes:
        return self._file(ticket, filename).read_bytes()

    def list(self, ticket: str) -> list[str]:
        base = self._dir(ticket)
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_file())

    def delete(self, ticket: str, filename: str) -> None:
        self._file(ticket, filename).unlink()
