"""Shared strict BackendArtifact JSON codec regressions."""

from __future__ import annotations

import json

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import (
    BackendArtifact,
    MANIFEST_VERSION,
    backend_binding_identity,
)
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.compiler import compile_source


SOURCE = "module Add { in a:u8 in b:u8 out y:u9 y=a+b }"


def _artifacts() -> tuple[BackendArtifact, BackendArtifact]:
    module = compile_source(SOURCE, include_clash=False).ir
    return emit_clash_artifact(module), emit_systemverilog_artifact(module)


@pytest.mark.parametrize("index", (0, 1), ids=("clash", "direct-systemverilog"))
def test_backend_artifact_round_trip_is_byte_identical(index: int) -> None:
    artifact = _artifacts()[index]
    encoded = artifact.to_json()
    restored = BackendArtifact.from_json(encoded)

    assert restored.to_json() == encoded
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.build_identity == artifact.build_identity
    assert tuple(item.source_origin for item in restored.bindings) == tuple(
        item.source_origin for item in artifact.bindings
    )
    assert backend_binding_identity(restored) == backend_binding_identity(artifact)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda data: data.update(extra=True), "unsupported field"),
        (
            lambda data: data.update(
                manifest_version=MANIFEST_VERSION,
                companions=[],
            ),
            "requires manifest version 5",
        ),
        (lambda data: data.update(bindings={}), "bindings must be an array"),
        (
            lambda data: data["bindings"][0].update(extra=True),
            "binding contains unsupported field",
        ),
        (
            lambda data: data["bindings"][0].update(rtl_path=[]),
            "binding rtl_path must be a string",
        ),
        (
            lambda data: data["bindings"][0].update(source_origin=7),
            "source_origin must be an object, string, or null",
        ),
    ),
)
def test_backend_artifact_rejects_malformed_schema(mutate, message: str) -> None:
    data = json.loads(_artifacts()[1].to_json())
    mutate(data)

    with pytest.raises(ValueError, match=message):
        BackendArtifact.from_json(data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("backend", "clash", "backend does not match"),
        ("selected_ir_identity", "wrong-selected", "selected IR identity"),
        ("artifact_hash", "0" * 64, "hash does not match"),
    ),
)
def test_backend_artifact_rejects_binding_artifact_mismatch(
    field: str, value: str, message: str
) -> None:
    data = json.loads(_artifacts()[1].to_json())
    data["bindings"][0][field] = value

    with pytest.raises(ValueError, match=message):
        BackendArtifact.from_json(data)


def test_backend_artifact_rejects_non_object_root() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        BackendArtifact.from_json("[]")


def test_backend_artifact_rejects_unknown_future_version() -> None:
    data = json.loads(_artifacts()[0].to_json())
    data["manifest_version"] = 11

    with pytest.raises(ValueError, match="unsupported backend manifest version"):
        BackendArtifact.from_json(data)
