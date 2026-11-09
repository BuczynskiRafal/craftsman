# Atomic saves, snapshots and namespaces for kvstore

Callers now keep several subsystems in one store and read it from a background thread, so:

- `Store.save()` must be atomic. If the process dies while saving, the file on disk is either the
  previous content or the new content, never truncated or half written.
- `Store.snapshot()` returns the live (not expired) entries as a dict that is independent of the
  store: writes made to the store afterwards, including changes to a stored list or dict value,
  must not show up in a snapshot that was already taken.
- `Store.namespace(name)` returns a view with `set`, `get`, `delete` and `keys` that keeps its
  keys under `name:`. The view's `keys()` returns names without the prefix.
