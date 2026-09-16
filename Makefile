SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)
WORKERS ?= 8
BUILD_ROOT ?= build/local-release
DIST_DIR ?= $(BUILD_ROOT)/dist
TAG ?= v$(shell $(PYTHON) -c 'from zlang._version import __version__; print(__version__)')

FAST_TEST_PATHS := \
	tests/parser tests/semantic tests/conformance tests/editor \
	tests/packaging tests/release

.PHONY: help public-check static audit release-tools test-fast test \
	test-release-twice editor-test package release-candidate

help:
	@printf '%s\n' \
		'make public-check       validate the public projection and release metadata' \
		'make static             run Ruff, compileall, and diff checks' \
		'make audit              run REUSE and Python dependency audits' \
		'make release-tools      validate the pinned external-tool inventory' \
		'make test-fast          run the compiler/editor/package CI subset' \
		'make test               run the complete parallel test suite' \
		'make test-release-twice run two zero-skip suites and validate both JUnit files' \
		'make editor-test        install locked editor dependencies and run its tests' \
		'make package            build one sdist and two byte-identical wheels' \
		'make release-candidate  run the local non-publishing release gate'

public-check:
	public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-check.XXXXXX")"
	trap 'rm -rf -- "$$public_root"' EXIT
	$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
	$(PYTHON) "$$public_root/tools/public_tree.py" check-export --source "$$public_root"
	$(PYTHON) "$$public_root/tools/release_status.py" check --root "$$public_root" --tag "$(TAG)"

static:
	$(PYTHON) -m ruff check zlang tests tools
	$(PYTHON) -m compileall -q zlang tests tools
	git diff --check
	git diff --cached --check

audit:
	public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-audit.XXXXXX")"
	trap 'rm -rf -- "$$public_root"' EXIT
	$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
	$(PYTHON) -m reuse --root "$$public_root" lint
	$(PYTHON) -m pip_audit "$$public_root" --progress-spinner off

release-tools:
	public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-tools.XXXXXX")"
	trap 'rm -rf -- "$$public_root"' EXIT
	$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
	$(PYTHON) "$$public_root/tools/release_status.py" check \
		--root "$$public_root" --check-tools --tag "$(TAG)"

test-fast:
	$(PYTHON) -m pytest -n "$(WORKERS)" --dist=loadscope -q $(FAST_TEST_PATHS)

test:
	$(PYTHON) -m pytest -n "$(WORKERS)" --dist=loadscope -q

test-release-twice:
	public_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-public-tests.XXXXXX")"
	trap 'rm -rf -- "$$public_root"' EXIT
	python_bin="$(PYTHON)"
	if [[ "$$python_bin" == */* ]]; then
		python_bin="$$(cd "$$(dirname "$$python_bin")" && pwd)/$$(basename "$$python_bin")"
	fi
	report_root="$$(pwd)/build"
	mkdir -p "$$report_root"
	$(PYTHON) tools/public_tree.py export --source . --destination "$$public_root"
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
	umask 022
	export SOURCE_DATE_EPOCH="$$(git log -1 --format=%ct)"
	source_root="$$(mktemp -d "$${TMPDIR:-/tmp}/zlang-release-source.XXXXXX")"
	trap 'rm -rf -- "$$source_root"' EXIT
	mkdir -p "$(BUILD_ROOT)/first" "$(BUILD_ROOT)/second" "$(DIST_DIR)"
	$(PYTHON) tools/public_tree.py export --source . --destination "$$source_root"
	$(PYTHON) -m build "$$source_root" --no-isolation --sdist --outdir "$(BUILD_ROOT)/first"
	$(PYTHON) -m build "$$source_root" --no-isolation --wheel --outdir "$(BUILD_ROOT)/first"
	$(PYTHON) -m build "$$source_root" --no-isolation --wheel --outdir "$(BUILD_ROOT)/second"
	mapfile -t first_wheels < <(find "$(BUILD_ROOT)/first" -maxdepth 1 -type f -name '*.whl' -print | sort)
	mapfile -t second_wheels < <(find "$(BUILD_ROOT)/second" -maxdepth 1 -type f -name '*.whl' -print | sort)
	test "$${#first_wheels[@]}" -eq 1 && test "$${#second_wheels[@]}" -eq 1
	cmp -- "$${first_wheels[0]}" "$${second_wheels[0]}"
	cp -- "$(BUILD_ROOT)"/first/* "$(DIST_DIR)/"
	$(PYTHON) -m twine check "$(DIST_DIR)"/*

# This target prepares and validates local candidate artifacts. It deliberately
# does not create commits/tags, upload artifacts, or publish a GitHub release.
release-candidate: public-check static audit release-tools test-release-twice editor-test package
