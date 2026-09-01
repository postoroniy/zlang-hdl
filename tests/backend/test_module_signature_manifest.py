from __future__ import annotations

import hashlib
import json

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import (
    BackendArtifact,
    MANIFEST_VERSION,
    MODULE_SIGNATURE_MANIFEST_VERSION,
)
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.compiler import compile_source


DECLARED = """
interface AddIfc {
    in a : u8
    in b : u8
    out y : u9
}

module Add : AddIfc {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""

LEGACY = """
module Add {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""


def _modules():
    return (
        compile_source(DECLARED, top="Add", include_clash=False).ir,
        compile_source(LEGACY, top="Add", include_clash=False).ir,
    )


@pytest.mark.parametrize(
    "emit", (emit_clash_artifact, emit_systemverilog_artifact)
)
def test_named_signature_is_metadata_only_and_round_trips(emit) -> None:
    declared, legacy = _modules()
    declared_artifact = emit(declared)
    legacy_artifact = emit(legacy)

    assert declared_artifact.text == legacy_artifact.text
    assert declared_artifact.artifact_hash == legacy_artifact.artifact_hash
    assert declared_artifact.artifact_hash == hashlib.sha256(
        declared_artifact.text.encode()
    ).hexdigest()
    assert declared_artifact.manifest_version >= MODULE_SIGNATURE_MANIFEST_VERSION
    assert declared_artifact.module_signature is not None
    assert declared_artifact.module_signature.nominal_identity == (
        declared.module_signature.nominal_identity
    )
    assert declared_artifact.module_signature.applied_identity == (
        declared.module_signature.identity
    )
    assert declared_artifact.module_signature.signature_data == (
        declared.module_signature.to_data()
    )

    restored = BackendArtifact.from_json(declared_artifact.to_json())
    assert restored.module_signature == declared_artifact.module_signature
    assert restored.build_identity == declared_artifact.build_identity
    assert restored.artifact_hash == declared_artifact.artifact_hash

    # The public contract participates in the semantic build identity, never
    # in the hash of emitted RTL text.
    assert declared_artifact.build_identity != legacy_artifact.build_identity


@pytest.mark.parametrize(
    "emit", (emit_clash_artifact, emit_systemverilog_artifact)
)
def test_absent_signature_preserves_the_legacy_manifest_shape(emit) -> None:
    _, legacy = _modules()
    artifact = emit(legacy)
    data = json.loads(artifact.to_json())

    assert artifact.module_signature is None
    assert artifact.manifest_version == MANIFEST_VERSION
    assert "module_signature" not in data
    assert artifact.artifact_hash == hashlib.sha256(artifact.text.encode()).hexdigest()


@pytest.mark.parametrize("field", ("nominal_identity", "applied_identity"))
def test_tampered_signature_identity_is_rejected(field: str) -> None:
    declared, _ = _modules()
    artifact = emit_systemverilog_artifact(declared)
    data = json.loads(artifact.to_json())
    data["module_signature"][field] = "0" * 64
    # Avoid the outer identity being the first diagnostic: both layers must be
    # independently validated.
    data.pop("build_identity")

    with pytest.raises(ValueError, match=f"{field.removesuffix('_identity')} identity"):
        BackendArtifact.from_json(data)


def test_tampered_signature_data_is_rejected() -> None:
    declared, _ = _modules()
    artifact = emit_systemverilog_artifact(declared)
    data = json.loads(artifact.to_json())
    data["module_signature"]["signature"]["ports"][0]["type"] = "u9"
    data.pop("build_identity")

    with pytest.raises(ValueError, match="applied identity"):
        BackendArtifact.from_json(data)


def test_signature_metadata_is_backend_independent() -> None:
    declared, _ = _modules()
    clash = emit_clash_artifact(declared)
    systemverilog = emit_systemverilog_artifact(declared)

    assert clash.module_signature == systemverilog.module_signature
    assert clash.module_signature is not None
    assert clash.module_signature.signature_data["ports"] == [
        {
            "capacity": None,
            "direction": "input",
            "domain": None,
            "name": "a",
            "protocol": "wire",
            "type": "u8",
            "virtual_channels": None,
        },
        {
            "capacity": None,
            "direction": "input",
            "domain": None,
            "name": "b",
            "protocol": "wire",
            "type": "u8",
            "virtual_channels": None,
        },
        {
            "capacity": None,
            "direction": "output",
            "domain": None,
            "name": "y",
            "protocol": "wire",
            "type": "u9",
            "virtual_channels": None,
        },
    ]


def test_signature_cannot_be_smuggled_into_an_older_manifest_schema() -> None:
    declared, _ = _modules()
    data = json.loads(emit_systemverilog_artifact(declared).to_json())
    data["manifest_version"] = MANIFEST_VERSION
    data.pop("build_identity")

    with pytest.raises(ValueError, match="requires manifest version"):
        BackendArtifact.from_json(data)


def test_applied_type_and_value_parameters_are_published_in_full() -> None:
    result = compile_source(
        """
interface PairIfc<type T, N=2> { in x:T out y:vec<N,T> }
module Pair<type T, N=2> : PairIfc<T,N> {
    in x:T
    out y:vec<N,T>
    y=generate(i in 0..N) x
}
module Top {
    in x:u8
    out y:vec<3,u8>
    inst pair:Pair<T=u8,N=3> { x=x }
    y=pair.y
}
""",
        include_clash=False,
    )
    child = result.ir.children[0]
    clash = emit_clash_artifact(child)
    systemverilog = emit_systemverilog_artifact(child)

    assert clash.module_signature == systemverilog.module_signature
    assert clash.module_signature is not None
    assert clash.module_signature.signature_data["parameters"] == [
        {
            "declared_default": None,
            "kind": "type",
            "name": "T",
            "value": "u8",
        },
        {
            "declared_default": 2,
            "kind": "value",
            "name": "N",
            "value": 3,
        },
    ]
    assert BackendArtifact.from_json(clash.to_json()).module_signature == (
        clash.module_signature
    )
