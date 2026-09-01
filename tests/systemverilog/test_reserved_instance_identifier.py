from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = """
module Child {
    in x:u8
    out y:u8
    y=x
}

module Top {
    in x:u8
    out y:u8
    inst table:Child { x }
    y=table.y
}
"""


def _compile():
    return compile_source(SOURCE, include_clash=False)


def test_reserved_instance_name_is_mangled_without_changing_semantic_identity() -> None:
    first = _compile()
    repeated = _compile()
    assert first.high_level_ir_identity == repeated.high_level_ir_identity
    assert first.selected_ir_identity == repeated.selected_ir_identity
    assert first.ir.elaborated_instances[0].instance.name == "table"
    assert first.ir.elaborated_instances[0].semantic_path == ("Top", "table")

    artifact = emit_artifact(
        first.ir, selected_ir_identity=first.selected_ir_identity
    )
    repeated_artifact = emit_artifact(
        repeated.ir, selected_ir_identity=repeated.selected_ir_identity
    )
    assert artifact.text == repeated_artifact.text
    assert artifact.artifact_hash == repeated_artifact.artifact_hash
    assert " zlang_table (" in artifact.text
    assert " table (" not in artifact.text
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.to_json() == artifact.to_json()


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_reserved_instance_name_is_strict_verilator_clean(tmp_path: Path) -> None:
    compilation = _compile()
    artifact = emit_artifact(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    rtl = tmp_path / "Top.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "Top")
