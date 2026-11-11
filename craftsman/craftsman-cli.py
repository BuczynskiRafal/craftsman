"""Run the Craftsman CLI of the package this file is in, whatever the working directory contains.

The user-level hooks `craftsman install` writes run `"<python>" "<this file>" hook ...` (see
`craftsman.hook_command`). `python -m craftsman.cli` would put the hook's working directory, the guarded
project, first on `sys.path`, so a project's own `craftsman/` directory would run instead of this package; a
script puts its own directory there instead, which this one replaces with the directory that holds the
package. It has the name of the other bootstraps, which the command policy reads as the Craftsman CLI.
"""

import os
import sys

_PACKAGE = os.path.dirname(os.path.realpath(__file__))
sys.path[:] = [os.path.dirname(_PACKAGE), *(entry for entry in sys.path if entry != _PACKAGE)]

from craftsman.cli import main  # noqa: E402

raise SystemExit(main())
