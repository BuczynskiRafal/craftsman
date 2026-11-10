"""Run the Craftsman CLI when this interpreter is Python 3.10+; else exit with 97 before reading stdin.

`bin/launcher.sh` starts it with each candidate interpreter in turn, so the version check and the command
share one interpreter start. Any Python from 2.7 on can parse this file: nothing that needs 3.10 is
imported before the check. It has the name of the source checkout's shim, which the command policy reads
as the Craftsman CLI.
"""

import os
import sys

TOO_OLD = 97

if sys.version_info < (3, 10):
    sys.exit(TOO_OLD)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from craftsman.cli import main  # noqa: E402

raise SystemExit(main())
