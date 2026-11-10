#!/bin/sh
# Runs the Craftsman CLI with the first of python3, python and py that is Python 3.10+, in one
# interpreter start: craftsman-cli.py checks the version itself and exits, before it reads stdin, when
# the interpreter is older.
case $0 in
  /*) SCRIPT=$0 ;;
  *) SCRIPT=$PWD/$0 ;;
esac
BIN_DIR=${SCRIPT%/*}
VERSION_CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

for PYTHON_CMD in python3 python py; do
  command -v "$PYTHON_CMD" >/dev/null 2>&1 || continue
  PYTHONPATH="$BIN_DIR/.." "$PYTHON_CMD" "$BIN_DIR/craftsman-cli.py" "$@"
  STATUS=$?
  [ "$STATUS" -eq 0 ] && exit 0
  # A failure is the command's own unless the interpreter fails the version check (too old, or not a
  # working Python at all): only then is the next one tried. `craftsman run` passes on any exit code.
  if "$PYTHON_CMD" -c "$VERSION_CHECK" >/dev/null 2>&1; then
    exit "$STATUS"
  fi
done

echo "Craftsman gate inactive: Python 3.10+ not found" >&2
exit 0
