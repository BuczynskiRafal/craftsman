#!/usr/bin/env bash
set -euo pipefail

HOOKS_DIR="${HOOKS_DIR:-$(cd "$(dirname "$0")" && pwd -P)}"

run_craftsman_guard() {
  local hook_name="$1"
  shift
  local guard_script="${HOOKS_DIR}/../scripts/git-guard.py"
  [[ -f "${guard_script}" ]] || return 0

  local py_cmd=()
  if [[ -f "${HOOKS_DIR}/.python_cmd" ]]; then
    local py_line=""
    IFS= read -r py_line < "${HOOKS_DIR}/.python_cmd" || true
    py_line="${py_line%$'\r'}"
    if [[ -n "${py_line}" && -x "${py_line}" ]]; then
      py_cmd=("${py_line}")
    elif [[ -n "${py_line}" ]]; then
      read -r -a py_cmd <<< "${py_line}" || true
    fi
  fi
  if [[ ${#py_cmd[@]} -gt 0 ]] && command -v "${py_cmd[0]}" >/dev/null 2>&1; then
    "${py_cmd[@]}" "${guard_script}" --git-hook "${hook_name}" "$@"
  elif command -v python3 >/dev/null 2>&1; then
    python3 "${guard_script}" --git-hook "${hook_name}" "$@"
  elif command -v python >/dev/null 2>&1; then
    python "${guard_script}" --git-hook "${hook_name}" "$@"
  else
    echo "[ERROR] craftsman git-guard: no Python interpreter found to run ${guard_script}" >&2
    return 1
  fi
}

chain_git_hook() {
  local hook_name="$1"
  shift
  local prev_resolved=""
  local run_prev=""
  local run_local=""

  if [[ -f "${HOOKS_DIR}/.previous_hooks_path" ]]; then
    local prev_hooks_dir=""
    IFS= read -r prev_hooks_dir < "${HOOKS_DIR}/.previous_hooks_path" || true
    prev_hooks_dir="${prev_hooks_dir%$'\r'}"
    local tilde="~"
    if [[ "${prev_hooks_dir}" == "${tilde}" ]]; then
      prev_hooks_dir="${HOME}"
    elif [[ "${prev_hooks_dir}" == "${tilde}/"* ]]; then
      prev_hooks_dir="${HOME}/${prev_hooks_dir#"${tilde}/"}"
    fi
    if [[ -n "${prev_hooks_dir}" && -x "${prev_hooks_dir}/${hook_name}" ]]; then
      prev_resolved="$(cd "${prev_hooks_dir}" 2>/dev/null && pwd -P || true)"
      if [[ -n "${prev_resolved}" && "${prev_resolved}" != "${HOOKS_DIR}" ]]; then
        run_prev="${prev_resolved}/${hook_name}"
      fi
    fi
  fi

  local git_common_dir
  git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  if [[ -n "${git_common_dir}" && -x "${git_common_dir}/hooks/${hook_name}" ]]; then
    local local_hook_dir
    local_hook_dir="$(cd "${git_common_dir}/hooks" 2>/dev/null && pwd -P || true)"
    if [[ -n "${local_hook_dir}" && "${local_hook_dir}" != "${HOOKS_DIR}" && "${local_hook_dir}" != "${prev_resolved}" ]]; then
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
