#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.gemini/config"
SKILLS_DIR="${CONFIG_DIR}/skills"
RULES_DIR="${CONFIG_DIR}/rules"
SCRIPTS_DIR="${CONFIG_DIR}/scripts"
PLUGINS_DIR="${CONFIG_DIR}/plugins"

MODE="copy"

usage() {
  cat <<EOF
Usage: ./install.sh [--copy | --symlink | --plugin]

Options:
  --copy      (Default) Copy skills, rules, hooks.json, and scripts into ~/.gemini/config/
  --symlink   Symlink skills, rules, hooks.json, and scripts into ~/.gemini/config/ so git pull updates them live
  --plugin    Install the entire repository as a toggleable Antigravity plugin in ~/.gemini/config/plugins/craftsman
  -h, --help  Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy)
      MODE="copy"
      shift
      ;;
    --symlink)
      MODE="symlink"
      shift
      ;;
    --plugin)
      MODE="plugin"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

mkdir -p "${SKILLS_DIR}" "${RULES_DIR}" "${SCRIPTS_DIR}" "${PLUGINS_DIR}"

if [[ "${MODE}" == "plugin" ]]; then
  TARGET_PLUGIN="${PLUGINS_DIR}/craftsman"
  rm -rf "${TARGET_PLUGIN}"
  ln -s "${REPO_DIR}" "${TARGET_PLUGIN}"
  echo "[OK] Installed craftsman as an Antigravity plugin -> ${TARGET_PLUGIN}"
  exit 0
fi

echo "Installing Craftsman rules into ${RULES_DIR} (${MODE} mode)..."
for rule_path in "${REPO_DIR}/rules/"*.md; do
  rule_name="$(basename "${rule_path}")"
  target="${RULES_DIR}/${rule_name}"
  rm -rf "${target}"
  if [[ "${MODE}" == "symlink" ]]; then
    ln -s "${rule_path}" "${target}"
  else
    cp "${rule_path}" "${target}"
  fi
  echo "  + rule: ${rule_name}"
done

echo "Installing Craftsman skills into ${SKILLS_DIR} (${MODE} mode)..."
for skill_dir in "${REPO_DIR}/skills/"*; do
  [[ -d "${skill_dir}" ]] || continue
  skill_name="$(basename "${skill_dir}")"
  target="${SKILLS_DIR}/${skill_name}"
  rm -rf "${target}"
  if [[ "${MODE}" == "symlink" ]]; then
    ln -s "${skill_dir}" "${target}"
  else
    cp -R "${skill_dir}" "${target}"
  fi
  echo "  + skill: /${skill_name}"
done

echo "Installing Craftsman Hard-Loop scripts into ${SCRIPTS_DIR} (${MODE} mode)..."
for script_path in "${REPO_DIR}/scripts/"*; do
  [[ -f "${script_path}" ]] || continue
  chmod +x "${script_path}"
  script_name="$(basename "${script_path}")"
  target="${SCRIPTS_DIR}/${script_name}"
  rm -rf "${target}"
  if [[ "${MODE}" == "symlink" ]]; then
    ln -s "${script_path}" "${target}"
  else
    cp "${script_path}" "${target}"
    chmod +x "${target}"
  fi
  echo "  + script: ${script_name}"
done

if [[ -f "${REPO_DIR}/hooks.json" ]]; then
  TARGET_HOOKS="${CONFIG_DIR}/hooks.json"
  rm -rf "${TARGET_HOOKS}"
  if [[ "${MODE}" == "symlink" ]]; then
    ln -s "${REPO_DIR}/hooks.json" "${TARGET_HOOKS}"
  else
    cp "${REPO_DIR}/hooks.json" "${TARGET_HOOKS}"
  fi
  echo "  + hooks: ${TARGET_HOOKS}"
fi

echo ""
echo "[OK] Craftsman suite installed successfully!"
echo "     Skills installed:  $(find "${REPO_DIR}/skills" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
echo "     Rules installed:   $(find "${REPO_DIR}/rules" -mindepth 1 -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
echo "     Scripts installed: $(find "${REPO_DIR}/scripts" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' ')"
