"""Configuration string interpolation."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .merge import ConfigError

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_.-]+)(?::-([^}]*))?\}")


def interpolate_config(
    config: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env_map = dict(env or {})
    root = copy.deepcopy(dict(config))

    def lookup(expr: str) -> Any:
        if expr.startswith("env."):
            return env_map.get(expr[4:])
        cur: Any = root
        for part in expr.split("."):
            if isinstance(cur, Mapping) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    def walk(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [walk(item) for item in val]
        if isinstance(val, str):
            def repl(m: re.Match[str]) -> str:
                key, default = m.group(1), m.group(2)
                resolved = lookup(key)
                if resolved is None or (key.startswith("env.") and resolved == ""):
                    if default is not None:
                        return default
                    raise ConfigError(f"missing variable: {key}")
                return str(resolved)

            return _VAR_RE.sub(repl, val)
        return val

    return walk(root)
