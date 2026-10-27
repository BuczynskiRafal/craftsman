#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=()
check_python() {
  "$@" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1
}

if command -v python3 >/dev/null 2>&1 && check_python python3; then
  PY=(python3)
elif command -v python >/dev/null 2>&1 && check_python python; then
  PY=(python)
elif command -v py >/dev/null 2>&1 && check_python py -3; then
  PY=(py -3)
else
  echo "[ERROR] Python >= 3.10 is required but was not found on PATH." >&2
  exit 1
fi

if [[ -n "${HOME:-}" ]]; then
  export USERPROFILE="${HOME}"
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${REPO_DIR}:${PYTHONPATH}"
else
  export PYTHONPATH="${REPO_DIR}"
fi

exec "${PY[@]}" -m craftsman.cli install --repo-dir "${REPO_DIR}" "$@"
