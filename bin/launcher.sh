#!/bin/sh
# Runs the Craftsman CLI with the first of python3, python and py that is Python 3.10+, in one
# interpreter start: craftsman-cli.py checks the version itself and exits, before it reads stdin, when
# the interpreter is older.
# A path with a drive letter (C:/... or C:\...) or a UNC path is absolute too: Git Bash on Windows runs
# "${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" in that form.
case $0 in
  /*|[A-Za-z]:[\\/]*|\\\\*) SCRIPT=$0 ;;
  *) SCRIPT=$PWD/$0 ;;
esac
BIN_DIR=${SCRIPT%[\\/]*}
VERSION_CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

# The interpreter cannot replace this shell (`exec`): a failure that is the interpreter's own sends the
# launcher on to the next one. It runs as a child instead, and a platform that stops a hook that ran too
# long (SIGTERM, SIGINT or SIGHUP to this shell) stops the child too, so no hook outlives its time. SIGKILL
# cannot be passed on. An asynchronous child reads /dev/null unless given stdin explicitly (fd 3), and
# ignores SIGINT, so it gets SIGTERM whatever this shell received.
CHILD=
trap '[ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null; exit 143' TERM
trap '[ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null; exit 130' INT
trap '[ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null; exit 129' HUP
if command true 2>/dev/null 3<&0; then exec 3<&0; else exec 3</dev/null; fi

for PYTHON_CMD in python3 python py; do
  command -v "$PYTHON_CMD" >/dev/null 2>&1 || continue
  PYTHONPATH="$BIN_DIR/.." "$PYTHON_CMD" "$BIN_DIR/craftsman-cli.py" "$@" <&3 3<&- &
  CHILD=$!
  wait "$CHILD"
  STATUS=$?
  CHILD=
  [ "$STATUS" -eq 0 ] && exit 0
  # A failure is the command's own unless the interpreter fails the version check (too old, or not a
  # working Python at all): only then is the next one tried. `craftsman run` passes on any exit code.
  if "$PYTHON_CMD" -c "$VERSION_CHECK" >/dev/null 2>&1; then
    exit "$STATUS"
  fi
done

echo "Craftsman gate inactive: Python 3.10+ not found" >&2
exit 0
