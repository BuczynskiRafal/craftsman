"""Hidden acceptance tests for config_overlay."""

import copy
import unittest

from config_overlay import (
    ConfigCycleError,
    ConfigError,
    ConfigValidationError,
    deep_merge,
    interpolate_config,
    redact_secrets,
    resolve_profile,
    validate_schema,
)


class HiddenConfigOverlayTests(unittest.TestCase):
    def test_diamond_inheritance_and_no_extends_in_output(self) -> None:
        profiles = {
            "base": {"a": 1, "b": 1, "x": 1, "nested": {"k1": 1, "k2": 1}},
            "left": {"extends": "base", "b": 2, "x": 2, "nested": {"k2": 2}},
            "right": {"extends": "base", "x": 3},
            "prod": {"extends": ["left", "right"], "c": 4},
        }
        orig = copy.deepcopy(profiles)
        res = resolve_profile(profiles, "prod", resolve_interpolations=False)
        self.assertEqual(profiles, orig)  # input not mutated
        self.assertNotIn("extends", res)
        self.assertEqual(res["a"], 1)
        self.assertEqual(res["b"], 2)  # right must not reset left's override back to base
        self.assertEqual(res["x"], 3)  # right overrides left
        self.assertEqual(res["c"], 4)
        self.assertEqual(res["nested"], {"k1": 1, "k2": 2})

    def test_inheritance_cycle_detection_and_missing_parent(self) -> None:
        with self.assertRaises(ConfigCycleError):
            resolve_profile({"a": {"extends": "b"}, "b": {"extends": "a"}}, "a")
        with self.assertRaises(ConfigCycleError):
            resolve_profile({"self": {"extends": "self"}}, "self")
        with self.assertRaises(ConfigError):
            resolve_profile({"a": {"extends": "missing"}}, "a")

    def test_list_merge_directives_and_tombstone_unset(self) -> None:
        base = {"tags": ["core", "web"], "secret": "drop-me", "nested": {"keep": 1, "old": 2}}
        overlay = {
            "tags": {"$merge": "unique_append", "values": ["web", "metrics", "core", "api"]},
            "secret": {"$unset": True},
            "missing_unset": {"$unset": True},
            "nested": {"old": {"$unset": True}, "new": 3},
        }
        merged = deep_merge(base, overlay)
        self.assertEqual(merged["tags"], ["core", "web", "metrics", "api"])
        self.assertNotIn("secret", merged)
        self.assertNotIn("missing_unset", merged)
        self.assertEqual(merged["nested"], {"keep": 1, "new": 3})
        self.assertEqual(base["tags"], ["core", "web"])
        self.assertEqual(base["secret"], "drop-me")

        prep = deep_merge({"x": ["b", "c"]}, {"x": {"$merge": "prepend", "values": ["a"]}})
        self.assertEqual(prep["x"], ["a", "b", "c"])
        rep = deep_merge({"x": ["b", "c"]}, {"x": {"$merge": "replace", "values": ["z"]}})
        self.assertEqual(rep["x"], ["z"])

        with self.assertRaises(ConfigError):
            deep_merge({"x": "not-a-list"}, {"x": {"$merge": "append", "values": ["a"]}})
        with self.assertRaises(ConfigError):
            deep_merge({"x": []}, {"x": {"$merge": "unknown_mode", "values": ["a"]}})

    def test_escaped_placeholders_stay_literal_across_transitive_resolution(self) -> None:
        cfg = {
            "a": "${b}",
            "b": r"\${RAW_LITERAL}",
            "direct": r"prefix-\${env.FOO}-suffix",
            "empty_def": "${env.MISSING:-}",
        }
        res = interpolate_config(cfg, env={"RAW_LITERAL": "NOPE", "FOO": "NOPE", "MISSING": ""})
        self.assertEqual(res["b"], "${RAW_LITERAL}")
        self.assertEqual(res["a"], "${RAW_LITERAL}")
        self.assertEqual(res["direct"], "prefix-${env.FOO}-suffix")
        self.assertEqual(res["empty_def"], "")

    def test_transitive_interpolation_and_type_preservation(self) -> None:
        cfg = {
            "port": 9000,
            "flag": True,
            "ports": [80, "${port}"],
            "service": {
                "port": "${port}",
                "flag": "${flag}",
                "ports": "${ports}",
                "endpoint": "https://${host}:${port}/v1",
            },
            "host": "${env.SERVICE_HOST:-api.internal}",
        }
        res = interpolate_config(cfg, env={})
        self.assertIs(type(res["service"]["port"]), int)
        self.assertEqual(res["service"]["port"], 9000)
        self.assertIs(type(res["service"]["flag"]), bool)
        self.assertTrue(res["service"]["flag"])
        self.assertEqual(res["service"]["ports"], [80, 9000])
        self.assertEqual(res["service"]["endpoint"], "https://api.internal:9000/v1")

    def test_interpolation_cycle_detection(self) -> None:
        with self.assertRaises(ConfigCycleError):
            interpolate_config({"a": "${b}", "b": "${a}"})
        with self.assertRaises(ConfigCycleError):
            interpolate_config({"a": "${a}"})
        with self.assertRaises(ConfigError):
            interpolate_config({"a": "${missing.key}"})

    def test_nested_default_with_braces_or_fallback_expression(self) -> None:
        cfg = {
            "primary_host": "db.local",
            "host": "${env.DB_HOST:-${primary_host}}",
        }
        res = interpolate_config(cfg, env={})
        self.assertEqual(res["host"], "db.local")
        res2 = interpolate_config(cfg, env={"DB_HOST": "prod.db"})
        self.assertEqual(res2["host"], "prod.db")

    def test_schema_validation_collects_all_errors_nested_and_rejects_bool_as_int(self) -> None:
        schema = {
            "port": {"type": "int", "required": True, "min": 1, "max": 65535},
            "name": {"type": "str", "required": True, "min": 3},
            "db": {
                "type": "dict",
                "required": True,
                "fields": {
                    "pool": {"type": "int", "required": True, "min": 1},
                },
            },
            "hosts": {
                "type": "list",
                "min": 1,
                "items": {"type": "str", "min": 2},
            },
        }
        bad = {"port": True, "name": "x", "db": {"pool": 0}, "hosts": ["ok", "a"]}
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_schema(bad, schema)
        errs = ctx.exception.errors
        self.assertGreaterEqual(len(errs), 4)

        good = {"port": 8080, "name": "svc", "db": {"pool": 4}, "hosts": ["h1", "h2"]}
        validate_schema(good, schema)

    def test_redact_secrets_recursive_case_insensitive_non_mutating(self) -> None:
        cfg = {
            "API_KEY": "secret1",
            "db": {"password": "pw", "host": "localhost"},
            "webhooks": [{"url": "https://x", "token": "t1"}, {"url": "https://y"}],
            "nested": {"auth": {"clientSecret": "keep-unless-matched", "bearer_token": "b1"}},
        }
        snap = copy.deepcopy(cfg)
        redacted = redact_secrets(cfg, secret_keys=("password", "token", "api_key", "nested.auth.bearer_token"))
        self.assertEqual(cfg, snap)
        self.assertEqual(redacted["API_KEY"], "***")
        self.assertEqual(redacted["db"]["password"], "***")
        self.assertEqual(redacted["db"]["host"], "localhost")
        self.assertEqual(redacted["webhooks"][0]["token"], "***")
        self.assertEqual(redacted["nested"]["auth"]["bearer_token"], "***")
        self.assertEqual(redacted["nested"]["auth"]["clientSecret"], "keep-unless-matched")


if __name__ == "__main__":
    unittest.main()
