"""Starter sanity tests for pkgcache_sync."""

import tempfile
import unittest
from pathlib import Path

from pkgcache_sync import PackageCache


class StarterPkgCacheTests(unittest.TestCase):
    def test_put_get_and_delete_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PackageCache(Path(tmp), max_bytes=1024)
            cache.put("pkg-a", b"hello-package")
            self.assertEqual(cache.get("pkg-a"), b"hello-package")
            self.assertTrue(cache.delete("pkg-a"))
            self.assertFalse(cache.delete("pkg-a"))


if __name__ == "__main__":
    unittest.main()
