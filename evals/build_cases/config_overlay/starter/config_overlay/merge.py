"""Deep merging and domain errors for config_overlay."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


class ConfigError(Exception):
    """Base error for configuration loading, merging, interpolation, or schema."""


class ConfigCycleError(ConfigError):
    """Raised when profile inheritance or variable interpolation contains a cycle."""


class ConfigValidationError(ConfigError):
    """Raised when configuration fails schema validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    # Starter bug: shallow copy + ignores $unset and $merge directives.
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out
