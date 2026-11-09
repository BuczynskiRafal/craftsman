"""Craftsman test suite.

Every test runs against a throwaway home directory and an empty global git configuration, so no
test can read or change the real `$HOME`, `~/.gitconfig` or the system git configuration.
"""

import atexit
import os
import shutil
import tempfile

from tests.ambient import AMBIENT_PLATFORM_ENV_VARS

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
# The platform markers hooks read to tell which agent runs them (the shared list, so that a test run started
# from inside an agent session sees none of them); tests set the ones they need.
for _variable in (*sorted(AMBIENT_PLATFORM_ENV_VARS), "XDG_CONFIG_HOME", "APPDATA"):
    os.environ.pop(_variable, None)
