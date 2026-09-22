SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)
CARGO_AUDIT ?= cargo-audit
CARGO_AUDIT_VERSION ?= 0.22.2
WORKERS ?= 16
BUILD_ROOT ?= build/local-release
DIST_DIR ?= $(BUILD_ROOT)/dist
TAG ?= v$(shell $(PYTHON) -c 'from zlang._version import __version__; print(__version__)')
STRUCTURAL_PROFILE ?= small
STRUCTURAL_REPORT_DIR ?= build/structural
COMMUNITY_PDF_COVER ?= $(HOME)/Downloads/ZLang-HDL_cover.jpg

FAST_TEST_PATHS := \
	tests/parser tests/semantic tests/conformance tests/editor \
	tests/packaging tests/release

.PHONY: help public-check static jit-check jit-audit jit-advisory-audit native-release-set audit release-tools test-fast test test-structural \
	structural-baseline \
	community-pdf community-pdf-check test-release-twice editor-test package release-candidate

help:
	@printf '%s\n' \
		'make public-check       validate the public projection and release metadata' \
		'make static             run Ruff, compileall, and diff checks' \
		'make jit-check          run locked Rust format, Clippy, and unit gates' \
		'make jit-audit          audit locked native sources/licenses and optional wheel' \
		'make jit-advisory-audit audit locked native crates with pinned cargo-audit' \
		'make native-release-set require the audited Linux native wheel' \
		'make audit              run REUSE and Python dependency audits' \
		'make release-tools      validate the pinned external-tool inventory' \
		'make test-fast          run the compiler/editor/package CI subset' \
		'make test               run the complete parallel test suite' \
		'make test-structural    run reduced structural correctness/tool gates' \
		'make structural-baseline measure the selected structural profile' \
		'make community-pdf      rebuild the highlighted XeLaTeX language reference' \
		'make community-pdf-check validate PDF and release-status identities' \
		'make test-release-twice run two zero-skip suites and validate both JUnit files' \
		'make editor-test        install locked editor dependencies and run its tests' \
		'make package            build one sdist and two byte-identical wheels' \
		'make release-candidate  run the local non-publishing release gate'

public-check:
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-check.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
		$(PYTHON) tools/public_tree.py check-export --source "$$public_root" --config "$(CURDIR)/release/public-tree.toml"
		$(PYTHON) "$$public_root/tools/release_status.py" check --root "$$public_root" --tag "$(TAG)"
	else
		$(PYTHON) tools/release_status.py check --root . --tag "$(TAG)"
	fi

static:
	$(PYTHON) -m ruff check zlang tests tools
	$(PYTHON) -m ruff check --select ARG001,ARG002 zlang
	$(PYTHON) tools/python_source_audit.py zlang
	$(PYTHON) -m compileall -q zlang tests tools
	git diff --check
	git diff --cached --check

jit-check:
	if [[ -d native-runtime ]]; then
		cd native-runtime
		cargo fmt --all -- --check
		cargo test --locked
		cargo clippy --locked --all-targets -- -D warnings
	else
		$(PYTHON) -c 'import _zlang_native_sim as runtime; from zlang.simulation_plan import SIMULATION_RUNTIME_ABI as expected; assert runtime.runtime_abi() == expected'
	fi

jit-audit:
	if [[ -d native-runtime ]]; then
		arguments=(--root native-runtime)
		if [[ -n "$${ZLANG_NATIVE_RUNTIME_WHEEL:-}" ]]; then
			arguments+=(--wheel "$$ZLANG_NATIVE_RUNTIME_WHEEL")
		fi
		$(PYTHON) tools/audit_native_runtime.py "$${arguments[@]}"
	else
		wheel="$${ZLANG_NATIVE_RUNTIME_WHEEL:-}"
		if [[ -z "$$wheel" ]]; then
			wheels=(release/native-wheels/*manylinux_2_28_x86_64.whl)
			test "$${#wheels[@]}" -eq 1 && test -f "$${wheels[0]}"
			wheel="$${wheels[0]}"
		fi
		version="$(TAG)"
		$(PYTHON) tools/audit_native_binary.py --expected-version "$${version#v}" "$$wheel"
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
	$(PYTHON) tools/audit_native_binary.py --expected-version "$${version#v}" \
		--require-release-platforms "$${wheels[@]}"

audit:
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-audit.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
		$(PYTHON) -m reuse --root "$$public_root" lint
		$(PYTHON) -m pip_audit "$$public_root" --progress-spinner off
	else
		$(PYTHON) -m reuse --root . lint
		$(PYTHON) -m pip_audit . --progress-spinner off
	fi

release-tools:
	if [[ -f tools/public_tree.py ]]; then
		public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-tools.XXXXXX")"
		trap 'rm -rf -- "$$public_root"' EXIT
		$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
		$(PYTHON) "$$public_root/tools/release_status.py" check \
			--root "$$public_root" --check-tools --tag "$(TAG)"
	else
		$(PYTHON) tools/release_status.py check --root . --check-tools --tag "$(TAG)"
	fi

test-fast:
	$(PYTHON) -m pytest -n "$(WORKERS)" --dist=loadscope -q $(FAST_TEST_PATHS)

test:
	$(PYTHON) -m pytest -n "$(WORKERS)" --dist=loadscope -q

test-structural:
	$(PYTHON) -m pytest -q tests/structural/test_structural_witnesses.py

structural-baseline:
	mkdir -p "$(STRUCTURAL_REPORT_DIR)"
	$(PYTHON) tools/structural_synthesis_baseline.py \
		--profile "$(STRUCTURAL_PROFILE)" \
		--json "$(STRUCTURAL_REPORT_DIR)/$(STRUCTURAL_PROFILE).json" \
		--markdown "$(STRUCTURAL_REPORT_DIR)/$(STRUCTURAL_PROFILE).md"

community-pdf:
	$(PYTHON) tools/build_community_pdf.py \
			--root . --cover "$(COMMUNITY_PDF_COVER)"

community-pdf-check:
	if [[ -f tools/build_community_pdf.py ]]; then
		$(PYTHON) tools/build_community_pdf.py --root . --check
	else
		$(PYTHON) tools/release_status.py check --root . --tag "$(TAG)"
	fi

test-release-twice:
	public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-tests.XXXXXX")"
	trap 'rm -rf -- "$$public_root"' EXIT
	python_bin="$(PYTHON)"
	if [[ "$$python_bin" == */* ]]; then
		python_bin="$$(cd "$$(dirname "$$python_bin")" && pwd)/$$(basename "$$python_bin")"
	else
		python_bin="$$(command -v "$$python_bin")"
	fi
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
	mapfile -t native_wheels < <(find "$$public_root/.native-test/wheels" \
		-maxdepth 1 -type f -name 'zlang_native_sim-*.whl' -print)
	test "$${#native_wheels[@]}" -eq 1
	"$$python_bin" "$$public_root/tools/materialize_native_test_extension.py" \
		"$${native_wheels[0]}" "$$public_root"
	cd "$$public_root"
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
	umask 022
	export SOURCE_DATE_EPOCH="$$(git log -1 --format=%ct)"
	python_bin="$(PYTHON)"
	if [[ "$$python_bin" == */* ]]; then
		python_dir="$$(cd "$$(dirname "$$python_bin")" && pwd)"
		export PATH="$$python_dir:$$PATH"
	fi
	source_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-release-source.XXXXXX")"
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
		$(PYTHON) tools/audit_native_runtime.py --root native-runtime \
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
release-candidate: native-release-set community-pdf-check public-check static jit-check jit-audit jit-advisory-audit audit release-tools test-release-twice editor-test package
