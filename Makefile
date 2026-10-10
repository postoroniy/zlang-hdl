SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

VENV ?= $(CURDIR)/.venv
VENV := $(abspath $(VENV))
PYTHON := $(VENV)/bin/python
PYTHON_SCRIPTS := $(VENV)/bin
TMP_ROOT ?= $(CURDIR)/build/tmp
LOCAL_RUNNER := $(CURDIR)/tools/run_local_env.sh
RUN_PYTHON = ZLANG_VENV="$(VENV)" "$(LOCAL_RUNNER)" --purpose "$(1)" -- "$(PYTHON)"
CARGO_AUDIT ?= cargo-audit
CARGO_AUDIT_VERSION ?= 0.22.2
SETUPTOOLS_VERSION ?= 84.0.0
RUFF_VERSION ?= 0.12.12
PIP_AUDIT_VERSION ?= 2.10.1
REUSE_VERSION ?= 5.1.1
TWINE_VERSION ?= 6.2.0
RELEASE_PYTHON_TOOLS := \
	"pip-audit==$(PIP_AUDIT_VERSION)" \
	"reuse==$(REUSE_VERSION)" \
	"ruff==$(RUFF_VERSION)" \
	"twine==$(TWINE_VERSION)"
WORKERS ?= 16
BUILD_ROOT ?= build/local-release
DIST_DIR ?= $(BUILD_ROOT)/dist
TAG ?= v$(shell if test -x "$(PYTHON)"; then "$(PYTHON)" -c 'from zlang._version import __version__; print(__version__)'; else printf unknown; fi)
PREVIOUS_TAG ?= $(shell git describe --tags --abbrev=0 HEAD)
RELEASE_PREFLIGHT_REPORT ?= build/release-preflight.json
EDITOR_VSIX ?= build/editor-release/zlang-hdl-0.1.0.vsix
STRUCTURAL_PROFILE ?= small
STRUCTURAL_REPORT_DIR ?= build/structural

FAST_TEST_PATHS := \
	tests/parser tests/semantic tests/conformance tests/editor \
	tests/packaging tests/release

.PHONY: help venv env-check release-review-clean release-review release-regressions release-preflight public-check static jit-check jit-audit jit-advisory-audit native-release-set native-release-install audit release-tools test-fast test test-structural \
	structural-baseline \
	community-pdf-check test-release-twice editor-test editor-host-test package release-candidate

LOCAL_ENV_TARGETS := \
	release-regressions release-preflight public-check static jit-check jit-audit \
	jit-advisory-audit native-release-set native-release-install audit release-tools test-fast test \
	test-structural structural-baseline community-pdf-check test-release-twice \
	editor-test editor-host-test release-review release-candidate

$(LOCAL_ENV_TARGETS): env-check

help:
	@printf '%s\n' \
		'make release-review     validate a clean public review branch before merge' \
		'make release-regressions validate fix inclusion and permanent regression selectors' \
		'make release-preflight  bind version, tag, Git tree and release artifacts' \
		'make public-check       validate the public projection and release metadata' \
		'make static             run Ruff, compileall, and diff checks' \
		'make jit-check          run locked Rust format, Clippy, and unit gates' \
		'make jit-audit          audit locked native sources/licenses and optional wheel' \
		'make jit-advisory-audit audit locked native crates with pinned cargo-audit' \
		'make native-release-set require the audited Linux native wheel' \
		'make native-release-install install the audited release wheel into this worktree venv' \
		'make audit              run REUSE and Python dependency audits' \
		'make release-tools      validate the pinned external-tool inventory' \
		'make test-fast          run the compiler/editor/package CI subset' \
		'make test               run the complete parallel test suite' \
		'make test-structural    run reduced structural correctness/tool gates' \
		'make structural-baseline measure the selected structural profile' \
		'make community-pdf-check validate PDF and release-status identities' \
		'make test-release-twice run two zero-skip suites and validate both JUnit files' \
		'make editor-test        install locked editor dependencies and run its tests' \
		'make editor-host-test   build, audit and run the installed VSIX host smoke' \
		'make package            build one sdist and two byte-identical wheels' \
		'make release-candidate  run the local non-publishing release gate'

venv:
	mkdir -p "$(TMP_ROOT)"
	run_tmp="$$(mktemp -d "$(TMP_ROOT)/venv.XXXXXX")"
	trap 'rm -rf -- "$$run_tmp"' EXIT
	export TMPDIR="$$run_tmp" TMP="$$run_tmp" TEMP="$$run_tmp" PYTHONNOUSERSITE=1
	if [[ ! -x "$(PYTHON)" ]]; then
		if command -v uv >/dev/null; then
			uv venv --python 3.12 "$(VENV)"
		else
			python3 -c 'import sys; assert (3, 12) <= sys.version_info < (3, 13), sys.version'
			python3 -m venv "$(VENV)"
		fi
	fi
	# Install the exact build backend first: a newly created Python venv is not
	# guaranteed to include setuptools, while the editable install below avoids
	# a separate build-isolation environment.
	if command -v uv >/dev/null; then
		uv pip install --python "$(PYTHON)" "setuptools==$(SETUPTOOLS_VERSION)"
		uv pip install --python "$(PYTHON)" -e '.[test]' $(RELEASE_PYTHON_TOOLS)
	else
		"$(PYTHON)" -m pip install --disable-pip-version-check "setuptools==$(SETUPTOOLS_VERSION)"
		"$(PYTHON)" -m pip install --disable-pip-version-check --no-build-isolation -e '.[test]' $(RELEASE_PYTHON_TOOLS)
	fi

env-check:
	if [[ ! -x "$(PYTHON)" ]]; then
		echo "missing local virtual environment: run 'make venv' in $(CURDIR)" >&2
		exit 2
	fi
	if [[ -n "$${VIRTUAL_ENV:-}" ]] && [[ "$$(cd "$${VIRTUAL_ENV}" && pwd -P)" != "$$(cd "$(VENV)" && pwd -P)" ]]; then
		echo "active VIRTUAL_ENV is not this worktree: deactivate; make venv; source .venv/bin/activate" >&2
		exit 2
	fi
	"$(PYTHON)" tools/check_local_environment.py --root "$(CURDIR)" --venv "$(VENV)"

# This is deliberately usable on a reviewed public branch. Candidate mode
# additionally requires exact protected origin/main and therefore runs only
# after merge.
release-review-clean:
	if ! git diff --quiet || ! git diff --cached --quiet || \
		[[ -n "$$(git ls-files --others --exclude-standard)" ]]; then \
		echo 'release review requires a clean committed public checkout' >&2; \
		exit 2; \
	fi

release-review: release-review-clean release-regressions community-pdf-check public-check static test-release-twice

release-regressions:
	PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,release-regressions) tools/release_regressions.py \
		--root . \
		--release "$(patsubst v%,%,$(TAG))" \
		--previous-tag "$(PREVIOUS_TAG)"

release-preflight: release-regressions
	mkdir -p "$(dir $(RELEASE_PREFLIGHT_REPORT))"
	PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,release-preflight) tools/release_preflight.py \
		--root . \
		--tag "$(TAG)" \
		--previous-tag "$(PREVIOUS_TAG)" \
		--mode candidate \
		--require-clean \
		--output "$(RELEASE_PREFLIGHT_REPORT)"

public-check:
	mkdir -p "$(TMP_ROOT)"
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$(TMP_ROOT)/public-check.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,public-check) tools/public_tree.py export --source . --destination "$$public_root"
		PYTHONPATH="$$public_root" $(call RUN_PYTHON,public-check) tools/public_tree.py check-export --source "$$public_root" --config "$(CURDIR)/release/public-tree.toml"
		PYTHONPATH="$$public_root" $(call RUN_PYTHON,public-check) "$$public_root/tools/release_status.py" check --root "$$public_root" --tag "$(TAG)"
	else
		PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,public-check) tools/release_status.py check --root . --tag "$(TAG)"
	fi

static:
	$(call RUN_PYTHON,static-ruff) -m ruff check zlang tests tools
	$(call RUN_PYTHON,static-ruff-args) -m ruff check --select ARG001,ARG002 zlang
	$(call RUN_PYTHON,static-source-audit) tools/python_source_audit.py zlang
	$(call RUN_PYTHON,static-workflow-structure) tools/audit_workflow_structure.py --root .
	$(call RUN_PYTHON,static-workflow-env) tools/audit_workflow_local_env.py --root .
	$(call RUN_PYTHON,static-compileall) -m compileall -q zlang tests tools
	git diff --check
	git diff --cached --check

jit-check:
	if [[ -d native-runtime ]]; then
		cd native-runtime
		cargo fmt --all -- --check
		cargo test --locked
		cargo clippy --locked --all-targets -- -D warnings
	else
		$(call RUN_PYTHON,jit-check) -c 'import _zlang_native_sim as runtime; from zlang.simulation_plan import SIMULATION_RUNTIME_ABI as expected; assert runtime.runtime_abi() == expected'
	fi

jit-audit:
	if [[ -d native-runtime ]]; then
		arguments=(--root native-runtime)
		if [[ -n "$${ZLANG_NATIVE_RUNTIME_WHEEL:-}" ]]; then
			arguments+=(--wheel "$$ZLANG_NATIVE_RUNTIME_WHEEL")
		fi
		$(call RUN_PYTHON,jit-audit) -m tools.audit_native_runtime "$${arguments[@]}"
	else
		wheel="$${ZLANG_NATIVE_RUNTIME_WHEEL:-}"
		if [[ -z "$$wheel" ]]; then
			wheels=(release/native-wheels/*manylinux_2_28_x86_64.whl)
			test "$${#wheels[@]}" -eq 1 && test -f "$${wheels[0]}"
			wheel="$${wheels[0]}"
		fi
		version="$(TAG)"
		$(call RUN_PYTHON,jit-audit) tools/audit_native_binary.py --expected-version "$${version#v}" "$$wheel"
	fi

jit-advisory-audit:
	if [[ -d native-runtime ]]; then
		command -v "$(CARGO_AUDIT)" >/dev/null
		test "$$($(CARGO_AUDIT) --version)" = "cargo-audit $(CARGO_AUDIT_VERSION)"
		$(CARGO_AUDIT) audit --file native-runtime/Cargo.lock --deny warnings
	else
		echo 'native Cargo.lock is intentionally absent from the Community source tree'
	fi

native-release-set:
	if [[ ! -d release/native-wheels ]]; then
		echo 'release/native-wheels is missing: stage the audited Linux wheel' >&2
		exit 2
	fi
	mapfile -t wheels < <(find release/native-wheels -maxdepth 1 \
		-type f -name 'zlang_native_sim-*.whl' -print | sort)
	version="$(TAG)"
	$(call RUN_PYTHON,native-release-set) tools/audit_native_binary.py --expected-version "$${version#v}" \
		--require-release-platforms "$${wheels[@]}"

native-release-install: native-release-set
	mapfile -t wheels < <(find release/native-wheels -maxdepth 1 \
		-type f -name 'zlang_native_sim-*.whl' -print | sort)
	test "$${#wheels[@]}" -eq 1 && test -f "$${wheels[0]}"
	$(call RUN_PYTHON,native-release-install) -m pip install --disable-pip-version-check \
		--force-reinstall --no-deps "$${wheels[0]}"

audit:
	mkdir -p "$(TMP_ROOT)"
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$(TMP_ROOT)/public-audit.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		$(call RUN_PYTHON,audit) tools/public_tree.py export --source . --destination "$$public_root"
		$(call RUN_PYTHON,audit) -m reuse --root "$$public_root" lint
		$(call RUN_PYTHON,audit) -m pip_audit "$$public_root" --progress-spinner off
	else
		$(call RUN_PYTHON,audit) -m reuse --root . lint
		$(call RUN_PYTHON,audit) -m pip_audit . --progress-spinner off
	fi

release-tools:
	mkdir -p "$(TMP_ROOT)"
	export PATH="$(PYTHON_SCRIPTS):$$PATH"
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$(TMP_ROOT)/public-tools.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,release-tools) tools/public_tree.py export --source . --destination "$$public_root"
		PYTHONPATH="$$public_root" $(call RUN_PYTHON,release-tools) "$$public_root/tools/release_status.py" check \
			--root "$$public_root" --check-tools --tag "$(TAG)"
	else
		PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,release-tools) tools/release_status.py check --root . --check-tools --tag "$(TAG)"
	fi

test-fast:
	$(call RUN_PYTHON,test-fast) -m pytest -n "$(WORKERS)" --dist=loadscope -q $(FAST_TEST_PATHS)

test:
	$(call RUN_PYTHON,test) -m pytest -n "$(WORKERS)" --dist=loadscope -q

test-structural:
	$(call RUN_PYTHON,test-structural) -m pytest -q tests/structural/test_structural_witnesses.py

structural-baseline:
	mkdir -p "$(STRUCTURAL_REPORT_DIR)"
	$(call RUN_PYTHON,structural-baseline) tools/structural_synthesis_baseline.py \
		--profile "$(STRUCTURAL_PROFILE)" \
		--json "$(STRUCTURAL_REPORT_DIR)/$(STRUCTURAL_PROFILE).json" \
		--markdown "$(STRUCTURAL_REPORT_DIR)/$(STRUCTURAL_PROFILE).md"

community-pdf-check:
	PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,community-pdf) tools/release_status.py check --root . --tag "$(TAG)"

test-release-twice:
	mkdir -p "$(TMP_ROOT)"
	run_tmp="$$(mktemp -d "$(TMP_ROOT)/release-tests.XXXXXX")"
	public_root="$$(mktemp -d "$$run_tmp/public.XXXXXX")"
	trap 'rm -rf -- "$$run_tmp"' EXIT
	export TMPDIR="$$run_tmp" TMP="$$run_tmp" TEMP="$$run_tmp" PYTHONNOUSERSITE=1
	python_bin="$(PYTHON)"
	if [[ "$$python_bin" == */* ]]; then
		python_bin="$$(cd "$$(dirname "$$python_bin")" && pwd)/$$(basename "$$python_bin")"
	else
		python_bin="$$(command -v "$$python_bin")"
	fi
	export PATH="$$(dirname "$$python_bin"):$$PATH"
	report_root="$$(pwd)/build"
	mkdir -p "$$report_root"
	if [[ -f tools/public_tree.py ]]; then
		$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
	else
		git archive --format=tar HEAD | tar -xf - -C "$$public_root"
	fi
	mkdir -p "$$public_root/.native-test/wheels"
	if [[ -d native-runtime ]]; then
		maturin_bin="$$(dirname "$$python_bin")/maturin"
		test -x "$$maturin_bin"
		cd native-runtime
		CARGO_TARGET_DIR="$(CURDIR)/native-runtime/target" \
			"$$maturin_bin" build \
				--release --locked --offline --compatibility off \
				--out "$$public_root/.native-test/wheels"
	elif [[ -n "$${ZLANG_NATIVE_RUNTIME_WHEEL:-}" ]]; then
		cp -- "$$ZLANG_NATIVE_RUNTIME_WHEEL" "$$public_root/.native-test/wheels/"
	else
		wheels=("$$public_root"/release/native-wheels/*manylinux_2_28_x86_64.whl)
		test "$${#wheels[@]}" -eq 1 && test -f "$${wheels[0]}"
		cp -- "$${wheels[0]}" "$$public_root/.native-test/wheels/"
	fi
	# The exported source is a separate worktree.  Provision its own venv rather
	# than running its suite through this checkout's environment: the local
	# environment policy deliberately rejects sibling virtual environments.
	env -u VIRTUAL_ENV make -C "$$public_root" venv
	python_bin="$$public_root/.venv/bin/python"
	export PATH="$$public_root/.venv/bin:$$PATH"
	mapfile -t native_wheels < <(find "$$public_root/.native-test/wheels" \
		-maxdepth 1 -type f -name 'zlang_native_sim-*.whl' -print)
	test "$${#native_wheels[@]}" -eq 1
	"$$python_bin" "$$public_root/tools/materialize_native_test_extension.py" \
		"$${native_wheels[0]}" "$$public_root"
	cd "$$public_root"
	export PYTHONPATH="$$public_root"
	"$$python_bin" -m pytest -p tools.pytest_no_skips \
		-n "$(WORKERS)" --dist=loadscope -q --junitxml="$$report_root/release-1.xml"
	"$$python_bin" tools/release_status.py check \
		--root . --junit "$$report_root/release-1.xml"
	"$$python_bin" -m pytest -p tools.pytest_no_skips \
		-n "$(WORKERS)" --dist=loadscope -q --junitxml="$$report_root/release-2.xml"
	"$$python_bin" tools/release_status.py check \
		--root . --junit "$$report_root/release-2.xml"

editor-test:
	npm --prefix editors/vscode/zlang-hdl ci --ignore-scripts
	npm --prefix editors/vscode/zlang-hdl test

editor-host-test: editor-test
	if [[ -e "$(EDITOR_VSIX)" ]]; then
		echo "$(EDITOR_VSIX) already exists; choose a fresh EDITOR_VSIX" >&2
		exit 2
	fi
	mkdir -p "$(dir $(EDITOR_VSIX))"
	npm --prefix editors/vscode/zlang-hdl run package -- "$(abspath $(EDITOR_VSIX))"
	$(call RUN_PYTHON,editor-host) tests/editor/test_vscode_package.py "$(abspath $(EDITOR_VSIX))" \
		> "$(abspath $(EDITOR_VSIX)).audit.json"
	PYTHONPATH="$(CURDIR)" ZLANG_VENV="$(VENV)" "$(LOCAL_RUNNER)" \
		--purpose "editor-vsix-host" -- \
		xvfb-run -a npm --prefix editors/vscode/zlang-hdl run test:host -- \
		"$(abspath $(EDITOR_VSIX))"

package:
	if [[ -e "$(BUILD_ROOT)" ]]; then \
		echo "$(BUILD_ROOT) already exists; choose a fresh BUILD_ROOT" >&2; \
		exit 2; \
	fi
	if [[ ! -f tools/public_tree.py ]]; then
		if ! git diff --quiet || ! git diff --cached --quiet || \
			[[ -n "$$(git ls-files --others --exclude-standard)" ]]; then
			echo 'public package requires a clean committed checkout' >&2
			exit 2
		fi
	fi
	$(MAKE) --no-print-directory env-check
	mkdir -p "$(TMP_ROOT)"
	umask 022
	export SOURCE_DATE_EPOCH="$$(git log -1 --format=%ct)"
	python_bin="$(PYTHON)"
	if [[ "$$python_bin" == */* ]]; then
		python_dir="$$(cd "$$(dirname "$$python_bin")" && pwd)"
		export PATH="$$python_dir:$$PATH"
	fi
	source_root="$$(mktemp -d "$(TMP_ROOT)/release-source.XXXXXX")"
	trap 'rm -rf -- "$$source_root"' EXIT
	mkdir -p "$(BUILD_ROOT)/first" "$(BUILD_ROOT)/second" "$(DIST_DIR)"
	if [[ -f tools/public_tree.py ]]; then
		$(PYTHON) tools/public_tree.py export --source . --destination "$$source_root"
	else
		git archive --format=tar HEAD | tar -xf - -C "$$source_root"
	fi
	$(PYTHON) -m build "$$source_root" --no-isolation --sdist --wheel --outdir "$(BUILD_ROOT)/first"
	$(PYTHON) -m build "$$source_root" --no-isolation --wheel --outdir "$(BUILD_ROOT)/second"
	mapfile -t first_wheels < <(find "$(BUILD_ROOT)/first" -maxdepth 1 -type f -name '*.whl' -print | sort)
	mapfile -t second_wheels < <(find "$(BUILD_ROOT)/second" -maxdepth 1 -type f -name '*.whl' -print | sort)
	mapfile -t first_sdists < <(find "$(BUILD_ROOT)/first" -maxdepth 1 -type f -name '*.tar.gz' -print | sort)
	test "$${#first_wheels[@]}" -eq 1 && test "$${#second_wheels[@]}" -eq 1
	test "$${#first_sdists[@]}" -eq 1
	release_version="$(TAG)"
	if [[ "$$(basename "$${first_wheels[0]}")" != "zlang_hdl-$${release_version#v}-py3-none-any.whl" ]] || \
		[[ "$$(basename "$${first_sdists[0]}")" != "zlang_hdl-$${release_version#v}.tar.gz" ]]; then
		echo 'Python wheel/sdist version does not match the release tag' >&2
		exit 2
	fi
	cmp -- "$${first_wheels[0]}" "$${second_wheels[0]}"
	cp -- "$(BUILD_ROOT)"/first/* "$(DIST_DIR)/"
	if [[ -d native-runtime ]]; then
		mkdir -p "$(BUILD_ROOT)/native-first" "$(BUILD_ROOT)/native-second" \
			"$(BUILD_ROOT)/native-target"
		native-runtime/build_manylinux_wheel.sh native-runtime \
			"$(BUILD_ROOT)/native-first" "$(BUILD_ROOT)/native-target"
		native-runtime/build_manylinux_wheel.sh native-runtime \
			"$(BUILD_ROOT)/native-second" "$(BUILD_ROOT)/native-target"
		mapfile -t native_first < <(find "$(BUILD_ROOT)/native-first" \
			-maxdepth 1 -type f -name 'zlang_native_sim-*.whl' -print)
		mapfile -t native_second < <(find "$(BUILD_ROOT)/native-second" \
			-maxdepth 1 -type f -name 'zlang_native_sim-*.whl' -print)
		test "$${#native_first[@]}" -eq 1 && test "$${#native_second[@]}" -eq 1
		cmp -- "$${native_first[0]}" "$${native_second[0]}"
		$(PYTHON) -m tools.audit_native_runtime --root native-runtime \
			--wheel "$${native_first[0]}"
		cp -- "$${native_first[0]}" "$(DIST_DIR)/"
	else
		mapfile -t native_release_wheels < <(find release/native-wheels \
			-maxdepth 1 -type f -name 'zlang_native_sim-*.whl' -print | sort)
		version="$(TAG)"
		$(PYTHON) tools/audit_native_binary.py --expected-version "$${version#v}" \
			--require-release-platforms "$${native_release_wheels[@]}"
		cp -- "$${native_release_wheels[@]}" "$(DIST_DIR)/"
	fi
	$(PYTHON) -m twine check "$(DIST_DIR)"/*

# This target prepares and validates local candidate artifacts. It deliberately
# does not create commits/tags, upload artifacts, or publish a GitHub release.
release-candidate: release-preflight native-release-install community-pdf-check public-check static jit-check jit-audit jit-advisory-audit audit release-tools test-release-twice editor-host-test package
