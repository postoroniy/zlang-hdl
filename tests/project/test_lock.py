from __future__ import annotations

from pathlib import Path

import pytest

from zlang.dependencies import (
    DependencyClosure,
    DependencyModelError,
    DependencyModuleIdentity,
    DependencySourceKind,
    LockedModule,
    LockedPackage,
    ResolvedModule,
)
from zlang.project import ProjectLock, ProjectModelError


D0 = "0" * 64
D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
REV = "a" * 40


def _packages() -> tuple[LockedPackage, ...]:
    leaf = LockedPackage(
        "vendor.math",
        "1.0.0",
        DependencySourceKind.PATH,
        "../math",
        None,
        D1,
        (),
        (LockedModule("vendor.math.fixed", "fixed.zl", D2, ("std.math.fixed",)),),
    )
    fft = LockedPackage(
        "vendor.fft",
        "2.0.0",
        DependencySourceKind.GIT,
        "https://example.invalid/fft.git",
        REV,
        D2,
        ("vendor.math",),
        (
            LockedModule("vendor.fft.twiddle", "twiddle.zl", D3, ("vendor.math.fixed",)),
            LockedModule("vendor.fft.core", "core.zl", D1, ("vendor.fft.twiddle",)),
        ),
    )
    return fft, leaf


def test_lock_parse_render_round_trip_and_identity_are_deterministic() -> None:
    lock = ProjectLock(1, D0, _packages())
    # Construction normalizes package and module order, independent of insertion.
    assert tuple(item.name for item in lock.packages) == ("vendor.fft", "vendor.math")
    assert tuple(item.logical_path for item in lock.package("vendor.fft").modules) == (
        "vendor.fft.core",
        "vendor.fft.twiddle",
    )

    rendered = lock.render()
    restored = ProjectLock.parse(rendered)
    assert restored == lock
    assert restored.render() == rendered
    assert restored.identity == lock.identity
    assert ProjectLock.from_data(lock.to_data()) == lock
    assert lock.module("vendor.math.fixed").digest == D2
    assert lock.module("vendor.math.fixed").dependencies == ("std.math.fixed",)
    assert lock.package("vendor.math").package_identity == lock.package("vendor.math").identity
    assert "/home/" not in rendered


def test_lock_identity_changes_for_every_exact_record_family() -> None:
    original = ProjectLock(1, D0, _packages())
    changed_manifest = ProjectLock(1, D3, _packages())
    changed_module = ProjectLock(
        1,
        D0,
        (
            _packages()[0],
            LockedPackage(
                "vendor.math", "1.0.0", DependencySourceKind.PATH, "../math",
                None, D1, (), (LockedModule("vendor.math.fixed", "fixed.zl", D3),),
            ),
        ),
    )
    assert original.identity != changed_manifest.identity
    assert original.identity != changed_module.identity


@pytest.mark.parametrize("second_name", ("vendor.math", "VENDOR.MATH"))
def test_lock_rejects_duplicate_or_case_colliding_packages(second_name: str) -> None:
    first = _packages()[1]
    second = LockedPackage(
        second_name, "1", DependencySourceKind.PATH, "../other", None, D2,
    )
    with pytest.raises(ProjectModelError, match="duplicate locked package"):
        ProjectLock(1, D0, (first, second))


def test_lock_rejects_duplicate_module_index_across_packages() -> None:
    first = _packages()[1]
    duplicate = LockedPackage(
        "vendor", "1", DependencySourceKind.PATH, "../other", None, D2,
        (), (LockedModule("vendor.math.fixed", "different.zl", D3),),
    )
    with pytest.raises(ProjectModelError, match="duplicate locked module"):
        ProjectLock(1, D0, (first, duplicate))


def test_locked_package_rejects_duplicate_relative_or_logical_modules() -> None:
    with pytest.raises(DependencyModelError, match="duplicate locked module relative path"):
        LockedPackage(
            "vendor.math", "1", DependencySourceKind.PATH, "../math", None, D1, (),
            (
                LockedModule("vendor.math.a", "same.zl", D2),
                LockedModule("vendor.math.b", "same.zl", D3),
            ),
        )
    with pytest.raises(DependencyModelError, match="duplicate locked module"):
        LockedPackage(
            "vendor.math", "1", DependencySourceKind.PATH, "../math", None, D1, (),
            (
                LockedModule("vendor.math.a", "a.zl", D2),
                LockedModule("VENDOR.MATH.A", "b.zl", D3),
            ),
        )


def test_locked_package_rejects_module_outside_its_namespace() -> None:
    with pytest.raises(DependencyModelError, match="outside package"):
        LockedPackage(
            "vendor.math", "1", DependencySourceKind.PATH, "../math", None, D1,
            (), (LockedModule("other.math", "math.zl", D2),),
        )


def test_lock_rejects_missing_dependency_and_cycles() -> None:
    missing = LockedPackage(
        "vendor.a", "1", DependencySourceKind.PATH, "../a", None, D1,
        ("vendor.missing",), (),
    )
    with pytest.raises(ProjectModelError, match="unavailable package 'vendor.missing'"):
        ProjectLock(1, D0, (missing,))

    a = LockedPackage(
        "vendor.a", "1", DependencySourceKind.PATH, "../a", None, D1,
        ("vendor.b",), (),
    )
    b = LockedPackage(
        "vendor.b", "1", DependencySourceKind.PATH, "../b", None, D2,
        ("vendor.a",), (),
    )
    with pytest.raises(ProjectModelError, match=r"vendor\.a -> vendor\.b -> vendor\.a"):
        ProjectLock(1, D0, (a, b))


def test_lock_parser_rejects_unknown_keys_and_invalid_exact_fields() -> None:
    rendered = ProjectLock(1, D0, _packages()).render()
    with pytest.raises(ProjectModelError, match="unknown locked package key 'mystery'"):
        ProjectLock.parse(rendered.replace('version = "2.0.0"', 'version = "2.0.0"\nmystery = 1'))
    with pytest.raises(ProjectModelError, match="complete lowercase"):
        ProjectLock.parse(rendered.replace(f'revision = "{REV}"', 'revision = "main"'))
    with pytest.raises(ProjectModelError, match="manifest resolution digest"):
        ProjectLock.parse(rendered.replace(D0, "bad", 1))


def test_lock_load_is_read_only_and_missing_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "zlang.lock"
    path.write_text(ProjectLock(1, D0, _packages()).render())
    before = path.stat().st_mtime_ns
    assert ProjectLock.load(path).identity == ProjectLock(1, D0, _packages()).identity
    assert path.stat().st_mtime_ns == before
    with pytest.raises(ProjectModelError, match="lock is unavailable"):
        ProjectLock.load(tmp_path / "missing.lock")


def test_dependency_closure_and_module_identity_round_trip_stably() -> None:
    modules = (
        DependencyModuleIdentity("vendor.math.fixed", D2, D1),
        DependencyModuleIdentity("vendor.fft.core", D1, D2, REV),
    )
    closure = DependencyClosure(1, D0, tuple(reversed(modules)))
    assert tuple(item.logical_path for item in closure.modules) == (
        "vendor.fft.core",
        "vendor.math.fixed",
    )
    restored = DependencyClosure.from_data(closure.to_data())
    assert restored == closure
    assert restored.identity == closure.identity
    assert DependencyClosure(1, D0, modules).identity == closure.identity


def test_dependency_closure_rejects_duplicate_modules() -> None:
    module = DependencyModuleIdentity("vendor.math.fixed", D2, D1)
    with pytest.raises(DependencyModelError, match="duplicate closure module"):
        DependencyClosure(1, D0, (module, module))


def test_resolved_module_matches_small_resolver_facing_shape() -> None:
    resolved = ResolvedModule(
        "vendor.math.fixed",
        Path("/cache/vendor/math/fixed.zl"),
        object(),
        D2,
        ("std.math.fixed",),
        D1,
    )
    assert resolved.dependencies == ("std.math.fixed",)
    assert resolved.imports == resolved.dependencies
    assert resolved.identity == DependencyModuleIdentity("vendor.math.fixed", D2, D1)
