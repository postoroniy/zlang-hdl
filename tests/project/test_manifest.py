from __future__ import annotations

from pathlib import Path

import pytest

from zlang.dependencies import DependencySourceKind
from zlang.project import (
    ProjectManifest,
    ProjectModelError,
    discover_project_manifest,
)


REVISION = "1" * 40


def _manifest(*, profiles: str = "") -> str:
    return f'''schema = 1

[project]
name = "acme.top"
version = "1.2.3"
source-root = "src"

[dependencies]
"vendor.math" = {{ path = "../math" }}
"vendor.fft" = {{ git = "https://example.invalid/fft.git", rev = "{REVISION}" }}
{profiles}'''


def test_manifest_parse_render_round_trip_is_deterministic() -> None:
    manifest = ProjectManifest.parse(_manifest(), path="/work/zlang.toml")
    assert manifest.package == "acme.top"
    assert manifest.version == "1.2.3"
    assert manifest.source_root == Path("src")
    assert tuple(item.package for item in manifest.dependencies) == (
        "vendor.fft",
        "vendor.math",
    )
    assert manifest.dependencies[0].kind is DependencySourceKind.GIT
    assert manifest.dependencies[1].kind is DependencySourceKind.PATH

    rendered = manifest.render()
    restored = ProjectManifest.parse(rendered)
    assert restored.to_data() == manifest.to_data()
    assert ProjectManifest.from_data(manifest.to_data()).to_data() == manifest.to_data()
    assert restored.render() == rendered
    assert restored.resolution_digest == manifest.resolution_digest
    assert "/work" not in manifest.resolution_digest


def test_profiles_round_trip_but_do_not_change_resolution_digest() -> None:
    first = ProjectManifest.parse(_manifest(profiles='''
[profiles.fast]
backend = "systemverilog"
formal = false
workers = [1, 2, 4]

[profiles.fast.constraints]
latency = 4
'''))
    second = ProjectManifest.parse(_manifest(profiles='''
[profiles.fast]
backend = "clash"
formal = true
workers = [8]
'''))
    assert first.resolution_digest == second.resolution_digest
    assert first != second
    restored = ProjectManifest.parse(first.render())
    assert restored.to_data() == first.to_data()


def test_load_hashes_exact_raw_manifest_bytes(tmp_path: Path) -> None:
    path = tmp_path / "zlang.toml"
    path.write_text(_manifest() + "# exact bytes\n", encoding="utf-8")
    manifest = ProjectManifest.load(path)
    assert manifest.content_digest is not None
    assert len(manifest.content_digest) == 64
    assert manifest.path == path
    assert manifest.source_directory == tmp_path / "src"


def test_discovery_walks_source_parents_and_supports_explicit_override(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = root / "src" / "nested" / "top.zhl"
    source.parent.mkdir(parents=True)
    source.write_text("module Top {}\n")
    (root / "zlang.toml").write_text(_manifest())

    discovered = discover_project_manifest(source)
    assert discovered is not None
    assert discovered.path == root / "zlang.toml"
    assert discover_project_manifest(source, explicit=root) == discovered
    assert discover_project_manifest(tmp_path / "outside") is None


@pytest.mark.parametrize(
    ("fragment", "message"),
    (
        ("mystery = 1\n", "unknown project manifest key 'mystery'"),
        ("extra = 1\n", "unknown project table key 'extra'"),
    ),
)
def test_manifest_rejects_unknown_keys(fragment: str, message: str) -> None:
    if fragment.startswith("extra"):
        source = _manifest().replace('source-root = "src"', 'source-root = "src"\n' + fragment)
    else:
        source = fragment + _manifest()
    with pytest.raises(ProjectModelError, match=message):
        ProjectManifest.parse(source)


@pytest.mark.parametrize(
    ("entry", "message"),
    (
        ('"vendor.bad" = { path = "../bad", git = "x", rev = "' + REVISION + '" }',
         "exactly one of path or Git"),
        ('"vendor.bad" = { git = "x" }', "must be.*git"),
        ('"vendor.bad" = { path = "/absolute" }', "portable relative path"),
        ('"vendor.bad" = { git = "x", rev = "main" }', "complete lowercase"),
        ('"std.bus" = { path = "../bad" }', "namespace 'std' is reserved"),
        ('"vendor.bad" = { path = "../bad", typo = 1 }', "unknown dependency.*typo"),
    ),
)
def test_manifest_dependency_source_is_strict(entry: str, message: str) -> None:
    source = '''schema = 1
[project]
name = "acme.top"
version = "1"
source-root = "."
[dependencies]
''' + entry
    with pytest.raises((ProjectModelError, ValueError), match=message):
        ProjectManifest.parse(source)


def test_manifest_rejects_unsupported_schema_and_unsafe_source_root() -> None:
    with pytest.raises(ProjectModelError, match="unsupported project schema 2"):
        ProjectManifest.parse(_manifest().replace("schema = 1", "schema = 2"))
    with pytest.raises(ProjectModelError, match="source-root"):
        ProjectManifest.parse(_manifest().replace('source-root = "src"', 'source-root = "../src"'))


def test_explicit_discovery_has_stable_missing_and_wrong_file_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(ProjectModelError, match="must name zlang.toml"):
        discover_project_manifest(tmp_path, explicit=tmp_path / "other.toml")
    with pytest.raises(ProjectModelError, match="manifest is unavailable"):
        discover_project_manifest(tmp_path, explicit=tmp_path / "zlang.toml")
