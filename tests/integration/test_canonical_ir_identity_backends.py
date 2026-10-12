from __future__ import annotations

from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_source




def test_interface_contract_changes_build_identity_but_not_rtl_hash() -> None:
    plain = compile_source(
        "module Pass { in a:u8 out y:u8 y=a }",
    )
    named = compile_source(
        """
interface PassIfc { in a:u8 out y:u8 }
module Pass : PassIfc { in a:u8 out y:u8 y=a }
""",
    )
    plain_artifact = emit_target_artifact(
        plain.ir,
        plain.implementation_graph,
        selected_ir_identity=plain.selected_ir_identity,
    )
    named_artifact = emit_target_artifact(
        named.ir,
        named.implementation_graph,
        selected_ir_identity=named.selected_ir_identity,
    )

    assert plain_artifact.text == named_artifact.text
    assert plain_artifact.artifact_hash == named_artifact.artifact_hash
    assert plain_artifact.selected_ir_identity != named_artifact.selected_ir_identity
    assert plain_artifact.build_identity != named_artifact.build_identity
