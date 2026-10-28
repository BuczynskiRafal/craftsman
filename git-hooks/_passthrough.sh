#!/usr/bin/env bash
# Installed as every standard git hook and sourced by the guarded ones: runs the hooks Craftsman
# found before it, the core.hooksPath it replaced (.previous_hooks_path) and the repository's own
# .git/hooks, and gives the guarded hooks run_craftsman_guard.
set -euo pipefail

# Run as a hook, the folder is this script's; sourced, the guarded hook has set HOOKS_DIR already.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  HOOKS_DIR="$(cd "$(dirname "$0")" && pwd -P)"
fi

run_craftsman_guard() {
  local hook_name="$1"
  shift
  local guard_script="${HOOKS_DIR}/../scripts/git-guard.py"
  local python_cmd=""
  if [[ ! -f "${guard_script}" ]]; then
    echo "[ERROR] craftsman git hooks: ${guard_script} is missing; run 'craftsman install --git-hooks' to repair, or bypass once with git -c core.hooksPath= ${hook_name}" >&2
    return 1
  fi
  if [[ -f "${HOOKS_DIR}/.python_cmd" ]]; then
    IFS= read -r python_cmd < "${HOOKS_DIR}/.python_cmd" || true
    python_cmd="${python_cmd%$'\r'}"
  fi
  if [[ -z "${python_cmd}" || ! -x "${python_cmd}" ]]; then
    echo "[ERROR] craftsman git hooks: ${HOOKS_DIR}/.python_cmd names no runnable Python; run 'craftsman install --git-hooks' to repair" >&2
    return 1
  fi
  "${python_cmd}" "${guard_script}" --git-hook "${hook_name}" "$@"
}

chain_git_hook() {
  local hook_name="$1"
  shift
  local prev_dir=""
  local run_prev=""
  local run_local=""

  if [[ -f "${HOOKS_DIR}/.previous_hooks_path" ]]; then
    IFS= read -r prev_dir < "${HOOKS_DIR}/.previous_hooks_path" || true
    prev_dir="${prev_dir%$'\r'}"
    if [[ -n "${prev_dir}" && -x "${prev_dir}/${hook_name}" ]]; then
      prev_dir="$(cd "${prev_dir}" && pwd -P)"
      if [[ "${prev_dir}" != "${HOOKS_DIR}" ]]; then
        run_prev="${prev_dir}/${hook_name}"
      fi
    fi
  fi

  local git_common_dir
  git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  if [[ -n "${git_common_dir}" && -x "${git_common_dir}/hooks/${hook_name}" ]]; then
    local local_hook_dir
    local_hook_dir="$(cd "${git_common_dir}/hooks" && pwd -P)"
    if [[ "${local_hook_dir}" != "${HOOKS_DIR}" && "${local_hook_dir}" != "${prev_dir}" ]]; then
      run_local="${local_hook_dir}/${hook_name}"
    fi
  fi

  local needs_stdin=0
  case "${hook_name}" in
    pre-push|post-rewrite|pre-receive|update|post-receive|proc-receive)
      needs_stdin=1
      ;;
  esac

  if [[ -n "${run_prev}" && -n "${run_local}" && "${needs_stdin}" -eq 1 && ! -t 0 ]]; then
    local stdin_data
    stdin_data="$(cat)"
    printf "%s" "${stdin_data:+${stdin_data}$'\n'}" | "${run_prev}" "$@"
    printf "%s" "${stdin_data:+${stdin_data}$'\n'}" | "${run_local}" "$@"
  elif [[ -n "${run_prev}" && -n "${run_local}" ]]; then
    "${run_prev}" "$@"
    exec "${run_local}" "$@"
  elif [[ -n "${run_prev}" ]]; then
    exec "${run_prev}" "$@"
  elif [[ -n "${run_local}" ]]; then
    exec "${run_local}" "$@"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  chain_git_hook "$(basename "$0")" "$@"
fi
