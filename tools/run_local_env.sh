#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run one command in this checkout's venv and collision-safe temporary directory.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
venv="${ZLANG_VENV:-$root/.venv}"
venv="$(cd "$venv" 2>/dev/null && pwd -P)" || {
    echo "local virtual environment does not exist: $venv" >&2
    exit 2
}
purpose="run"

if [[ "${1:-}" == "--purpose" ]]; then
    purpose="${2:?missing local run purpose}"
    shift 2
fi
[[ "${1:-}" == "--" ]] && shift
[[ $# -gt 0 ]] || { echo "usage: $0 [--purpose NAME] -- COMMAND [ARGS...]" >&2; exit 2; }
[[ "$purpose" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid local run purpose: $purpose" >&2; exit 2; }
[[ "$venv" == "$root"/* ]] || { echo "local virtual environment must be below this worktree: $root" >&2; exit 2; }
[[ -x "$venv/bin/python" ]] || { echo "missing local virtual environment: run 'make venv' in $root" >&2; exit 2; }
if [[ -n "${VIRTUAL_ENV:-}" ]] && [[ "$(cd "$VIRTUAL_ENV" && pwd -P)" != "$(cd "$venv" && pwd -P)" ]]; then
    echo "active VIRTUAL_ENV is not this worktree: deactivate; make venv; source .venv/bin/activate" >&2
    exit 2
fi

tmp_root="$root/build/tmp"
mkdir -p "$tmp_root"
run_tmp="$(mktemp -d "$tmp_root/$purpose.XXXXXX")"
cleanup() {
    local result=$?
    if [[ "${KEEP_TMP:-0}" == "1" ]]; then
        printf 'retained local temporary directory: %s\n' "$run_tmp" >&2
    else
        rm -rf -- "$run_tmp"
    fi
    exit "$result"
}
trap cleanup EXIT

export PATH="$venv/bin:$PATH"
export PYTHONNOUSERSITE=1
export TMPDIR="$run_tmp"
export TMP="$run_tmp"
export TEMP="$run_tmp"
"$@"
