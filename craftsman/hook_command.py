"""Build absolute Python + CLI hook command prefixes for user-level lifecycle hooks.

A hook runs with the guarded project as its working directory. `python -m craftsman.cli` puts that directory
first on `sys.path`, so a project's own `craftsman/` directory (or `json.py`) would run in place of Craftsman;
the hooks therefore run the bootstrap script `craftsman/craftsman-cli.py` by its absolute path, which puts the
directory that holds this package first instead. The same works for a virtual environment, `pip install --user`,
an editable install and a source checkout, with any Python 3.10+: the bootstrap finds the package next to
itself, not on the interpreter's path.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from craftsman import __version__
from craftsman.generated import ExportError

BOOTSTRAP = Path(__file__).resolve().parent / "craftsman-cli.py"
RUN_CHECK_TIMEOUT_S = 30
# How Craftsman before this bootstrap started its user-level hooks: an interpreter, then `-m craftsman.cli`.
_MODULE_FORM_RE = re.compile(r"\s-m\s+craftsman\.cli(?:\s|$)")
MODULE_FORM_PROBLEM = (
    "the hooks start Craftsman with `-m craftsman.cli`, which runs a project's own `craftsman/` "
    "directory in its place"
)


def _bash_quote(value: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", value) + '"'


def _powershell_quote(value: str) -> str:
    return '"' + re.sub(r'([`"$])', r"`\1", value) + '"'


def uses_module_form(command: str) -> bool:
    """Whether a hook command starts Craftsman as `python -m craftsman.cli`, as installs before this
    bootstrap wrote it; `craftsman install` replaces such a command."""
    return _MODULE_FORM_RE.search(command) is not None


def _check_runs(python: str) -> None:
    """Fail unless `python`, run outside this checkout, starts this copy of Craftsman through the bootstrap."""
    if not BOOTSTRAP.is_file():
        raise ExportError(f"{BOOTSTRAP} is missing; reinstall craftsman and install again")
    try:
        proc = subprocess.run(
            [python, str(BOOTSTRAP), "--version"],
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=RUN_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError(f"cannot run {python} for the hook commands: {exc}") from exc
    printed = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or printed != f"craftsman {__version__}":
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        reason = f": {detail[-1]}" if detail else ""
        raise ExportError(f"{python} cannot run craftsman {__version__} from {BOOTSTRAP}{reason}")


def absolute_hook_prefixes(python: str | None = None) -> tuple[str, str]:
    """(bash, PowerShell) command prefixes that run the bootstrap with an absolute interpreter path."""
    interpreter = python or sys.executable
    if not interpreter:
        raise ExportError("cannot determine the Python interpreter for the hook commands")
    _check_runs(interpreter)
    return (
        f"{_bash_quote(interpreter)} {_bash_quote(str(BOOTSTRAP))}",
        f"& {_powershell_quote(interpreter)} {_powershell_quote(str(BOOTSTRAP))}",
    )
