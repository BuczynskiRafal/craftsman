#!/usr/bin/env bash
set -euo pipefail

# Target directory defaults to /tmp/craftsman-demo or $1
TARGET_DIR="${1:-/tmp/craftsman-demo}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Setting up Craftsman demo repository in: ${TARGET_DIR}"

# Ensure target directory is outside the Craftsman worktree to avoid nested repos
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REAL_TARGET="$(python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "${TARGET_DIR}")"
REAL_REPO="$(python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "${REPO_ROOT}")"

if [[ "${REAL_TARGET}" == "${REAL_REPO}" || "${REAL_TARGET}" == "${REAL_REPO}/"* ]]; then
  echo "Error: Target directory must be outside the craftsman repository worktree to prevent nested git repos." >&2
  exit 1
fi

# Only a directory this script created earlier (it holds the marker) is ever deleted.
MARKER=".craftsman-demo"
if [[ -e "${TARGET_DIR}" ]]; then
  if [[ -f "${TARGET_DIR}/${MARKER}" ]]; then
    rm -rf "${TARGET_DIR}"
  elif [[ -d "${TARGET_DIR}" && -z "$(ls -A "${TARGET_DIR}")" ]]; then
    rmdir "${TARGET_DIR}"
  else
    echo "Error: ${TARGET_DIR} already exists and was not created by this script; pick a new path." >&2
    exit 1
  fi
fi
mkdir -p "${TARGET_DIR}"
touch "${TARGET_DIR}/${MARKER}"

# Copy demo project files
cp -r "${SCRIPT_DIR}/shop" "${TARGET_DIR}/"
cp -r "${SCRIPT_DIR}/tests" "${TARGET_DIR}/"
cp "${SCRIPT_DIR}/README.md" "${TARGET_DIR}/"
cp "${SCRIPT_DIR}/.gitignore" "${TARGET_DIR}/"

# Initialize a clean git repository
cd "${TARGET_DIR}"
git init -b main
git config user.name "${GIT_AUTHOR_NAME:-Demo User}"
git config user.email "${GIT_AUTHOR_EMAIL:-demo@example.com}"

git add .
git commit -m "feat(catalog): initial product catalog implementation"

# Install Craftsman workspace configuration if craftsman CLI is available
if command -v craftsman >/dev/null 2>&1; then
  craftsman install --agents-workspace .
  git add .
  git commit -m "chore: configure craftsman workspace"
  echo "Craftsman workspace configuration installed."
else
  echo "Note: 'craftsman' CLI not found on PATH. Run 'pip install craftsman-gate' or 'pip install -e .' first."
fi

echo ""
echo "=== Demo Repository Ready ==="
echo "Location: ${TARGET_DIR}"
echo ""
echo "Try running:"
echo "  cd ${TARGET_DIR}"
echo "  python3 -m unittest discover -s tests"
echo ""
echo "Then prompt your AI agent:"
echo "  /craftsman Add 1-based pagination to catalog search results (page number and optional page size)"
echo ""
