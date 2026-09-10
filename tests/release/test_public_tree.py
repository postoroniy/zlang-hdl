from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_TREE = ROOT / "tools" / "public_tree.py"
RELEASE_STATUS = ROOT / "tools" / "release_status.py"
PINNED_CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


def _run(tool: Path, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tool), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _write_fixture(root: Path) -> None:
    files = {
        "README.md": "[Guide](docs/guide.md)\n",
        "docs/guide.md": "# Guide\n",
        "zlang/__init__.py": "from zlang.value import VALUE\n",
        "zlang/value.py": "VALUE = 1\n",
        "tests/fixtures/sample.zhl": "module Fixture { in x:u8 out y:u8 y=x }\n",
        "examples/top.zhl": "import std.core module Top { in x:u8 out y:u8 y=x }\n",
        "stdlib/core.zhl": "module StdCore { in x:u8 out y:u8 y=x }\n",
        ".github/workflows/ci.yml": f"steps:\n  - uses: {PINNED_CHECKOUT}\n",
        "private.txt": "not public\n",
        "release/public-tree.toml": """
schema = 1
[projection]
manifest = ".public-tree-manifest.json"
repository = "https://github.com/postoroniy/zlang-hdl"
include = ["README.md", "docs/**", "zlang/**", "tests/**", "examples/**", "stdlib/**", ".github/**", "release/**"]
exclude = ["**/__pycache__/**"]
required = ["README.md", "docs/guide.md", "tests/fixtures/sample.zhl"]
closure_roots = ["zlang", "tests/fixtures", "examples", "stdlib"]
[content]
scan_exempt = ["release/public-tree.toml"]
forbidden_substrings = ["/home/private/"]
forbidden_regex = ["ghp_[A-Za-z0-9]{30,}"]
text_extensions = ["", ".md", ".py", ".toml", ".yml", ".zhl"]
""".strip()
        + "\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_public_tree_export_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_fixture(source)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for destination in (first, second):
        completed = _run(
            PUBLIC_TREE,
            "export",
            "--source",
            str(source),
            "--destination",
            str(destination),
        )
        assert completed.returncode == 0, completed.stderr
        checked = _run(PUBLIC_TREE, "check-export", "--source", str(destination))
        assert checked.returncode == 0, checked.stderr
    assert not (first / "private.txt").exists()
    assert (first / ".public-tree-manifest.json").read_bytes() == (
        second / ".public-tree-manifest.json"
    ).read_bytes()
    manifest = json.loads((first / ".public-tree-manifest.json").read_text())
    assert [item["path"] for item in manifest["files"]] == sorted(
        item["path"] for item in manifest["files"]
    )

    (first / "README.md").write_text("tampered\n")
    checked = _run(PUBLIC_TREE, "check-export", "--source", str(first))
    assert checked.returncode == 1
    assert "manifest does not match" in checked.stderr


def test_public_tree_excludes_editor_scratch_but_retains_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_fixture(source)
    repository_config = tomllib.loads(
        (ROOT / "release/public-tree.toml").read_text(encoding="utf-8")
    )
    config_path = source / "release/public-tree.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace(
            'exclude = ["**/__pycache__/**"]',
            "exclude = " + json.dumps(repository_config["projection"]["exclude"]),
        )
        .replace('"README.md", "docs/**"', '"editors/**", "README.md", "docs/**"')
        .replace('closure_roots = ["zlang"', 'closure_roots = ["editors", "zlang"')
        .replace(
            'text_extensions = ["", ".md", ".py", ".toml", ".yml", ".zhl"]',
            "text_extensions = "
            + json.dumps(repository_config["content"]["text_extensions"]),
        ),
        encoding="utf-8",
    )
    editor = "editors/vscode/zlang-hdl"
    retained = {
        f"{editor}/README.md": "# Lexical editor\n",
        f"{editor}/package.json": '{"name":"zlang-hdl"}\n',
        f"{editor}/package-lock.json": '{"lockfileVersion":3}\n',
        f"{editor}/syntaxes/zlang.tmLanguage.json": '{"scopeName":"source.zlang"}\n',
        f"{editor}/test/tokenize.test.cjs": "'use strict';\n",
        f"{editor}/examples/verification.zhl": "module Fixture { in x:u8 out y:u8 y=x }\n",
    }
    excluded = (
        "node_modules/dependency/index.js",
        ".vscode-test/code/editor.bin",
        "root.vsix",
        ".npmrc",
        f"{editor}/node_modules/dependency/index.js",
        f"{editor}/node_modules/.bin/executable",
        f"{editor}/.vscode-test/code/editor.bin",
        f"{editor}/nested/.vscode-test/code/editor.bin",
        f"{editor}/zlang-hdl-0.1.0.vsix",
        f"{editor}/nested/artifact.vsix",
        f"{editor}/.npmrc",
        f"{editor}/nested/.npmrc",
        f"{editor}/.tmp/session.bin",
        f"{editor}/build/package.bin",
        f"{editor}/dist/package.bin",
        f"{editor}/out/extension.js",
    )
    for relative, content in retained.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relative in excluded:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # An ignored artifact must not even reach UTF-8/content validation.
        path.write_bytes(b"\x00\xff private generated scratch\n")
    # npm's executable links must be pruned with their excluded directory,
    # before the projection's deliberate rejection of source symlinks.
    (source / editor / "node_modules/.bin/tool").symlink_to("executable")

    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join((*excluded, *retained)) + "\n",
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert set(ignored.stdout.splitlines()) == set(excluded)

    destination = tmp_path / "public"
    completed = _run(
        PUBLIC_TREE,
        "export",
        "--source",
        str(source),
        "--destination",
        str(destination),
    )
    assert completed.returncode == 0, completed.stderr
    checked = _run(PUBLIC_TREE, "check-export", "--source", str(destination))
    assert checked.returncode == 0, checked.stderr
    manifest = json.loads((destination / ".public-tree-manifest.json").read_text())
    selected = {item["path"] for item in manifest["files"]}
    assert retained.keys() <= selected
    assert not selected.intersection(excluded)
    for relative, content in retained.items():
        assert (destination / relative).read_text(encoding="utf-8") == content
    assert not any((destination / relative).exists() for relative in excluded)


@pytest.mark.parametrize(
    ("relative", "replacement", "diagnostic"),
    (
        ("README.md", "[missing](docs/missing.md)\n", "broken local link"),
        ("zlang/value.py", "TOKEN='ghp_" + "a" * 32 + "'\n", "secret/private-content"),
        (
            ".github/workflows/ci.yml",
            "steps:\n  - uses: actions/checkout@v4\n",
            "not pinned by commit",
        ),
    ),
)
def test_public_tree_fails_closed(
    tmp_path: Path, relative: str, replacement: str, diagnostic: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_fixture(source)
    (source / relative).write_text(replacement, encoding="utf-8")
    completed = _run(PUBLIC_TREE, "check-source", "--source", str(source))
    assert completed.returncode == 1
    assert diagnostic in completed.stderr


def test_public_tree_rejects_omitted_fixture_and_nonempty_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_fixture(source)
    config = source / "release/public-tree.toml"
    config.write_text(
        config.read_text().replace('"tests/**", ', ""), encoding="utf-8"
    )
    completed = _run(PUBLIC_TREE, "check-source", "--source", str(source))
    assert completed.returncode == 1
    assert "required public file is excluded" in completed.stderr

    _write_fixture(source)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "keep").write_text("data")
    completed = _run(
        PUBLIC_TREE,
        "export",
        "--source",
        str(source),
        "--destination",
        str(destination),
    )
    assert completed.returncode == 1
    assert "destination is not empty" in completed.stderr


def test_public_tree_rejects_manifest_path_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_fixture(source)
    config = source / "release/public-tree.toml"
    config.write_text(
        config.read_text().replace(
            'manifest = ".public-tree-manifest.json"',
            'manifest = "../escaped-manifest.json"',
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "destination"
    completed = _run(
        PUBLIC_TREE,
        "export",
        "--source",
        str(source),
        "--destination",
        str(destination),
    )
    assert completed.returncode == 1
    assert "normalized repository-relative path" in completed.stderr
    assert not (tmp_path / "escaped-manifest.json").exists()


def test_release_status_checks_version_corpus_and_junit(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    (tmp_path / "examples").mkdir()
    (tmp_path / "tests/systemverilog").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0.1.0a1"\n'
    )
    (tmp_path / "examples/top.zhl").write_text(
        "module Child { in x:u8 out y:u8 y=x } module Top { in x:u8 out y:u8 y=x }"
    )
    (tmp_path / "tests/systemverilog/test_example_coverage.py").write_text(
        "CHILD_OR_TEMPLATE_ONLY = {('top.zhl', 'Child'): object()}\n"
        "DIRECT_UNSUPPORTED: dict = {}\n"
    )
    status = {
        "schema": 1,
        "release": {
            "channel": "alpha",
            "repository": "https://github.com/postoroniy/zlang-hdl",
            "version": "0.1.0a1",
        },
        "platform": {
            "operating_system": "Linux",
            "python": ">=3.12,<3.13",
            "architecture": "x86_64",
        },
        "validation": {
            "minimum_tests_passed": 2,
            "maximum_tests_skipped": 0,
            "example_corpus": {
                "source_files": 1,
                "module_roots": 2,
                "standalone_supported": 1,
                "child_or_template_only": 1,
                "direct_unsupported": 0,
            },
        },
        "eda_toolchain": {
            "clash": "1.11.0",
            "iverilog": "13.0",
            "sby": "0.68",
            "verilator": "5.044",
            "vvp": "13.0",
            "yosys": "0.68",
            "z3": "4.8.12",
        },
    }
    (tmp_path / "release/status.json").write_text(json.dumps(status))
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="2"><testcase name="a"/><testcase name="b"/></testsuite>'
    )
    completed = _run(
        RELEASE_STATUS,
        "check",
        "--root",
        str(tmp_path),
        "--junit",
        str(junit),
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr

    completed = _run(
        RELEASE_STATUS,
        "check",
        "--root",
        str(tmp_path),
        "--tag",
        "v0.1.0a2",
        cwd=ROOT,
    )
    assert completed.returncode == 1
    assert "does not match package version" in completed.stderr

    junit.write_text(
        '<testsuite tests="2" skipped="1"><testcase name="a"/>'
        '<testcase name="b"><skipped/></testcase></testsuite>'
    )
    completed = _run(
        RELEASE_STATUS,
        "check",
        "--root",
        str(tmp_path),
        "--junit",
        str(junit),
        cwd=ROOT,
    )
    assert completed.returncode == 1
    assert "skipped" in completed.stderr


def test_repository_workflows_use_only_immutable_external_actions() -> None:
    spec = importlib.util.spec_from_file_location("public_tree", PUBLIC_TREE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    texts = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    }
    module._check_action_pins(texts)


def test_release_workflows_preserve_checkout_and_security_contracts() -> None:
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    }
    for name in ("ci.yml", "eda.yml", "release.yml"):
        text = workflows[name]
        assert text.index("check-export --source .") < text.index("pip install")

    assert (
        "if: github.repository == 'postoroniy/zlang-hdl' "
        "&& github.ref == 'refs/heads/main'"
    ) in workflows["eda.yml"]

    release = workflows["release.yml"]
    assert (
        "needs: validate\n"
        "    runs-on: [self-hosted, linux, x64, zlang-eda]"
    ) in release
    assert "import std.math.complex" in release
    assert '"$environment/bin/zlang" package-smoke.zhl --check' in release

    # Installer inventory is release evidence, not merely project dependencies.
    assert release.count("--upgrade pip==26.2.1") == 2
    assert (
        "name: Build one sdist and a byte-reproducible wheel\n"
        "        shell: bash\n"
    ) in release  # GitHub's explicit bash enables pipefail for freeze | sort.
    for kind in ("wheel", "sdist"):
        assert f"python -m venv --without-pip build/{kind}-venv" in release
        assert f"python -m pip --python build/{kind}-venv/bin/python install" in release
    assert release.count("--disable-pip-version-check pip==26.2.1 dist/") == 2
    ordered_gates = (
        "-m pip freeze --all --exclude zlang-hdl",
        "diff -u build/wheel-requirements.txt build/sdist-requirements.txt",
        "cp build/wheel-requirements.txt dist/release-requirements.txt",
        "python tools/release_inventory.py",
        "cyclonedx-py requirements dist/release-requirements.txt",
        "name: Attest release checksums",
        "name: Upload reviewed release artifacts",
    )
    assert [release.index(gate) for gate in ordered_gates] == sorted(
        release.index(gate) for gate in ordered_gates
    )
    assert '--project-version "${GITHUB_REF_NAME#v}"' in release
    assert "--report build/release-inventory-audit.json" in release

    secret_scan = workflows["secret-scan.yml"]
    assert 'GITLEAKS_VERSION: "8.24.3"' in secret_scan
    assert (
        'GITLEAKS_ARCHIVE_SHA256: '
        '"9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c"'
        in secret_scan
    )
    assert "Gitleaks self-test failed to detect" in secret_scan
    assert '"$binary_dir/gitleaks" git . --redact --no-banner' in secret_scan

    dependency_review = workflows["dependency-review.yml"]
    assert (
        "actions/dependency-review-action@"
        "a1d282b36b6f3519aa1f3fc636f609c47dddb294 # v5.0.0"
        in dependency_review
    )


def test_release_workflow_audits_editor_before_attestation_and_publication(
    tmp_path: Path,
) -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert (
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0"
        in release
    )
    assert 'node-version: "22.23.2"' in release
    assert "cache-dependency-path: editors/vscode/zlang-hdl/package-lock.json" in release
    gates = (
        "name: Verify annotated tag and GitHub signature",
        "check-export --source .",
        "python tools/release_inventory.py",
        "name: Set up pinned Node.js for the lexical extension",
        "run: npm ci --ignore-scripts",
        "name: Require a fresh full locked editor advisory audit",
        "npm audit --package-lock-only --include=dev --include=optional --include=peer",
        "run: npm test",
        "name: Build and audit the exact-tag static lexical VSIX",
        'npm --prefix editors/vscode/zlang-hdl run package -- "$RUNNER_TEMP/zlang-hdl-0.1.0.vsix"',
        'python tests/editor/test_vscode_package.py "$RUNNER_TEMP/zlang-hdl-0.1.0.vsix"',
        'cp -- "$RUNNER_TEMP/zlang-hdl-0.1.0.vsix"',
        "name: Retain accepted dependency audit evidence",
        "cyclonedx-py requirements dist/release-requirements.txt",
        "name: Attest release checksums",
        "name: Upload reviewed release artifacts",
        'gh release create "$TAG" dist/*',
    )
    assert [release.index(gate) for gate in gates] == sorted(
        release.index(gate) for gate in gates
    )
    advisory = release.split(
        "      - name: Require a fresh full locked editor advisory audit\n", 1
    )[1].split("      - name:", 1)[0]
    assert "        shell: bash\n" in advisory
    assert '--audit-level=info --json > "$report"' in advisory
    assert 'report="$GITHUB_WORKSPACE/build/editor-npm-audit.json"' in advisory
    assert not re.search(r"\|\||--omit|--production|continue-on-error|audit-level=none", advisory)
    audit_script = textwrap.dedent(advisory.split("        run: |\n", 1)[1])
    (tmp_path / "build").mkdir()
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {}} | {
            f"node_modules/fixture-{index}": {"version": "1.0.0", "dev": True}
            for index in range(3)
        },
    }))
    clean_report = {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {
            "vulnerabilities": dict.fromkeys(
                ("info", "low", "moderate", "high", "critical", "total"), 0,
            ),
            "dependencies": {"dev": 3, "total": 3},
        },
    }
    # Exercise the literal workflow shell with controlled registry responses,
    # including npm's nonzero exit even when its stdout looks clean.
    cases = (
        (json.dumps(clean_report), 0, True),
        (json.dumps(clean_report), 1, False),
        ("not JSON", 0, False),
        (json.dumps(clean_report | {"error": {"code": "EAI_AGAIN"}}), 0, False),
        (json.dumps(clean_report | {"vulnerabilities": {"fixture": {}}}), 0, False),
        (json.dumps(clean_report | {"metadata": {}}), 0, False),
        (json.dumps(clean_report | {
            "metadata": clean_report["metadata"] | {"dependencies": {"total": 3}},
        }), 0, False),
        (json.dumps(clean_report | {
            "metadata": clean_report["metadata"] | {"dependencies": {"dev": 2, "total": 2}},
        }), 0, False),
    )
    stubs = (
        'npm() { printf "%s\\n" "$NPM_AUDIT_FIXTURE"; return "$NPM_AUDIT_STATUS"; }\n'
        'python() { "$PYTHON_TEST_EXECUTABLE" "$@"; }\n'
    )
    for report, status, accepted in cases:
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", stubs + audit_script],
            cwd=tmp_path, capture_output=True, text=True,
            env={
                "GITHUB_WORKSPACE": str(tmp_path),
                "NPM_AUDIT_FIXTURE": report,
                "NPM_AUDIT_STATUS": str(status),
                "PYTHON_TEST_EXECUTABLE": sys.executable,
            },
        )
        assert (result.returncode == 0) is accepted, result.stderr
        assert (tmp_path / "build/editor-npm-audit.json").read_text() == report + "\n"
    packaging = release.split(
        "      - name: Build and audit the exact-tag static lexical VSIX\n", 1
    )[1].split("      - name:", 1)[0]
    assert "        shell: bash\n" in packaging
    assert "||" not in packaging
    assert '> "$RUNNER_TEMP/zlang-hdl-0.1.0-vsix-audit.json"' in packaging
    assert '"$RUNNER_TEMP/zlang-hdl-0.1.0-vsix-audit.json" dist/' in packaging
    evidence = release.split(
        "      - name: Retain accepted dependency audit evidence\n", 1
    )[1].split("      - name:", 1)[0]
    assert "name: release-${{ github.ref_name }}-dependency-audits" in evidence
    assert "build/release-inventory-audit.json" in evidence
    assert "build/editor-npm-audit.json" in evidence
    assert "if-no-files-found: error" in evidence
    assert "retention-days: 30" in evidence
    assert "subject-checksums: dist/SHA256SUMS" in release
    assert "needs: [validate, eda]" in release
    assert release.count("--junitxml=build/release-") == 2
    assert not re.search(r"(?:vsce|ovsx|npm)\s+publish\b|VSCE_PAT|OVSX_PAT|NODE_AUTH_TOKEN", release)


def test_release_checksums_cover_editor_payloads_and_fail_if_either_is_missing(
    tmp_path: Path,
) -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    step = release.split(
        "      - name: Build release SBOM and checksums after inventory acceptance\n", 1
    )[1].split("      - name:", 1)[0]
    assert "        shell: bash\n" in step  # Explicit bash enables pipeline failure.
    body = textwrap.dedent(step.split("        run: |\n", 1)[1])
    checksum_script = "(cd dist && sha256sum" + body.split("(cd dist && sha256sum", 1)[1]
    payloads = (
        "zlang_hdl-0.1.0a3-py3-none-any.whl",
        "zlang_hdl-0.1.0a3.tar.gz",
        "zlang-hdl-v0.1.0a3.cdx.json",
        "release-requirements.txt",
        "zlang-hdl-0.1.0.vsix",
        "zlang-hdl-0.1.0-vsix-audit.json",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    expected = {}
    for name in payloads:
        content = f"checksum fixture: {name}\n".encode()
        (dist / name).write_bytes(content)
        expected[name] = hashlib.sha256(content).hexdigest()
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", checksum_script],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    recorded = {
        name: digest
        for digest, name in (
            line.split("  ", 1)
            for line in (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        )
    }
    assert recorded == expected
    for name in payloads[-2:]:
        path = dist / name
        content = path.read_bytes()
        path.unlink()
        failed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", checksum_script],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert failed.returncode != 0, f"Missing editor payload was accepted: {name}"
        path.write_bytes(content)


def test_repository_public_projection_is_closed_and_excludes_private_files() -> None:
    spec = importlib.util.spec_from_file_location("public_tree_live", PUBLIC_TREE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    config = module._load_config(ROOT, Path("release/public-tree.toml"))
    source_files = module._source_files(ROOT, config.manifest, config.exclude)
    selected = {
        path.relative_to(ROOT).as_posix()
        for path in module.validate_source(ROOT)
    }
    assert "docs/known-limitations.md" in selected
    assert "docs/project-scope.md" in selected
    assert "docs/editions.md" in selected
    assert "docs/licensing/COMMUNITY_BASELINE.md" in selected
    assert "docs/licensing/RELEASE_BOUNDARY_AUDIT.md" not in selected
    assert "TRADEMARKS.md" in selected
    assert "LICENSES/Apache-2.0.txt" in selected
    assert "LICENSES/MIT.txt" in selected
    assert not any(path.startswith("examples/comparisons/") for path in selected)
    assert not any("design-freeze" in path for path in selected)
    assert not any(path.startswith("docs/milestone-") for path in selected)
    for root in (
        "docs/reproducers",
        "editors/vscode/zlang-hdl",
        "stdlib",
        "tests",
        "tools",
        "zlang",
    ):
        expected = {
            path.relative_to(ROOT).as_posix()
            for path in source_files
            if path.is_relative_to(ROOT / root)
        }
        assert expected <= selected


def test_public_policy_documents_are_discoverable() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    wifi_notice = (
        ROOT / "examples/projects/80211a_transmitter/NOTICE"
    ).read_text(encoding="utf-8")
    trademarks = (ROOT / "TRADEMARKS.md").read_text(encoding="utf-8")
    project_scope = (ROOT / "docs/project-scope.md").read_text(encoding="utf-8")
    baseline = (ROOT / "docs/licensing/COMMUNITY_BASELINE.md").read_text(
        encoding="utf-8"
    )
    editions = (ROOT / "docs/editions.md").read_text(encoding="utf-8")

    assert "TRADEMARKS.md" in readme
    assert "docs/project-scope.md" in readme
    assert "docs/licensing/COMMUNITY_BASELINE.md" in readme
    assert "docs/editions.md" in readme
    assert "docs/licensing/COMMUNITY_BASELINE.md" in releasing
    assert "Everything in this release remains Community." in baseline
    assert "2026-09 public release" in baseline
    assert "CSR C/C++ software helper generation" in baseline
    assert "SystemVerilog UVM helper generation" in baseline
    assert "not-yet-implemented generators" in baseline
    assert "Verilator" in baseline and "state-access" in baseline
    assert "MIT" in baseline and "CC-BY-4.0" in baseline
    assert "Enterprise; classified, not implemented" in editions
    assert "TRADEMARKS.md" in notice
    assert "make the resulting HDL a copy" not in notice
    assert "TRADEMARKS.md" in releasing
    assert "does not require a Contributor License Agreement" in contributing
    assert "Copyright (c) 2026 Viacheslav Vinogradov" in wifi_notice
    assert "distributed under the MIT License" in wifi_notice
    assert "does not state that the name or any logo is registered" in trademarks
    assert "®" not in trademarks
    assert "™" not in trademarks
    for capability in (
        "direct-SystemVerilog",
        "local safety verification",
        "cross-backend evidence",
        "formal-aware candidate selection",
        "`implement`",
        "`choice`",
    ):
        assert capability in project_scope

def test_no_skip_plugin_turns_skip_into_failure(tmp_path: Path) -> None:
    test_file = tmp_path / "test_skipped.py"
    test_file.write_text(
        "import pytest\n\ndef test_external_tool():\n    pytest.skip('missing')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tools.pytest_no_skips",
            str(test_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "1 skipped" in completed.stdout
