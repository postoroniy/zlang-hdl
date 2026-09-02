from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.workspace import (
    WorkspaceError,
    load_project_workspace,
    update_project_lock,
)


def _manifest(
    root: Path,
    name: str,
    dependencies: str = "",
    *,
    profiles: str = "",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "zlang.toml"
    path.write_text(
        "schema = 1\n\n"
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'source-root = "src"\n'
        + (("\n[dependencies]\n" + dependencies) if dependencies else "")
        + profiles
    )
    (root / "src").mkdir(exist_ok=True)
    return path


def _source(root: Path, relative: str, text: str) -> Path:
    path = root / "src" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _path_graph(tmp_path: Path) -> tuple[Path, Path, Path]:
    leaf = tmp_path / "leaf"
    _manifest(leaf, "leaf")
    _source(leaf, "value.zhl", "module LeafValue { in x:u8 out y:u8 y=x }")

    middle = tmp_path / "middle"
    _manifest(middle, "middle", 'leaf = { path = "../leaf" }\n')
    _source(
        middle,
        "child.zhl",
        "import leaf.value module Child { in x:u8 out y:u8 "
        "inst leaf:LeafValue leaf.x=x y=leaf.y }",
    )

    root = tmp_path / "root"
    manifest = _manifest(root, "demo", 'middle = { path = "../middle" }\n')
    top = _source(
        root,
        "top.zhl",
        "import middle.child module Top { in x:u8 out y:u8 "
        "inst child:Child child.x=x y=child.y }",
    )
    return manifest, top, leaf


def _snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _commit_git(root: Path) -> str:
    git = shutil.which("git")
    assert git is not None
    subprocess.run((git, "init", "-q", str(root)), check=True)
    subprocess.run((git, "-C", str(root), "config", "user.name", "ZLang Test"), check=True)
    subprocess.run((git, "-C", str(root), "config", "user.email", "zlang@example.invalid"), check=True)
    subprocess.run((git, "-C", str(root), "add", "."), check=True)
    subprocess.run(
        (git, "-C", str(root), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"),
        check=True,
    )
    return subprocess.run(
        (git, "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_path_dependencies_lock_and_load_transitively_without_writes(tmp_path: Path) -> None:
    manifest, top, _ = _path_graph(tmp_path)
    lock = update_project_lock(manifest)
    assert tuple(item.name for item in lock.packages) == ("leaf", "middle")
    assert tuple(
        module.logical_path
        for package in lock.packages
        for module in package.modules
    ) == ("leaf.value", "middle.child")
    rendered = (manifest.parent / "zlang.lock").read_text()
    assert lock.render() == rendered

    before = _snapshot(tmp_path)
    workspace = load_project_workspace(top)
    after = _snapshot(tmp_path)
    assert workspace is not None
    assert before == after
    assert workspace.root_identity_for(top).logical_path == "demo.top"
    closure = workspace.resolver.resolve(("middle.child",), importer="demo.top")
    assert tuple(item.logical_path for item in closure) == (
        "leaf.value",
        "middle.child",
    )
    assert tuple(item.logical_path for item in workspace.dependency_closure.modules) == (
        "leaf.value",
        "middle.child",
    )


def test_path_lock_identity_is_portable_after_tree_relocation(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    manifest, top, _ = _path_graph(first_root)
    first = update_project_lock(manifest)
    second_root = tmp_path / "second"
    shutil.copytree(first_root, second_root)
    second_top = second_root / "root" / "src" / "top.zhl"
    second = load_project_workspace(second_top)
    assert second is not None
    assert second.lock.identity == first.identity
    assert second.dependency_closure.identity == load_project_workspace(top).dependency_closure.identity


@pytest.mark.parametrize("mutation", ("change", "add", "delete"))
def test_path_dependency_dirty_state_is_a_lock_mismatch(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest, top, leaf = _path_graph(tmp_path)
    update_project_lock(manifest)
    value = leaf / "src" / "value.zhl"
    if mutation == "change":
        value.write_text("module LeafValue { in x:u8 out y:u8 y=x ^ 1 }")
    elif mutation == "add":
        _source(leaf, "extra.zhl", "module Extra {}")
    elif mutation == "delete":
        value.unlink()
    with pytest.raises(WorkspaceError, match="dirty"):
        load_project_workspace(top)


def test_dependency_profile_change_is_outside_resolution_identity(tmp_path: Path) -> None:
    manifest, top, leaf = _path_graph(tmp_path)
    first = update_project_lock(manifest)
    with (leaf / "zlang.toml").open("a") as output:
        output.write("\n[profiles.debug]\nbackend = \"systemverilog\"\n")
    workspace = load_project_workspace(top)
    assert workspace is not None
    assert workspace.lock.identity == first.identity


def test_root_profile_change_does_not_change_resolution_identity(tmp_path: Path) -> None:
    manifest, top, _ = _path_graph(tmp_path)
    first = update_project_lock(manifest)
    with manifest.open("a") as output:
        output.write("\n[profiles.debug]\nbackend = \"systemverilog\"\n")
    workspace = load_project_workspace(top)
    assert workspace is not None
    assert workspace.lock.identity == first.identity


def test_lock_detects_dependency_cycle_before_publication(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _manifest(left, "left", 'right = { path = "../right" }\n')
    _source(left, "value.zhl", "module Left {}")
    _manifest(right, "right", 'left = { path = "../left" }\n')
    _source(right, "value.zhl", "module Right {}")
    root = tmp_path / "root"
    manifest = _manifest(root, "root", 'left = { path = "../left" }\n')
    _source(root, "top.zhl", "module Top {}")
    with pytest.raises(WorkspaceError, match="dependency cycle"):
        update_project_lock(manifest)
    assert not (root / "zlang.lock").exists()


def test_failed_update_preserves_previously_published_lock(tmp_path: Path) -> None:
    manifest, _, leaf = _path_graph(tmp_path)
    update_project_lock(manifest)
    lock_path = manifest.parent / "zlang.lock"
    accepted = lock_path.read_bytes()
    _source(leaf, "broken.zhl", "import unknown.module module Broken {}")
    with pytest.raises(WorkspaceError, match="undeclared logical import"):
        update_project_lock(manifest)
    assert lock_path.read_bytes() == accepted


def test_unused_module_import_error_prevents_lock_publication(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _manifest(dependency, "dependency")
    _source(dependency, "used.zhl", "module Used {}")
    _source(
        dependency,
        "unused.zhl",
        "import missing.package module Unused {}",
    )
    root = tmp_path / "root"
    manifest = _manifest(root, "root", 'dependency = { path = "../dependency" }\n')
    _source(root, "top.zhl", "module Top {}")
    with pytest.raises(WorkspaceError, match="undeclared logical import"):
        update_project_lock(manifest)
    assert not (root / "zlang.lock").exists()


def test_overlapping_package_source_roots_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="root"\nversion="1"\nsource-root="."\n'
        '[dependencies]\nnested={path="nested"}\n'
    )
    (root / "top.zhl").write_text("module Top {}")
    nested = root / "nested"
    _manifest(nested, "nested")
    _source(nested, "value.zhl", "module Nested {}")
    with pytest.raises(WorkspaceError, match="source roots overlap"):
        update_project_lock(manifest)


def test_git_dependency_compiles_from_cache_after_origin_disappears(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    origin = tmp_path / "origin"
    _manifest(origin, "vendor")
    _source(origin, "math.zhl", "module VendorMath { in x:u8 out y:u8 y=x }")
    subprocess.run((git, "init", "-q", str(origin)), check=True)
    subprocess.run((git, "-C", str(origin), "config", "user.name", "ZLang Test"), check=True)
    subprocess.run((git, "-C", str(origin), "config", "user.email", "zlang@example.invalid"), check=True)
    subprocess.run((git, "-C", str(origin), "add", "."), check=True)
    subprocess.run(
        (
            git, "-C", str(origin), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "fixture",
        ),
        check=True,
    )
    revision = subprocess.run(
        (git, "-C", str(origin), "rev-parse", "HEAD"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    root = tmp_path / "root"
    manifest = _manifest(
        root,
        "demo",
        f'vendor = {{ git = "{origin.as_posix()}", rev = "{revision}" }}\n',
    )
    top = _source(
        root,
        "top.zhl",
        "import vendor.math module Top { in x:u8 out y:u8 "
        "inst m:VendorMath m.x=x y=m.y }",
    )
    update_project_lock(manifest)
    origin.rename(tmp_path / "origin-unavailable")

    workspace = load_project_workspace(top)
    assert workspace is not None
    assert tuple(item.logical_path for item in workspace.dependency_closure.modules) == (
        "vendor.math",
    )
    cached = tuple((root / ".zlang" / "dependencies").glob("*/src/math.zhl"))
    assert len(cached) == 1
    cache_entry = cached[0].parents[1]
    unavailable = cache_entry.with_name(cache_entry.name + "-missing")
    cache_entry.rename(unavailable)
    with pytest.raises(WorkspaceError, match="unavailable or escapes"):
        load_project_workspace(top)
    unavailable.rename(cache_entry)
    cached = (cache_entry / "src" / "math.zhl",)
    cached[0].write_text("module VendorMath { in x:u8 out y:u8 y=x ^ 1 }")
    with pytest.raises(WorkspaceError, match="dirty"):
        load_project_workspace(top)


def test_git_package_path_dependency_is_explicitly_deferred(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    sibling = tmp_path / "sibling"
    _manifest(sibling, "sibling")
    _source(sibling, "value.zhl", "module Sibling {}")
    origin = tmp_path / "origin"
    _manifest(origin, "vendor", 'sibling = { path = "../sibling" }\n')
    _source(origin, "math.zhl", "module VendorMath {}")
    subprocess.run((git, "init", "-q", str(origin)), check=True)
    subprocess.run((git, "-C", str(origin), "config", "user.name", "ZLang Test"), check=True)
    subprocess.run((git, "-C", str(origin), "config", "user.email", "zlang@example.invalid"), check=True)
    subprocess.run((git, "-C", str(origin), "add", "."), check=True)
    subprocess.run(
        (git, "-C", str(origin), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"),
        check=True,
    )
    revision = subprocess.run(
        (git, "-C", str(origin), "rev-parse", "HEAD"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    root = tmp_path / "root"
    manifest = _manifest(
        root,
        "root",
        f'vendor = {{ git = "{origin.as_posix()}", rev = "{revision}" }}\n',
    )
    _source(root, "top.zhl", "module Top {}")
    with pytest.raises(WorkspaceError, match="cannot use path dependency"):
        update_project_lock(manifest)
    assert not (root / "zlang.lock").exists()


def test_transitive_git_dependencies_load_offline_from_pinned_caches(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git is unavailable")
    leaf = tmp_path / "leaf-origin"
    _manifest(leaf, "leafgit")
    _source(leaf, "value.zhl", "module GitLeaf { in x:u8 out y:u8 y=x }")
    leaf_revision = _commit_git(leaf)

    middle = tmp_path / "middle-origin"
    _manifest(
        middle,
        "middlegit",
        f'leafgit = {{ git = "{leaf.as_posix()}", rev = "{leaf_revision}" }}\n',
    )
    _source(
        middle,
        "value.zhl",
        "import leafgit.value module GitMiddle { in x:u8 out y:u8 "
        "inst leaf:GitLeaf leaf.x=x y=leaf.y }",
    )
    middle_revision = _commit_git(middle)

    root = tmp_path / "root"
    manifest = _manifest(
        root,
        "root",
        f'middlegit = {{ git = "{middle.as_posix()}", rev = "{middle_revision}" }}\n',
    )
    top = _source(
        root,
        "top.zhl",
        "import middlegit.value module Top { in x:u8 out y:u8 "
        "inst middle:GitMiddle middle.x=x y=middle.y }",
    )
    lock = update_project_lock(manifest)
    assert tuple(item.name for item in lock.packages) == ("leafgit", "middlegit")
    leaf.rename(tmp_path / "leaf-unavailable")
    middle.rename(tmp_path / "middle-unavailable")
    workspace = load_project_workspace(top)
    assert workspace is not None
    assert tuple(item.logical_path for item in workspace.dependency_closure.modules) == (
        "leafgit.value",
        "middlegit.value",
    )


def test_project_state_symlink_is_rejected_before_update(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manifest = _manifest(root, "root")
    _source(root, "top.zhl", "module Top {}")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / ".zlang").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(WorkspaceError, match="must not be a symlink"):
        update_project_lock(manifest)
    assert tuple(outside.iterdir()) == ()


def test_missing_lock_and_source_outside_project_fail_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manifest = _manifest(root, "demo")
    top = _source(root, "top.zhl", "module Top {}")
    with pytest.raises(WorkspaceError, match="lock is unavailable"):
        load_project_workspace(top)

    update_project_lock(manifest)
    outside = tmp_path / "outside.zhl"
    outside.write_text("module Outside {}")
    workspace = load_project_workspace(outside, project=manifest)
    assert workspace is not None
    with pytest.raises(WorkspaceError, match="not a module below"):
        workspace.root_identity_for(outside)
