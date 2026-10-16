#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Bootstrap one checkout. Make owns pins; native-release-install owns the wheel.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
venv="${ZLANG_VENV:-$root/.venv}"
tmp_root="${ZLANG_TMP_ROOT:-$root/build/tmp}"
install_editor="${ZLANG_INSTALL_EDITOR:-1}"
vscode="${ZLANG_VSCODE:-code}"
make_command="${MAKE:-make}"
[[ "$venv" == /* ]] || venv="$root/$venv"
[[ "$tmp_root" == /* ]] || tmp_root="$root/$tmp_root"
cd "$root"

fail() { printf 'make venv: %s\n' "$*" >&2; exit 2; }

check_local_path() {
    local candidate="$1" ancestor="$1" physical
    [[ "$candidate" == "$root"/* ]] || fail "path must be below this worktree: $candidate"
    while [[ ! -e "$ancestor" && ! -L "$ancestor" ]]; do ancestor="${ancestor%/*}"; done
    [[ -d "$ancestor" ]] || fail "path is not a usable directory: $ancestor"
    physical="$(cd "$ancestor" && pwd -P)"
    [[ "$physical" == "$root" || "$physical" == "$root"/* ]] || \
        fail "path resolves outside this worktree: $candidate"
}

[[ "$install_editor" == 0 || "$install_editor" == 1 ]] || fail 'INSTALL_EDITOR must be 0 or 1'
check_local_path "$venv"
check_local_path "$tmp_root"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    active="$(cd "$VIRTUAL_ENV" 2>/dev/null && pwd -P)" || active=""
    local_env="$(cd "$venv" 2>/dev/null && pwd -P)" || local_env="$venv"
    [[ "$active" == "$local_env" ]] || \
        fail 'active VIRTUAL_ENV is not this worktree: deactivate; make venv; source .venv/bin/activate'
fi
[[ "$(uname -s):$(uname -m)" == Linux:x86_64 ]] || \
    fail 'this checkout requires Linux x86-64 for its reviewed native runtime wheel'

python="$venv/bin/python"
python_contract='import sys; sys.exit(0 if sys.implementation.name == "cpython" and (3, 12) <= sys.version_info < (3, 13) else "CPython 3.12 required; found " + sys.implementation.name + " " + sys.version.split()[0])'
if [[ -x "$python" ]] && ! "$python" -c "$python_contract"; then
    fail "existing $venv has an incompatible interpreter; preserve it elsewhere and rerun make venv"
fi

mkdir -p "$tmp_root"
run_tmp="$(mktemp -d "$tmp_root/venv.XXXXXX")"
cleanup() {
    local result=$?
    if [[ "${KEEP_TMP:-0}" == 1 ]]; then
        printf 'retained local temporary directory: %s\n' "$run_tmp" >&2
    else
        rm -rf -- "$run_tmp"
    fi
    exit "$result"
}
trap cleanup EXIT
export TMPDIR="$run_tmp" TMP="$run_tmp" TEMP="$run_tmp" PYTHONNOUSERSITE=1
if [[ ! -x "$python" ]]; then
    if command -v uv >/dev/null; then
        uv venv --python 3.12 "$venv"
    else
        host_python=""
        for candidate in python3.12 python3; do
            if command -v "$candidate" >/dev/null && "$candidate" -c "$python_contract" 2>/dev/null; then
                host_python="$candidate"
                break
            fi
        done
        [[ -n "$host_python" ]] || fail 'install uv or CPython 3.12 (python3.12 or python3), then rerun make venv'
        "$host_python" -m venv "$venv"
    fi
fi
"$python" -c "$python_contract" || fail 'the local environment does not provide CPython 3.12'
venv="$(cd "$venv" && pwd -P)"
python="$venv/bin/python"
export PATH="$venv/bin:$PATH" VIRTUAL_ENV="$venv"
"$python" -m tools.check_local_environment --root "$root" --venv "$venv" --bootstrap
"$python" -m tools.check_local_environment --root "$root" \
    --quarantine-stale-metadata --tmp-root "$tmp_root"

# Only the editable project skips build isolation; external tools retain it.
if command -v uv >/dev/null; then
    uv pip install --python "$python" "pip==${ZLANG_BOOTSTRAP_PIP_VERSION:?}" \
        "setuptools==${ZLANG_BOOTSTRAP_SETUPTOOLS_VERSION:?}"
    uv pip install --python "$python" --no-build-isolation -e '.[test]'
    uv pip install --python "$python" "$@"
else
    if ! "$python" -m pip --version >/dev/null 2>&1; then
        "$python" -m ensurepip --upgrade || \
            fail 'pip is unavailable; install CPython 3.12 venv/ensurepip support or uv, then rerun make venv'
    fi
    "$python" -m pip install --disable-pip-version-check --upgrade \
        "pip==${ZLANG_BOOTSTRAP_PIP_VERSION:?}" "setuptools==${ZLANG_BOOTSTRAP_SETUPTOOLS_VERSION:?}"
    "$python" -m pip install --disable-pip-version-check --no-build-isolation -e '.[test]'
    "$python" -m pip install --disable-pip-version-check "$@"
fi
version="$("$python" -c 'from zlang._version import __version__; print(__version__)')"
if ! "$make_command" --no-print-directory native-release-install \
    VENV="$venv" TMP_ROOT="$tmp_root" TAG="v$version"; then
    fail 'native runtime installation failed; inspect the wheel audit/import error above; the compiler and native runtime must have matching release versions'
fi
printf 'Compiler, native runtime and zlang-lsp installed in %s\n' "$venv"

if [[ "$install_editor" == 1 && -z "${CI:-}" ]] && command -v "$vscode" >/dev/null; then
    node_major=""
    if command -v node >/dev/null && command -v npm >/dev/null; then
        node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)" || node_major=""
    fi
    if [[ "$node_major" == 22 ]]; then
        if ! "$make_command" --no-print-directory editor-install VENV="$venv" VSCODE="$vscode"; then
            fail 'compiler/native/LSP installation succeeded, but VS Code installation failed; retry make editor-install, or use INSTALL_EDITOR=0 to leave the editor unchanged'
        fi
    else
        printf '%s\n' 'VS Code extension not changed: Node.js 22 and npm are required. Install them, then run make editor-install.' >&2
    fi
else
    printf '%s\n' 'VS Code extension not changed. Run make editor-install to install this checkout explicitly.'
fi
