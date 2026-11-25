"""Hidden acceptance tests for pkgcache_sync."""

import tempfile
import unittest
from pathlib import Path

from pkgcache_sync import (
    CacheCapacityError,
    CacheCorruptionError,
    CacheError,
    CacheKeyError,
    PackageCache,
)


class HiddenPkgCacheTests(unittest.TestCase):
    def test_expected_sha256_mismatch_rejects_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=100)
            with self.assertRaises(CacheCorruptionError):
                cache.put("bad", b"payload", expected_sha256="0" * 64)
            self.assertEqual(cache.used_bytes(), 0)
            self.assertEqual(list(cache.blobs_dir.iterdir()), [])
            with self.assertRaises(CacheKeyError):
                cache.get("bad")

    def test_corrupted_or_missing_blob_on_disk_raises_on_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=1024)
            e = cache.put("pkg", b"valid-bytes")
            blob = cache.blobs_dir / e.digest
            blob.write_bytes(b"corrupt-bytes")
            with self.assertRaises(CacheCorruptionError):
                cache.get("pkg")

            blob.unlink()
            with self.assertRaises(CacheCorruptionError):
                cache.get("pkg")

    def test_shared_content_deduplicates_bytes_and_delete_preserves_live_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=200)
            data = b"A" * 80
            cache.put("k1", data)
            cache.put("k2", data)
            self.assertEqual(cache.used_bytes(), 80)

            self.assertTrue(cache.delete("k1"))
            self.assertEqual(cache.get("k2"), data)
            self.assertEqual(cache.used_bytes(), 80)

            self.assertTrue(cache.delete("k2"))
            self.assertEqual(cache.used_bytes(), 0)

    def test_lru_eviction_and_access_order_with_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = PackageCache(root, max_bytes=100)
            cache.put("a", b"1" * 40)
            cache.put("b", b"2" * 40)
            self.assertEqual(cache.get("a"), b"1" * 40)  # 'a' is now MRU, 'b' is LRU

            cache.put("c", b"3" * 40)  # must evict 'b' to fit 40 + 40 <= 100
            with self.assertRaises(CacheKeyError):
                cache.get("b")
            self.assertEqual(cache.get("a"), b"1" * 40)
            self.assertEqual(cache.get("c"), b"3" * 40)

            reloaded = PackageCache(root, max_bytes=100)
            self.assertEqual(reloaded.get("a"), b"1" * 40)

    def test_transitive_pinning_protects_dependencies_from_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=100)
            cache.put("lib-base", b"B" * 30)
            cache.put("lib-mid", b"M" * 30, deps=["lib-base"])
            cache.put("app", b"P" * 20, deps=["lib-mid"], pinned=True)
            # Protect chain: app -> lib-mid -> lib-base (80 bytes protected).
            cache.put("temp", b"T" * 20)
            # Touch nothing else; adding 'new' (20B) must evict 'temp' (unprotected, even though newer than lib-base!).
            cache.put("new", b"N" * 20)
            with self.assertRaises(CacheKeyError):
                cache.get("temp")
            self.assertEqual(cache.get("lib-base"), b"B" * 30)
            self.assertEqual(cache.get("lib-mid"), b"M" * 30)
            self.assertEqual(cache.get("app"), b"P" * 20)

    def test_capacity_overflow_rejects_without_evicting_existing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=100)
            cache.put("pin", b"P" * 60, pinned=True)
            cache.put("small", b"S" * 30)

            with self.assertRaises(CacheCapacityError):
                cache.put("too-big", b"X" * 50)

            # Even 'small' (unpinned) must NOT have been evicted by the failed put.
            self.assertEqual(cache.get("pin"), b"P" * 60)
            self.assertEqual(cache.get("small"), b"S" * 30)
            self.assertEqual(cache.used_bytes(), 90)

    def test_missing_or_self_dependency_and_delete_dependent_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=500)
            with self.assertRaises(CacheError):
                cache.put("a", b"abc", deps=["nonexistent"])
            with self.assertRaises(CacheError):
                cache.put("selfdep", b"abc", deps=["selfdep"])

            cache.put("base", b"123")
            cache.put("child", b"456", deps=["base"])
            with self.assertRaises(CacheError):
                cache.delete("base")
            self.assertTrue(cache.delete("child"))
            self.assertTrue(cache.delete("base"))

    def test_gc_cleans_unreferenced_blobs_and_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=500)
            e = cache.put("live", b"live-data")
            orphan_blob = cache.blobs_dir / ("a" * 64)
            orphan_blob.write_bytes(b"orphan")
            stale_tmp = cache.tmp_dir / "partial.staging"
            stale_tmp.write_bytes(b"incomplete")

            removed = cache.gc()
            self.assertEqual(sorted(removed), [f"blobs/{'a' * 64}", "tmp/partial.staging"])
            self.assertFalse(orphan_blob.exists())
            self.assertFalse(stale_tmp.exists())
            self.assertTrue((cache.blobs_dir / e.digest).is_file())
            self.assertEqual(cache.get("live"), b"live-data")

    def test_put_leaves_tmp_clean_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=500)
            cache.put("clean", b"content")
            self.assertEqual(list(cache.tmp_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
