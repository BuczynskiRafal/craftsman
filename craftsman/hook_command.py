"""Build absolute Python + CLI hook command prefixes for user-level lifecycle hooks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from craftsman.generated import ExportError

CLI_SHIM = Path("scripts") / "craftsman-cli.py"
IMPORT_CHECK_TIMEOUT_S = 30


def _bash_quote(value: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", value) + '"'


def _powershell_quote(value: str) -> str:
    return '"' + re.sub(r'([`"$])', r"`\1", value) + '"'


def cli_shim_for(python: str) -> Path | None:
    """The shim script `python` must run to reach this copy of craftsman, or None for `-m`.

    `-m craftsman.cli` works when `python` imports this very package on its own; a source checkout
    (or a different craftsman installed for that interpreter) needs the checkout's shim script.
    """
    package_dir = Path(__file__).resolve().parent
    if _imports_package(python, package_dir):
        return None
    shim = package_dir.parent / CLI_SHIM
    if not shim.is_file():
        raise ExportError(
            f"{python} cannot import craftsman from {package_dir} and {shim} is missing; "
            "install craftsman for that interpreter (pip install .) and export again"
        )
    return shim


def _imports_package(python: str, package_dir: Path) -> bool:
    """Whether `python`, run outside this checkout, imports the package in `package_dir`."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    try:
        proc = subprocess.run(
            [python, "-c", "import craftsman, sys; sys.stdout.write(craftsman.__file__)"],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=IMPORT_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError(f"cannot run {python} to check which craftsman it imports: {exc}") from exc
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve().parent == package_dir


def absolute_hook_prefixes(python: str | None = None) -> tuple[str, str]:
    """(bash, PowerShell) command prefixes that run craftsman with an absolute interpreter path."""
    interpreter = python or sys.executable
    if not interpreter:
        raise ExportError("cannot determine the Python interpreter for the hook commands")
    shim = cli_shim_for(interpreter)
    if shim is None:
        return (
            f"{_bash_quote(interpreter)} -m craftsman.cli",
            f"& {_powershell_quote(interpreter)} -m craftsman.cli",
        )
    return (
        f"{_bash_quote(interpreter)} {_bash_quote(str(shim))}",
        f"& {_powershell_quote(interpreter)} {_powershell_quote(str(shim))}",
    )
