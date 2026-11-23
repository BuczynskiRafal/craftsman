"""Profile inheritance loader."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .interpolate import interpolate_config
from .merge import ConfigError, deep_merge


def resolve_profile(
    profiles: Mapping[str, Mapping[str, Any]],
    profile_name: str,
    *,
    env: Mapping[str, str] | None = None,
    resolve_interpolations: bool = True,
) -> dict[str, Any]:
    if profile_name not in profiles:
        raise ConfigError(f"unknown profile: {profile_name}")
    merged = deep_merge({}, profiles[profile_name])
    merged.pop("extends", None)
    if resolve_interpolations:
        return interpolate_config(merged, env=env)
    return merged
