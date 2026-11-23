"""Snapshot persistence for SearchIndex."""

from __future__ import annotations

import json
from pathlib import Path

from .index import SearchIndex


def save_snapshot(index: SearchIndex, path: Path) -> None:
    data = {
        "docs": [
            {"doc_id": doc_id, "text": text, "facets": index.docs_facets.get(doc_id, {})}
            for doc_id, text in index.docs_text.items()
        ]
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


def load_snapshot(path: Path) -> SearchIndex:
    raw = json.loads(path.read_text(encoding="utf-8"))
    idx = SearchIndex()
    for item in raw.get("docs", []):
        idx.add_or_update(item["doc_id"], item["text"], item.get("facets"))
    return idx
