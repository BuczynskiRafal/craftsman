"""Schema validation and secret redaction."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from .merge import ConfigValidationError


def validate_schema(config: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    errors: list[str] = []
    for field, rules in schema.items():
        if rules.get("required") and field not in config:
            errors.append(f"missing required field: {field}")
    if errors:
        raise ConfigValidationError(errors)


def redact_secrets(
    config: Mapping[str, Any],
    secret_keys: Iterable[str] = ("password", "secret", "token", "api_key"),
) -> dict[str, Any]:
    keys = set(secret_keys)
    out = copy.deepcopy(dict(config))
    for k in list(out):
        if k in keys:
            out[k] = "***"
    return out
