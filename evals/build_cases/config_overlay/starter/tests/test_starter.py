"""Starter sanity tests for config_overlay."""

import unittest

from config_overlay import resolve_profile


class StarterConfigOverlayTests(unittest.TestCase):
    def test_single_profile_basic_merge_and_interpolation(self) -> None:
        profiles = {
            "dev": {
                "host": "localhost",
                "url": "http://${host}:8080",
                "env_tag": "${env.APP_ENV:-local}",
            }
        }
        resolved = resolve_profile(profiles, "dev", env={})
        self.assertEqual(resolved["url"], "http://localhost:8080")
        self.assertEqual(resolved["env_tag"], "local")


if __name__ == "__main__":
    unittest.main()
