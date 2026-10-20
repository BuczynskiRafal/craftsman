#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.gemini/config"
SKILLS_DIR="${CONFIG_DIR}/skills"
RULES_DIR="${CONFIG_DIR}/rules"
SCRIPTS_DIR="${CONFIG_DIR}/scripts"
PKG_TARGET_DIR="${CONFIG_DIR}/craftsman"
GIT_HOOKS_DIR="${CONFIG_DIR}/git-hooks"
PLUGINS_DIR="${CONFIG_DIR}/plugins"

STANDARD_PASSTHROUGH_GIT_HOOKS=(
  "applypatch-msg"
  "pre-applypatch"
  "post-applypatch"
  "pre-commit"
  "pre-merge-commit"
  "prepare-commit-msg"
  "post-commit"
  "pre-rebase"
  "post-checkout"
  "post-merge"
  "post-rewrite"
  "pre-receive"
  "update"
  "proc-receive"
  "post-receive"
  "post-update"
  "push-to-checkout"
)

MODE="copy"
INSTALL_ANTIGRAVITY=false
INSTALL_CLAUDE_USER=false
INSTALL_COPILOT_USER=false
COPILOT_WORKSPACE=""
AGENTS_WORKSPACE=""
INSTALL_GIT_HOOKS=false
DISABLE_GIT_HOOKS=false
EXPLICIT_TARGET=false

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Universal Multi-Platform Options:
  --auto                        (Default when no target flags given) Auto-detect installed IDEs/agents
                                (Antigravity, Claude Code, VS Code GitHub Copilot) + install Git hooks
  --all                         Install everywhere: Antigravity + Claude Code + GitHub Copilot + Git Hooks
  --copilot                     Export skills (*.prompt.md + ~/.copilot/skills) and rules (*.instructions.md)
                                to VS Code User profile for GitHub Copilot Chat
  --claude                      Export slash commands (*.md) and rules into ~/.claude/ (Claude Code)
  --antigravity                 Install skills, rules, scripts, and hooks.json into ~/.gemini/config/
  --copilot-workspace <path>    Export .github/prompts/*.prompt.md, .github/skills/*, and
                                .github/copilot-instructions.md into a target project repository
  --agents-workspace <path>     Export universal AGENTS.md (OpenAI Codex / Windsurf) and
                                .cursor/rules/craftsman.mdc (Cursor) into a target project repository
  --git-hooks                   Install OS-level Git hooks (commit-msg, pre-push, and local-hook forwarders)
                                and configure git config --global core.hooksPath
  --no-git-hooks                Skip global Git hooks when running in --auto mode

Google Antigravity Options:
  --copy                        Copy skills, rules, scripts, and merge hooks.json into ~/.gemini/config/
  --symlink                     Symlink skills, rules, and scripts (and merge hooks.json) into ~/.gemini/config/
  --plugin                      Install as a toggleable Antigravity plugin in ~/.gemini/config/plugins/craftsman
  -h, --help                    Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto)
      shift
      ;;
    --all)
      INSTALL_ANTIGRAVITY=true
      INSTALL_CLAUDE_USER=true
      INSTALL_COPILOT_USER=true
      INSTALL_GIT_HOOKS=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --antigravity|--copy)
      MODE="copy"
      INSTALL_ANTIGRAVITY=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --symlink)
      MODE="symlink"
      INSTALL_ANTIGRAVITY=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --plugin)
      MODE="plugin"
      INSTALL_ANTIGRAVITY=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --claude)
      INSTALL_CLAUDE_USER=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --copilot)
      INSTALL_COPILOT_USER=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --copilot-workspace)
      if [[ $# -lt 2 ]]; then
        echo "Error: --copilot-workspace requires a path argument" >&2
        exit 1
      fi
      COPILOT_WORKSPACE="$2"
      EXPLICIT_TARGET=true
      shift 2
      ;;
    --agents-workspace|--codex-workspace|--cursor-workspace)
      if [[ $# -lt 2 ]]; then
        echo "Error: $1 requires a path argument" >&2
        exit 1
      fi
      AGENTS_WORKSPACE="$2"
      EXPLICIT_TARGET=true
      shift 2
      ;;
    --git-hooks)
      INSTALL_GIT_HOOKS=true
      EXPLICIT_TARGET=true
      shift
      ;;
    --no-git-hooks)
      DISABLE_GIT_HOOKS=true
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

PYTHON_CMD=()
resolve_python_cmd() {
  local cand
  for cand in "python3" "python" "python3.13" "python3.12" "python3.11" "python3.10" "py -3" "py -3.13" "py -3.12" "py -3.11" "py -3.10"; do
    # shellcheck disable=SC2206
    local arr=(${cand})
    if "${arr[@]}" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
      PYTHON_CMD=("${arr[@]}")
      return 0
    fi
  done
  echo "[ERROR] Python >= 3.10 is required but was not found on PATH." >&2
  exit 1
}
resolve_python_cmd

detect_vscode_copilot() {
  if [[ -d "${HOME}/.copilot" || "${TERM_PROGRAM:-}" == "vscode" || -n "${VSCODE_PID:-}" || -n "${VSCODE_IPC_HOOK_CLI:-}" ]]; then
    return 0
  fi
  if [[ -d "${HOME}/AppData/Roaming/Code/User" || -d "${HOME}/Library/Application Support/Code/User" || -d "${XDG_CONFIG_HOME:-${HOME}/.config}/Code/User" ]]; then
    return 0
  fi
  if [[ -n "${APPDATA:-}" ]] && "${PYTHON_CMD[@]}" -c "import os, pathlib, sys; sys.exit(0 if (pathlib.Path(os.environ['APPDATA']) / 'Code' / 'User').is_dir() else 1)" >/dev/null 2>&1; then
    return 0
  fi
  if command -v code >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if [[ "${EXPLICIT_TARGET}" == false ]]; then
  echo "Auto-detecting installed AI coding platforms..."
  if [[ -d "${HOME}/.gemini/antigravity" || -d "${HOME}/.gemini/config" || -n "${AGY_MANAGED_ENV:-}" || -n "${ANTIGRAVITY_AGENT:-}" ]]; then
    INSTALL_ANTIGRAVITY=true
    echo "  * Detected: Google Antigravity"
  fi
  if [[ -d "${HOME}/.claude" || -n "${CLAUDECODE:-}" ]]; then
    INSTALL_CLAUDE_USER=true
    echo "  * Detected: Claude Code"
  fi
  if detect_vscode_copilot; then
    INSTALL_COPILOT_USER=true
    echo "  * Detected: GitHub Copilot (VS Code)"
  fi
  if [[ "${DISABLE_GIT_HOOKS}" == false ]]; then
    INSTALL_GIT_HOOKS=true
  fi
  if [[ "${INSTALL_ANTIGRAVITY}" == false && "${INSTALL_CLAUDE_USER}" == false && "${INSTALL_COPILOT_USER}" == false ]]; then
    echo "  ! No existing IDE directory detected; defaulting to GitHub Copilot user export."
    INSTALL_COPILOT_USER=true
  fi
fi

chmod +x "${REPO_DIR}/scripts/"*.py "${REPO_DIR}/git-hooks/"* 2>/dev/null || true

install_scripts_to() {
  local base_dir="$1"
  local target_scripts_dir="${base_dir}/scripts"
  local target_pkg_dir="${base_dir}/craftsman"
  mkdir -p "${base_dir}"
  local repo_resolved
  local base_resolved
  repo_resolved="$(cd "${REPO_DIR}" && pwd -P)"
  base_resolved="$(cd "${base_dir}" && pwd -P)"
  if [[ "${repo_resolved}" == "${base_resolved}" ]]; then
    chmod +x "${REPO_DIR}/scripts/"*.py 2>/dev/null || true
    return 0
  fi

  mkdir -p "${target_scripts_dir}"
  rm -rf "${target_pkg_dir}"
  if [[ "${MODE}" == "symlink" || "${MODE}" == "plugin" ]]; then
    ln -s "${REPO_DIR}/craftsman" "${target_pkg_dir}"
  else
    cp -R "${REPO_DIR}/craftsman" "${target_pkg_dir}"
  fi

  for script_path in "${REPO_DIR}/scripts/"*.py; do
    [[ -f "${script_path}" ]] || continue
    chmod +x "${script_path}"
    script_name="$(basename "${script_path}")"
    target="${target_scripts_dir}/${script_name}"
    rm -rf "${target}"
    if [[ "${MODE}" == "symlink" || "${MODE}" == "plugin" ]]; then
      ln -s "${script_path}" "${target}"
    else
      cp "${script_path}" "${target}"
      chmod +x "${target}"
    fi
  done
}

install_scripts() {
  install_scripts_to "${CONFIG_DIR}"
}

install_hooks_json() {
  local src_hooks="${REPO_DIR}/hooks.json"
  local target_hooks="${CONFIG_DIR}/hooks.json"
  [[ -f "${src_hooks}" ]] || return 0

  if [[ -L "${target_hooks}" ]]; then
    rm -f "${target_hooks}"
  fi

  if [[ -f "${target_hooks}" ]]; then
    "${PYTHON_CMD[@]}" - "${src_hooks}" "${target_hooks}" <<'PY'
import json, pathlib, shutil, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
src_data = json.loads(src.read_text(encoding="utf-8"))
try:
    dst_data = json.loads(dst.read_text(encoding="utf-8"))
    if not isinstance(dst_data, dict):
        raise ValueError("top-level JSON value must be an object")
except Exception as exc:
    bak = dst.with_suffix(".json.bak")
    shutil.copy2(dst, bak)
    sys.stderr.write(
        f"[ERROR] Failed to parse existing {dst} ({exc}); backed up to {bak} and aborted merge.\n"
    )
    sys.exit(1)

for key, val in src_data.items():
    existing_entry = dst_data.get(key)
    if isinstance(existing_entry, dict) and isinstance(val, dict) and isinstance(existing_entry.get("enabled"), bool):
        merged_entry = dict(val)
        merged_entry["enabled"] = existing_entry["enabled"]
        dst_data[key] = merged_entry
    else:
        dst_data[key] = val

dst.write_text(json.dumps(dst_data, indent=2) + "\n", encoding="utf-8")
PY
  else
    cp "${src_hooks}" "${target_hooks}"
  fi
  echo "  + hooks: ${target_hooks}"
}

write_passthrough_git_hook() {
  local hook_name="$1"
  local target="${GIT_HOOKS_DIR}/${hook_name}"
  cat > "${target}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

HOOKS_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd -P)"
PREV_RESOLVED=""
RUN_PREV=""
RUN_LOCAL=""

if [[ -f "\${HOOKS_DIR}/.previous_hooks_path" ]]; then
  PREV_HOOKS_DIR="\$(cat "\${HOOKS_DIR}/.previous_hooks_path")"
  [[ "\${PREV_HOOKS_DIR}" == "~" ]] && PREV_HOOKS_DIR="\${HOME}"
  PREV_HOOKS_DIR="\${PREV_HOOKS_DIR/#\~\//\${HOME}/}"
  if [[ -n "\${PREV_HOOKS_DIR}" && -x "\${PREV_HOOKS_DIR}/${hook_name}" ]]; then
    PREV_RESOLVED="\$(cd "\${PREV_HOOKS_DIR}" 2>/dev/null && pwd -P || true)"
    if [[ -n "\${PREV_RESOLVED}" && "\${PREV_RESOLVED}" != "\${HOOKS_DIR}" ]]; then
      RUN_PREV="\${PREV_RESOLVED}/${hook_name}"
    fi
  fi
fi

GIT_COMMON_DIR="\$(git rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -n "\${GIT_COMMON_DIR}" && -x "\${GIT_COMMON_DIR}/hooks/${hook_name}" ]]; then
  LOCAL_HOOK_DIR="\$(cd "\${GIT_COMMON_DIR}/hooks" 2>/dev/null && pwd -P || true)"
  if [[ -n "\${LOCAL_HOOK_DIR}" && "\${LOCAL_HOOK_DIR}" != "\${HOOKS_DIR}" && "\${LOCAL_HOOK_DIR}" != "\${PREV_RESOLVED}" ]]; then
    RUN_LOCAL="\${GIT_COMMON_DIR}/hooks/${hook_name}"
  fi
fi

if [[ -n "\${RUN_PREV}" && -n "\${RUN_LOCAL}" && ! -t 0 ]]; then
  STDIN_DATA="\$(cat)"
  printf "%s" "\${STDIN_DATA:+\${STDIN_DATA}\$'\n'}" | "\${RUN_PREV}" "\$@"
  printf "%s" "\${STDIN_DATA:+\${STDIN_DATA}\$'\n'}" | "\${RUN_LOCAL}" "\$@"
elif [[ -n "\${RUN_PREV}" && -n "\${RUN_LOCAL}" ]]; then
  "\${RUN_PREV}" "\$@"
  exec "\${RUN_LOCAL}" "\$@"
elif [[ -n "\${RUN_PREV}" ]]; then
  exec "\${RUN_PREV}" "\$@"
elif [[ -n "\${RUN_LOCAL}" ]]; then
  exec "\${RUN_LOCAL}" "\$@"
fi
EOF
  chmod +x "${target}"
}

if [[ "${INSTALL_ANTIGRAVITY}" == true ]]; then
  mkdir -p "${SKILLS_DIR}" "${RULES_DIR}" "${SCRIPTS_DIR}" "${PLUGINS_DIR}"

  if [[ "${MODE}" == "plugin" ]]; then
    install_scripts
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
    for script_path in "${REPO_DIR}/scripts/"*.py; do
      [[ -f "${script_path}" ]] && echo "  + script: $(basename "${script_path}")"
    done

    install_hooks_json

    echo ""
    echo "[OK] Craftsman Antigravity suite installed successfully!"
    echo "     Skills installed:  $(find "${REPO_DIR}/skills" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
    echo "     Rules installed:   $(find "${REPO_DIR}/rules" -mindepth 1 -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
    echo "     Scripts installed: $(find "${REPO_DIR}/scripts" -mindepth 1 -maxdepth 1 -name '*.py' | wc -l | tr -d ' ')"
  fi
fi

if [[ "${INSTALL_CLAUDE_USER}" == true ]]; then
  echo "Exporting Craftsman suite to Claude Code (~/.claude)..."
  "${PYTHON_CMD[@]}" "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --claude-dir
fi

if [[ "${INSTALL_COPILOT_USER}" == true ]]; then
  echo "Exporting Craftsman suite to GitHub Copilot (VS Code User Profile + Agent Skills)..."
  "${PYTHON_CMD[@]}" "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --user-dir
fi

if [[ -n "${COPILOT_WORKSPACE}" ]]; then
  echo "Exporting Craftsman suite to GitHub Copilot workspace: ${COPILOT_WORKSPACE}..."
  "${PYTHON_CMD[@]}" "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --workspace "${COPILOT_WORKSPACE}"
fi

if [[ -n "${AGENTS_WORKSPACE}" ]]; then
  echo "Exporting universal AGENTS.md and Cursor rules to workspace: ${AGENTS_WORKSPACE}..."
  "${PYTHON_CMD[@]}" "${REPO_DIR}/scripts/copilot-export.py" --repo-dir "${REPO_DIR}" --agents-workspace "${AGENTS_WORKSPACE}"
fi

if [[ "${INSTALL_GIT_HOOKS}" == true ]]; then
  if [[ "${INSTALL_ANTIGRAVITY}" == true || -d "${CONFIG_DIR}" || -d "${HOME}/.gemini/antigravity" ]]; then
    HOOKS_BASE_DIR="${CONFIG_DIR}"
  else
    HOOKS_BASE_DIR="${CRAFTSMAN_HOME:-${HOME}/.craftsman}"
  fi
  GIT_HOOKS_DIR="${HOOKS_BASE_DIR}/git-hooks"
  install_scripts_to "${HOOKS_BASE_DIR}"
  mkdir -p "${GIT_HOOKS_DIR}"
  printf "%s\n" "${PYTHON_CMD[*]}" > "${GIT_HOOKS_DIR}/.python_cmd"
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

  for passthrough_hook in "${STANDARD_PASSTHROUGH_GIT_HOOKS[@]}"; do
    write_passthrough_git_hook "${passthrough_hook}"
    echo "  + git-hook (passthrough): ${passthrough_hook}"
  done

  PREV_HOOKS_PATH="$(git config --global --get core.hooksPath 2>/dev/null || true)"
  PREV_HOOKS_EXPANDED="${PREV_HOOKS_PATH}"
  [[ "${PREV_HOOKS_EXPANDED}" == "~" ]] && PREV_HOOKS_EXPANDED="${HOME}"
  PREV_HOOKS_EXPANDED="${PREV_HOOKS_EXPANDED/#\~\//${HOME}/}"
  PREV_HOOKS_RESOLVED="$(cd "${PREV_HOOKS_EXPANDED}" 2>/dev/null && pwd -P || printf "%s" "${PREV_HOOKS_EXPANDED}")"
  GIT_HOOKS_RESOLVED="$(cd "${GIT_HOOKS_DIR}" 2>/dev/null && pwd -P || printf "%s" "${GIT_HOOKS_DIR}")"
  if [[ -n "${PREV_HOOKS_EXPANDED}" && "${PREV_HOOKS_RESOLVED}" != "${GIT_HOOKS_RESOLVED}" ]]; then
    printf "%s\n" "${PREV_HOOKS_EXPANDED}" > "${GIT_HOOKS_DIR}/.previous_hooks_path"
    echo "  ! backed up previous core.hooksPath (${PREV_HOOKS_PATH}) -> ${GIT_HOOKS_DIR}/.previous_hooks_path"
  fi
  git config --global core.hooksPath "${GIT_HOOKS_DIR}"
  echo "[OK] Configured git config --global core.hooksPath -> ${GIT_HOOKS_DIR}"
fi
