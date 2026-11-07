"""JSON file key-value store with optional expiry."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Clock = Callable[[], float]
Entry = dict[str, Any]


class Store:
    def __init__(self, path: Path, clock: Clock = time.time) -> None:
        self.path = path
        self._clock = clock
        self._data: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        self._data = json.loads(raw)

    def _expired(self, entry: Entry) -> bool:
        expires = entry["expires"]
        return expires is not None and expires <= self._clock()

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expires = None if ttl is None else self._clock() + ttl
        self._data[key] = {"value": value, "expires": expires}

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._data.get(key)
        if entry is None or self._expired(entry):
            return default
        return entry["value"]

    def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(k for k, e in self._data.items() if not self._expired(e))

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, sort_keys=True), encoding="utf-8")
