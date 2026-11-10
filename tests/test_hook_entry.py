"""Every way Craftsman starts runs its own code, whatever the working directory (the guarded project) holds.

`python -m craftsman.cli` puts the working directory first on `sys.path`, so a project's own `craftsman/`
directory would run in place of the gate. Each entry point here runs a bootstrap script instead, in a working
directory that holds such a package: the project's code must not run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from craftsman import __version__
from craftsman.hook_command import absolute_hook_prefixes

from tests.support import REPO_ROOT

MARKER = "THE PROJECT'S OWN CRAFTSMAN RAN"
VERSION = f"craftsman {__version__}"
# What a module of the planted package does when it is imported or run.
PLANTED = f"import sys\nprint({MARKER!r})\nprint({MARKER!r}, file=sys.stderr)\n\n\ndef main(*args, **kwargs):\n    return 0\n"
WINDOWS = os.name == "nt"


class ShadowingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.project = self.tmp / "project"
        package = self.project / "craftsman"
        package.mkdir(parents=True)
        for name in ("__init__", "__main__", "cli", "hooks", "hook_fast", "scope", "antigravity"):
            (package / f"{name}.py").write_text(PLANTED, encoding="utf-8")
        (self.project / "json.py").write_text(PLANTED, encoding="utf-8")

    def env(self, pythonpath: str | None = None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        env["HOME"] = str(self.tmp)
        if pythonpath is not None:
            env["PYTHONPATH"] = pythonpath
        return env

    def run_in_project(
        self, argv: list[str], stdin: str = "", pythonpath: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, input=stdin, cwd=self.project, env=self.env(pythonpath), capture_output=True,
            encoding="utf-8", timeout=120, check=False,
        )  # fmt: skip

    def assert_own_code_ran(self, result: subprocess.CompletedProcess[str], stdout: str) -> None:
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, stdout), result.stderr)

    def pre_tool_payload(self) -> str:
        return json.dumps({"cwd": str(self.project), "tool_name": "Read", "tool_input": {}})


class TheFixtureTests(ShadowingTestCase):
    def test_the_module_form_runs_the_projects_package(self) -> None:
        # Why the entry points below exist: `-m` imports the project's `craftsman/` even with the real one
        # on PYTHONPATH.
        result = self.run_in_project(
            [sys.executable, "-B", "-m", "craftsman.cli", "--version"], pythonpath=str(REPO_ROOT)
        )
        self.assertIn(MARKER, result.stdout)


class UserInstallTests(ShadowingTestCase):
    """The hooks `craftsman install --claude` and `--copilot` write: an interpreter and the package's bootstrap."""

    @unittest.skipIf(WINDOWS, "runs the bash form of the hook command")
    def test_the_hook_command_runs_this_package(self) -> None:
        bash, _ = absolute_hook_prefixes()
        for args, stdin, stdout in (
            ("--version", "", VERSION),
            ("hook pre-tool --platform claude", self.pre_tool_payload(), "{}"),
            ("hook pre-tool --platform copilot", self.pre_tool_payload(), "{}"),
        ):
            for pythonpath in (None, f":{REPO_ROOT}"):  # an empty entry is the working directory
                with self.subTest(args=args, pythonpath=pythonpath):
                    if pythonpath is not None:
                        (self.project / "json.py").unlink(missing_ok=True)  # see the module docstring
                    result = self.run_in_project(["bash", "-c", f"{bash} {args}"], stdin, pythonpath)
                    self.assert_own_code_ran(result, stdout)

    def test_the_bootstrap_runs_the_package_next_to_it(self) -> None:
        # An installed copy, while another craftsman (this checkout) is on PYTHONPATH and the project's in the
        # working directory: the copy runs.
        site = self.tmp / "site-packages"
        shutil.copytree(REPO_ROOT / "craftsman", site / "craftsman", ignore=shutil.ignore_patterns("__pycache__"))
        init = site / "craftsman" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace(__version__, "0.0.0+copy"), encoding="utf-8")

        result = self.run_in_project(
            [sys.executable, "-B", str(site / "craftsman" / "craftsman-cli.py"), "--version"],
            pythonpath=f":{REPO_ROOT}",
        )

        self.assert_own_code_ran(result, "craftsman 0.0.0+copy")


class CheckoutScriptTests(ShadowingTestCase):
    """The source checkout's shim and the Antigravity hook scripts (`hooks.json` runs `./scripts/...`)."""

    def run_script(
        self, script: str, args: list[str], stdin: str, pythonpath: str | None
    ) -> subprocess.CompletedProcess[str]:
        return self.run_in_project([sys.executable, "-B", str(REPO_ROOT / script), *args], stdin, pythonpath)

    def test_each_script_runs_this_checkout(self) -> None:
        for script, args, stdin, stdout in (
            ("scripts/craftsman-cli.py", ["--version"], "", VERSION),
            ("scripts/craftsman-cli.py", ["hook", "pre-tool", "--platform", "claude"], self.pre_tool_payload(), "{}"),
            ("scripts/craftsman-scope-guard.py", [], "{}", '{"decision": "allow"}'),
            ("scripts/craftsman-stop-gate.py", [], "{}", '{"decision": "allow"}'),
            ("bin/craftsman-cli.py", ["--version"], "", VERSION),
        ):
            # The checkout on PYTHONPATH after an empty entry: the working directory comes before it.
            for pythonpath in (None, f":{REPO_ROOT}"):
                with self.subTest(script=script, args=args, pythonpath=pythonpath):
                    if pythonpath is not None:
                        (self.project / "json.py").unlink(missing_ok=True)
                    self.assert_own_code_ran(self.run_script(script, args, stdin, pythonpath), stdout)


@unittest.skipIf(WINDOWS, "a POSIX shell script")
class PluginLauncherTests(ShadowingTestCase):
    def test_the_launcher_runs_the_plugin(self) -> None:
        launcher = REPO_ROOT / "bin" / "launcher.sh"
        for args, stdin, stdout in (
            (["--version"], "", VERSION),
            (["hook", "pre-tool", "--platform", "claude"], self.pre_tool_payload(), "{}"),
        ):
            with self.subTest(args=args):
                self.assert_own_code_ran(self.run_in_project(["sh", str(launcher), *args], stdin), stdout)


if __name__ == "__main__":
    unittest.main()
