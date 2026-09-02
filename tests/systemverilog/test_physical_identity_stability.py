from __future__ import annotations

from zlang.backend.companions import collect_rom_companions
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.dependencies import (
    DependencyClosure,
    DependencyModuleIdentity,
    LOCK_SCHEMA,
)


_VERBOSE = """
fn widen<type T>(x : u4) -> u8 { extend<8>(x) }

module Top {
    clock clk
    reset rst
    in x : u4
    in address : u1
    out y : u8
    out q : u8

    rom table : rom<u8,2> {
        read_latency 1
        init [1, 2]
    }
    table.read_address = address
    y = widen<T=u8>(x)
    q = table.read_data
}
"""

_CONCISE = _VERBOSE.replace("extend<8>(x)", "extend(x)")


def _compile(source: str, dependency_digest: str):
    closure = DependencyClosure(
        LOCK_SCHEMA,
        "e" * 64,
        (
            DependencyModuleIdentity(
                "dep.Helper",
                dependency_digest,
                "c" * 64,
                "1" * 40,
            ),
        ),
    )
    return compile_source(
        source,
        source_unit="physical-identity.zhl",
        dependency_closure=closure,
        include_clash=False,
    ).ir


def test_dependency_provenance_does_not_leak_into_direct_sv_or_rom_names() -> None:
    verbose = _compile(_VERBOSE, "d" * 64)
    concise = _compile(_CONCISE, "f" * 64)

    # Exact dependency provenance intentionally remains part of semantic,
    # selected/build, and proof identities.
    assert (
        verbose.callable_definitions[0].callee_identity
        != concise.callable_definitions[0].callee_identity
    )

    verbose_artifact = emit_artifact(verbose)
    concise_artifact = emit_artifact(concise)
    assert verbose_artifact.selected_ir_identity != concise_artifact.selected_ir_identity
    assert verbose_artifact.build_identity != concise_artifact.build_identity

    # Physical code and immutable ROM payloads depend only on typed hardware
    # semantics, not on those provenance identities.
    assert verbose_artifact.text == concise_artifact.text
    assert verbose_artifact.artifact_hash == concise_artifact.artifact_hash
    verbose_image, = collect_rom_companions(verbose)
    concise_image, = collect_rom_companions(concise)
    assert verbose_image.logical_path == concise_image.logical_path
    assert verbose_image.text == concise_image.text
    assert verbose_image.file_hash == concise_image.file_hash
