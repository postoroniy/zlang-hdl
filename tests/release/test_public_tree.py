from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

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
        "tests/fixtures/sample.zl": "module Fixture { in x:u8 out y:u8 y=x }\n",
        "examples/top.zl": "import std.core module Top { in x:u8 out y:u8 y=x }\n",
        "stdlib/core.zl": "module StdCore { in x:u8 out y:u8 y=x }\n",
        ".github/workflows/ci.yml": f"steps:\n  - uses: {PINNED_CHECKOUT}\n",
        "private.txt": "not public\n",
        "release/public-tree.toml": """
schema = 1
[projection]
manifest = ".public-tree-manifest.json"
repository = "https://github.com/postoroniy/zlang-hdl"
include = ["README.md", "docs/**", "zlang/**", "tests/**", "examples/**", "stdlib/**", ".github/**", "release/**"]
exclude = ["**/__pycache__/**"]
required = ["README.md", "docs/guide.md", "tests/fixtures/sample.zl"]
closure_roots = ["zlang", "tests/fixtures", "examples", "stdlib"]
[content]
scan_exempt = ["release/public-tree.toml"]
forbidden_substrings = ["/home/private/"]
forbidden_regex = ["ghp_[A-Za-z0-9]{30,}"]
text_extensions = ["", ".md", ".py", ".toml", ".yml", ".zl"]
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
    (tmp_path / "examples/top.zl").write_text(
        "module Child { in x:u8 out y:u8 y=x } module Top { in x:u8 out y:u8 y=x }"
    )
    (tmp_path / "tests/systemverilog/test_example_coverage.py").write_text(
        "CHILD_OR_TEMPLATE_ONLY = {('top.zl', 'Child'): object()}\n"
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

    release = workflows["release.yml"]
    assert "import std.math.complex" in release
    assert '"$environment/bin/zlangc" package-smoke.zl --check' in release

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


def test_repository_public_projection_is_closed_and_excludes_private_files() -> None:
    spec = importlib.util.spec_from_file_location("public_tree_live", PUBLIC_TREE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    selected = {
        path.relative_to(ROOT).as_posix()
        for path in module.validate_source(ROOT)
    }
    assert "docs/known-limitations.md" in selected
    assert "LICENSES/Apache-2.0.txt" in selected
    assert "LICENSES/MIT.txt" in selected
    assert not any(path.startswith("examples/comparisons/") for path in selected)
    assert not any("design-freeze" in path for path in selected)
    assert not any(path.startswith("docs/milestone-") for path in selected)
    for root in (
        "docs/reproducers",
        "editors/vscode/zlang-vscode",
        "stdlib",
        "tests",
        "tools",
        "zlang",
    ):
        expected = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / root).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        assert expected <= selected


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
