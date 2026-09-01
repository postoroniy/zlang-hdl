from __future__ import annotations

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_source


def test_backend_artifacts_share_selected_semantic_identity_not_graph_identity() -> None:
    result = compile_source(
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }",
        include_clash=False,
    )
    graph = result.implementation_graph
    assert graph is not None
    clash = emit_clash_artifact(
        result.ir,
        selected_ir_identity=result.selected_ir_identity,
    )
    direct = emit_target_artifact(
        result.ir,
        graph,
        selected_ir_identity=result.selected_ir_identity,
    )

    assert clash.selected_ir_identity == result.selected_ir_identity
    assert direct.selected_ir_identity == result.selected_ir_identity
    assert graph.identity != result.selected_ir_identity
    assert direct.implementation is not None
    assert direct.implementation.graph_identity == graph.identity


def test_interface_contract_changes_build_identity_but_not_rtl_hash() -> None:
    plain = compile_source(
        "module Pass { in a:u8 out y:u8 y=a }",
        include_clash=False,
    )
    named = compile_source(
        """
interface PassIfc { in a:u8 out y:u8 }
module Pass : PassIfc { in a:u8 out y:u8 y=a }
""",
        include_clash=False,
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
