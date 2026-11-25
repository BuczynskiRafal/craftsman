# Task: Complete Layered Config Resolver, Inheritance, Interpolation & Secret Redaction

Complete `config_overlay` so profile inheritance, deep merging with list directives and tombstones, cycle-safe interpolation with escaping, multi-error schema validation, and non-mutating secret redaction satisfy every requirement below while keeping `tests/test_starter.py` passing.

## Requirements

1. **Deep Merge & List Directives (`merge.py`)**
   - `deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]` must never mutate inputs and must return independent deep copies.
   - Nested dicts merge recursively.
   - Tombstone removal: if `overlay[k]` is the dict `{"$unset": True}`, remove key `k` from the merged dict (if present) rather than copying `{"$unset": True}` into the output.
   - List merge directives: if `overlay[k]` is a dict containing `"$merge"` and `"values"` (a `list`):
     - Require `base.get(k)` (when present) to be a `list`, otherwise raise `ConfigError`.
     - `"replace"`: use a copy of `values`.
     - `"append"`: `[*base_list, *values]`.
     - `"prepend"`: `[*values, *base_list]`.
     - `"unique_append"`: keep `base_list` elements in order and append items from `values` not already present in the result list (preserving first-seen order).
     - Raise `ConfigError` on unknown `$merge` mode or non-list `values`.
   - Plain lists (without `$merge`) replace the base value completely.

2. **Profile Inheritance & Precedence (`loader.py`)**
   - `resolve_profile(profiles: Mapping[str, Mapping[str, Any]], profile_name: str, *, env: Mapping[str, str] | None = None, resolve_interpolations: bool = True) -> dict[str, Any]`
   - Raise `ConfigError` if `profile_name` (or any referenced `extends` target) does not exist.
   - Profiles may specify `"extends": str | list[str]`. The `"extends"` key MUST be stripped from resolved output.
   - Cycle detection: raise `ConfigCycleError` (subclass of `ConfigError`) if inheritance contains a cycle (self or indirect).
   - Diamond inheritance precedence: flatten ancestors via depth-first post-order left-to-right **with ancestor deduplication** (each ancestor is merged only at its first appearance so a later sibling extending the same shared base does NOT reset overrides made by an earlier sibling). Finally overlay the target profile itself.
     - Example: `prod` extends `["left", "right"]`; both `left` and `right` extend `base`. Merge order is `[base, left, right, prod]`.

3. **String Interpolation, Type Preservation & Cycles (`interpolate.py`)**
   - `interpolate_config(config: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> dict[str, Any]` returns a new dict without mutating `config`.
   - Supports `${expr}` and `${expr:-default}` inside strings (in dicts and lists):
     - If `expr` starts with `"env."` (`env.NAME`), read `NAME` from `env`. Treat missing or empty string `""` as unset (falls back to `default` if `:-default` is supplied, else raises `ConfigError`). Note: empty `default` (`${env.FOO:-}`) resolves to `""`.
     - Otherwise, treat `expr` as a dotted config path relative to the root of `config` (e.g. `${db.host}`). Missing keys or `None` values use `default` if supplied, else raise `ConfigError`.
   - Escaping: `\${...}` must render as literal `${...}` WITHOUT being expanded, even when values reference other interpolated keys!
   - Transitive resolution & cycles:
     - Chained references (e.g. `url: "https://${host}:${port}"` where `host: "${env.HOST:-localhost}"`) resolve transitively.
     - Fallback defaults may themselves contain `${...}` expressions (e.g. `${env.HOST:-${fallback_host}}`), so placeholder parsing must match balanced braces rather than stopping at the first `}`.
     - If resolving a config key depends cyclically on itself (directly or indirectly), raise `ConfigCycleError`.
   - Type preservation:
     - When a string value consists *solely* of one single unescaped placeholder (`"${port}"`) and the resolved target is a non-string (`int`, `float`, `bool`, `list`, `dict`), return a deep copy (with nested interpolations applied) of that typed value instead of converting it to `str`.
     - Embedded placeholders (e.g. `"${host}:${port}"`) convert values to `str`.

4. **Schema Validation & Secret Redaction (`schema.py`)**
   - `validate_schema(config: Mapping[str, Any], schema: Mapping[str, Any]) -> None`:
     - Schema mapping `{field: rule_dict}` where `rule_dict` supports `"type"` (`"str"|"int"|"float"|"bool"|"dict"|"list"`, rejecting `bool` for `"int"`/`"float"`), `"required"` (bool), `"min"` / `"max"` (numeric value bounds or string/list `len` bounds), `"fields"` (nested schema dict for dicts), and `"items"` (rule dict for list items).
     - MUST collect **all** validation errors across the entire config (not stop at the first error) and raise `ConfigValidationError(errors)` with `.errors: list[str]`.
   - `redact_secrets(config: Mapping[str, Any], secret_keys: Iterable[str] = ("password", "secret", "token", "api_key")) -> dict[str, Any]`:
     - Return a NEW dict with all matching keys (case-insensitive exact key name match OR dotted full-path match) recursively replaced by `"***"` across nested dicts and lists of dicts, WITHOUT mutating `config`.
