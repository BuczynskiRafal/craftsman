"""Craftsman test suite.

Every test runs against a throwaway home directory and an empty global git configuration, so no
test can read or change the real `$HOME`, `~/.gitconfig` or the system git configuration.
"""

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="craftsman-tests-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ.update(
    {
        "HOME": _HOME,
        "USERPROFILE": _HOME,
        "GIT_CONFIG_GLOBAL": os.path.join(_HOME, ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
)
# The platform markers hooks read to tell which agent runs them; tests set the one they need.
for _variable in (
    "XDG_CONFIG_HOME",
    "APPDATA",
    "CRAFTSMAN_HOME",
    "CLAUDECODE",
    "VSCODE_PID",
    "TERM_PROGRAM",
    "AGY_MANAGED_ENV",
):
    os.environ.pop(_variable, None)
