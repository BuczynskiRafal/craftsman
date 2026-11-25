# Task: Complete Content-Addressed Package Cache with Integrity Verification, Transitive Pinning, Atomic Staging & GC

Complete `pkgcache_sync` so SHA-256 integrity checks, atomic blob staging, blob deduplication across keys, transitive dependency pinning on LRU eviction, transactional capacity limits, and garbage collection satisfy every requirement below while keeping `tests/test_starter.py` passing.

## Requirements

1. **Content-Addressed Storage & Atomic Staging (`hasher.py`, `cache.py`)**
   - Blobs are stored by SHA-256 hex digest at `<root>/blobs/<digest>`.
   - `PackageCache.put(key, data: bytes, *, expected_sha256: str | None = None, deps: Sequence[str] = (), pinned: bool = False) -> CacheEntry`
   - Validate non-empty `key` (raise `CacheError` otherwise).
   - Compute `digest = sha256_bytes(data)`. If `expected_sha256` is supplied and differs (case-insensitive comparison supported by lowering `expected_sha256`), raise `CacheCorruptionError` **before** modifying the cache or manifest.
   - Validate all `deps`: every `dep` must already exist in the cache manifest and `dep != key` (else raise `CacheError` before modifying state).
   - Stage new blobs to a temporary file inside `<root>/tmp/` and atomically move into `<root>/blobs/<digest>` via `os.replace()`. Clean up temporary staging files on failure.

2. **Integrity on Read & Monotonic LRU (`cache.py`, `manifest.py`)**
   - `PackageCache.get(key: str) -> bytes`
   - Raise `CacheKeyError` if `key` is not in the manifest.
   - Read `<root>/blobs/<digest>` and verify `sha256_bytes(content) == entry.digest`. If the blob file is missing on disk or its digest mismatches, raise `CacheCorruptionError`.
   - On successful `get` (and `put`), update `entry.last_used_seq` using a strictly increasing monotonic counter persisted in the manifest (`next_access_seq`) so LRU order is well-defined even within the same timestamp tick.

3. **Eviction, Transitive Pinning & Capacity Guarantees (`cache.py`, `manifest.py`)**
   - Cache size accounting: `PackageCache.used_bytes()` equals the sum of `size` across all **distinct** `digest`s currently referenced in the manifest (`sum(blob_size(d) for d in manifest.referenced_digests())`). Storing a duplicate payload under a new key reuses the existing blob and adds `0` additional blob bytes.
   - Eviction protection: a key `k` is **protected** if `entry.pinned == True` OR if any `pinned == True` key depends on `k` directly or transitively (`P -> A -> B` protects both `A` and `B`). Protected keys must never be evicted.
   - Eviction order & transactional capacity check on `put`:
     - Evictable entries are unprotected keys sorted by `(last_used_seq, key)` ascending (least-recently-used first).
     - Before modifying disk or manifest on `put`, compute whether `used_bytes` after replacing `key` (and freeing its previous blob if no other entry references it) and adding `new_bytes` fits within `max_bytes` once evictable unprotected entries are removed.
     - If even evicting all unprotected entries would still exceed `max_bytes`, raise `CacheCapacityError` **without evicting any existing entries or writing the new entry**.

4. **Blob Deduplication, Deletion & Garbage Collection (`cache.py`, `gc.py`)**
   - `PackageCache.delete(key: str) -> bool`:
     - Return `False` if missing.
     - If any other entry in the manifest currently lists `key` in `deps`, raise `CacheError` (refuse deleting an active dependency).
     - Remove `key` from manifest. Delete `<root>/blobs/<entry.digest>` **only if** no remaining entry in the manifest references `entry.digest`.
   - Same reference-counting rule applies during LRU eviction: never delete a blob file while another manifest entry still references that digest!
   - `PackageCache.pin(key: str)` / `unpin(key: str)`: raise `CacheKeyError` if `key` missing; toggle `pinned` and persist manifest.
   - `collect_garbage(root: Path, manifest: CacheManifest) -> list[str]`:
     - Delete any file in `<root>/blobs/` whose name is not in `referenced_digests` AND any stale file in `<root>/tmp/`.
     - Return sorted list of removed file paths (relative POSIX paths like `"blobs/<digest>"` or `"tmp/<name>"`).
     - Expose as `PackageCache.gc() -> list[str]`.
