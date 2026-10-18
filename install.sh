#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.gemini/config"
SKILLS_DIR="${CONFIG_DIR}/skills"
RULES_DIR="${CONFIG_DIR}/rules"
SCRIPTS_DIR="${CONFIG_DIR}/scripts"
GIT_HOOKS_DIR="${CONFIG_DIR}/git-hooks"
PLUGINS_DIR="${CONFIG_DIR}/plugins"

MODE="copy"
INSTALL_ANTIGRAVITY=true
INSTALL_CLAUDE_USER=false
INSTALL_COPILOT_USER=false
COPILOT_WORKSPACE=""
AGENTS_WORKSPACE=""
INSTALL_GIT_HOOKS=false

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Universal Multi-Platform Options:
  --all                         Install everywhere: Antigravity + Claude Code + GitHub Copilot + Git Hooks
  --claude                      Export 17 slash commands (*.md) and rules into ~/.claude/ (Claude Code)
  --copilot                     Export 17 skills (*.prompt.md) and rules (*.instructions.md)
                                to VS Code User profile (Code/User/prompts) for GitHub Copilot Chat
  --copilot-workspace <path>    Export .github/prompts/*.prompt.md and .github/copilot-instructions.md
                                into a target project repository
  --agents-workspace <path>     Export universal AGENTS.md (OpenAI Codex / Windsurf) and
                                .cursor/rules/craftsman.mdc (Cursor) into a target project repository
  --git-hooks                   Install OS-level Git hooks (commit-msg & pre-push) into
                                ~/.gemini/config/git-hooks and set git config --global core.hooksPath

Google Antigravity Options:
  --copy                        (Default) Copy skills, rules, hooks.json, and scripts into ~/.gemini/config/
  --symlink                     Symlink skills, rules, hooks.json, and scripts into ~/.gemini/config/
  --plugin                      Install as a toggleable Antigravity plugin in ~/.gemini/config/plugins/craftsman
  -h, --help                    Show this help message
EOF
}

if [[ $# -gt 0 ]]; then
  INSTALL_ANTIGRAVITY=false
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      INSTALL_ANTIGRAVITY=true
      INSTALL_CLAUDE_USER=true
      INSTALL_COPILOT_USER=true
      INSTALL_GIT_HOOKS=true
      shift
      ;;
    --copy)
      MODE="copy"
      INSTALL_ANTIGRAVITY=true
      shift
      ;;
    --symlink)
      MODE="symlink"
      INSTALL_ANTIGRAVITY=true
      shift
      ;;
    --plugin)
      MODE="plugin"
      INSTALL_ANTIGRAVITY=true
      shift
      ;;
    --claude)
      INSTALL_CLAUDE_USER=true
      shift
      ;;
    --copilot)
      INSTALL_COPILOT_USER=true
      shift
      ;;
    --copilot-workspace)
      if [[ $# -lt 2 ]]; then
        echo "Error: --copilot-workspace requires a path argument" >&2
        exit 1
      fi
      COPILOT_WORKSPACE="$2"
      shift 2
      ;;
    --agents-workspace|--codex-workspace|--cursor-workspace)
      if [[ $# -lt 2 ]]; then
        echo "Error: $1 requires a path argument" >&2
        exit 1
      fi
      AGENTS_WORKSPACE="$2"
      shift 2
      ;;
    --git-hooks)
      INSTALL_GIT_HOOKS=true
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

chmod +x "${REPO_DIR}/scripts/"*.py "${REPO_DIR}/git-hooks/"* 2>/dev/null || true

install_scripts() {
  mkdir -p "${SCRIPTS_DIR}"
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
  done
}

if [[ "${INSTALL_ANTIGRAVITY}" == true ]]; then
  mkdir -p "${SKILLS_DIR}" "${RULES_DIR}" "${SCRIPTS_DIR}" "${PLUGINS_DIR}"

  if [[ "${MODE}" == "plugin" ]]; then
    TARGET_PLUGIN="${PLUGINS_DIR}/craftsman"
    rm -rf "${TARGET_PLUGIN}"
    ln -s "${REPO_DIR}" "${TARGET_PLUGIN}"
    echo "[OK] Installed craftsman as an Antigravity plugin -> ${TARGET_PLUGIN}"
  else
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
    install_scripts
    for script_path in "${REPO_DIR}/scripts/"*; do
      [[ -f "${script_path}" ]] && echo "  + script: $(basename "${script_path}")"
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
    echo "[OK] Craftsman Antigravity suite installed successfully!"
    echo "     Skills installed:  $(find "${REPO_DIR}/skills" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
    echo "     Rules installed:   $(find "${REPO_DIR}/rules" -mindepth 1 -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
    echo "     Scripts installed: $(find "${REPO_DIR}/scripts" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' ')"
  fi
fi

if [[ "${INSTALL_CLAUDE_USER}" == true ]]; then
  echo "Exporting Craftsman suite to Claude Code (~/.claude)..."
  python3 "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --claude-dir
fi

if [[ "${INSTALL_COPILOT_USER}" == true ]]; then
  echo "Exporting Craftsman suite to GitHub Copilot (VS Code User Profile)..."
  python3 "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --user-dir
fi

if [[ -n "${COPILOT_WORKSPACE}" ]]; then
  echo "Exporting Craftsman suite to GitHub Copilot workspace: ${COPILOT_WORKSPACE}..."
  python3 "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --workspace "${COPILOT_WORKSPACE}"
fi

if [[ -n "${AGENTS_WORKSPACE}" ]]; then
  echo "Exporting universal AGENTS.md and Cursor rules to workspace: ${AGENTS_WORKSPACE}..."
  python3 "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --agents-workspace "${AGENTS_WORKSPACE}"
fi

if [[ "${INSTALL_GIT_HOOKS}" == true ]]; then
  install_scripts
  mkdir -p "${GIT_HOOKS_DIR}"
  echo "Installing global Git hooks into ${GIT_HOOKS_DIR}..."
  for hook_file in "${REPO_DIR}/git-hooks/"*; do
    [[ -f "${hook_file}" ]] || continue
    chmod +x "${hook_file}"
    hook_name="$(basename "${hook_file}")"
    target="${GIT_HOOKS_DIR}/${hook_name}"
    rm -rf "${target}"
    if [[ "${MODE}" == "symlink" ]]; then
      ln -s "${hook_file}" "${target}"
    else
      cp "${hook_file}" "${target}"
      chmod +x "${target}"
    fi
    echo "  + git-hook: ${hook_name}"
  done
  git config --global core.hooksPath "${GIT_HOOKS_DIR}"
  echo "[OK] Configured git config --global core.hooksPath -> ${GIT_HOOKS_DIR}"
fi
