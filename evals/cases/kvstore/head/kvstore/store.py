"""JSON file key-value store with optional expiry."""

from __future__ import annotations

import json
import shutil
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

    def snapshot(self) -> dict[str, Entry]:
        """Live entries as a copy that later writes to the store do not change."""
        return {k: e for k, e in self._data.items() if not self._expired(e)}

    def namespace(self, name: str) -> Namespace:
        return Namespace(self, name)

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True), encoding="utf-8")
        shutil.copyfile(tmp, self.path)
        tmp.unlink()


class Namespace:
    """A view of a store whose keys all live under ``<name>:``."""

    def __init__(self, store: Store, name: str) -> None:
        self._store = store
        self._prefix = f"{name}:"

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._store.set(self._prefix + key, value, ttl)

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(self._prefix + key, default)

    def delete(self, key: str) -> bool:
        return self._store.delete(self._prefix + key)

    def keys(self) -> list[str]:
        start = len(self._prefix)
        return [k[start:] for k in self._store.keys() if k.startswith(self._prefix)]
