#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Sourced through BASH_ENV before every GitHub Actions run step.
set -euo pipefail

root="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
venv="$root/.venv"
if [[ ! -x "$venv/bin/python" ]]; then
    python3 -m venv "$venv"
fi
export PATH="$venv/bin:$PATH"
export PYTHONNOUSERSITE=1

mkdir -p "$root/build/tmp"
scratch="$(mktemp -d "$root/build/tmp/ci.XXXXXX")"
export TMPDIR="$scratch"
export TMP="$scratch"
export TEMP="$scratch"
trap 'rm -rf -- "$scratch"' EXIT
