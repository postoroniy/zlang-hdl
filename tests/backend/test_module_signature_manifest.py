from __future__ import annotations

import hashlib
import json

import pytest

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
        compile_source(DECLARED, top="Add").ir,
        compile_source(LEGACY, top="Add").ir,
    )






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




def test_signature_cannot_be_smuggled_into_an_older_manifest_schema() -> None:
    declared, _ = _modules()
    data = json.loads(emit_systemverilog_artifact(declared).to_json())
    data["manifest_version"] = MANIFEST_VERSION
    data.pop("build_identity")

    with pytest.raises(ValueError, match="requires manifest version"):
        BackendArtifact.from_json(data)
