"""Content-addressed project indexing must not bypass source or editor checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import zlang.module_resolver as module_resolver
import pytest
from zlang.module_resolver import ModuleResolutionError
from zlang.workspace import load_project_workspace, update_project_lock
from zlang.workspace_parse_cache import _shard_path, load_parse_index


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "design"
    source_root = root / "src"
    source_root.mkdir(parents=True)
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema = 1\n\n[project]\nname = "demo"\nversion = "0.1.0"\n'
        'source-root = "src"\n',
        encoding="utf-8",
    )
    top = source_root / "top.zhl"
    top.write_text("import demo.child module Top {}", encoding="utf-8")
    (source_root / "child.zhl").write_text(
        "import demo.leaf module Child {}", encoding="utf-8"
    )
    (source_root / "leaf.zhl").write_text("module Leaf {}", encoding="utf-8")
    unused = source_root / "unused.zhl"
    unused.write_text("module Unused {}", encoding="utf-8")
    return manifest, top, unused


def _forget_process_ast() -> None:
    module_resolver._PARSED_SOURCE_CACHE.clear()
    module_resolver._PARSED_SOURCE_CACHE_BYTES = 0


def test_persistent_index_defers_unrelated_parse_but_materializes_imports(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manifest, top, unused = _project(tmp_path)
    update_project_lock(manifest)
    _forget_process_ast()
    actual_parse = module_resolver.parse
    calls: list[str] = []

    def count_parse(text: str):
        calls.append(text)
        return actual_parse(text)

    monkeypatch.setattr(module_resolver, "parse", count_parse)
    workspace = load_project_workspace(top)
    assert workspace is not None
    assert calls == []
    assert all(record.ast is None for record in workspace.root_modules)
    closure = workspace.resolver.resolve(("demo.child",))
    assert tuple(record.logical_path for record in closure) == (
        "demo.leaf", "demo.child"
    )
    assert len(calls) == 2
    assert closure[-1].ast.source_identity == "demo.child"

    # A changed, otherwise unused module is parsed again; the old exact digest
    # cannot validate the new source bytes.
    unused.write_text("module Unused { in x:u8 }", encoding="utf-8")
    _forget_process_ast()
    calls.clear()
    load_project_workspace(top)
    assert calls == ["module Unused { in x:u8 }"]


def test_corrupt_shard_misses_and_unsaved_overlay_is_never_published(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manifest, top, _ = _project(tmp_path)
    update_project_lock(manifest)
    digest = hashlib.sha256(top.read_bytes()).hexdigest()
    shard = _shard_path("demo.top", digest)
    shard.write_bytes(b"{broken")
    _forget_process_ast()
    calls: list[str] = []
    actual_parse = module_resolver.parse

    def count_parse(text: str):
        calls.append(text)
        return actual_parse(text)

    monkeypatch.setattr(module_resolver, "parse", count_parse)
    workspace = load_project_workspace(top)
    assert workspace is not None
    assert "import demo.child module Top {}" in calls

    overlay = "import demo.child module Top { in changed:u8 }"
    overlay_digest = hashlib.sha256(overlay.encode()).hexdigest()
    workspace = load_project_workspace(top, source_overlays={top.resolve(): overlay})
    assert workspace is not None
    assert next(
        record.digest
        for record in workspace.root_modules
        if record.logical_path == "demo.top"
    ) == overlay_digest
    assert load_parse_index("demo.top", overlay_digest) is None


def test_validly_encoded_but_wrong_import_index_fails_closed_on_use(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manifest, top, _ = _project(tmp_path)
    update_project_lock(manifest)
    child = top.with_name("child.zhl")
    digest = hashlib.sha256(child.read_bytes()).hexdigest()
    shard = _shard_path("demo.child", digest)
    data = json.loads(shard.read_text(encoding="utf-8"))
    data["imports"] = ["demo.unused"]
    shard.write_text(json.dumps(data), encoding="utf-8")
    _forget_process_ast()

    workspace = load_project_workspace(top)
    assert workspace is not None
    with pytest.raises(ModuleResolutionError, match="cached module index dependency mismatch"):
        workspace.resolver.resolve(("demo.child",))
