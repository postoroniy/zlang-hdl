from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact, emit_target
from zlang.backend.systemverilog import emit_formal_artifact as emit_sv_formal_artifact
from zlang.backend.systemverilog.emitter import SystemVerilogEmissionError
from zlang.compilation_session import CompilationSession
from zlang.equivalence import publish_bindings
from zlang.formal import (
    build_formal_design,
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    emit_sby,
)
from zlang.formal_exploration import FormalPolicy
from zlang.exploration import TransformFamily
from zlang.implementation_request import (
    ImplementationContribution,
    PolicyOrigin,
    TransformPolicy,
)
from zlang.ir.equivalence import BindingSide
from zlang.ir.formal import (
    FormalStatus,
    ProofMode,
    connect_formal_design as connect_formal_design_low_level,
    generate_properties,
)
from zlang.semantic import SemanticError


ASYNC_COUNTER = """
module AsyncCounter {
    clock clk
    async reset arst @clk
    in x : u8
    out y : u8
    reg count : u8 = 0
    when 1 { count <- x }
    y = count
}
"""


ASYNC_EXPLORE = """
module AsyncExplore {
    clock clk
    async reset arst @clk
    in a : vec<4,u3>
    in b : vec<4,u3>
    out y : u8
    y = implement {
        dot(a,b)
        intent { latency >= 1 dsp <= 4 minimize lut }
    }
}
"""


ASYNC_PLAIN_VALUE = """
module AsyncPlainValue {
    clock clk
    async reset arst @clk
    in a, b, c : u8
    out y : u10
    y = a + b + c
}
"""


def _counter():
    return CompilationSession(ASYNC_COUNTER).selected_ir


def _formal_artifact(module):
    return emit_sv_formal_artifact(
        module,
        build_recursive_formal_design(module),
        selected_ir_identity=f"selected:{module.name}",
    )


def test_m35_harness_uses_the_exact_safe_async_reset_contract() -> None:
    module = _counter()
    design = build_formal_design(module)
    assert design.non_executable_reason is None

    connected = connect_formal_design(design, _formal_artifact(module))
    assert connected.connected_backend == "direct_systemverilog"
    assert connected.non_executable_reason is None
    harness = emit_harness(connected)
    assert "(* ASYNC_REG = \"TRUE\" *) reg [1:0] zlang_formal_reset_release" in harness
    assert "always @(posedge clk or posedge arst)" in harness
    assert "assign zlang_formal_reset_active = arst ||" in harness
    assert "initial assume(arst);" in harness
    assert "end else if (zlang_formal_reset_active) begin" in harness
    assert "if (!zlang_formal_reset_active) assert" in harness
    # State history is cleared through the effective reset epoch, while the
    # reset-epoch invariant needs an independent first-sample guard.  Otherwise
    # ``$past(reset_active) -> state == reset_value`` is vacuous forever.
    assert "reg zlang_m35_reset_past_valid = 1'b0;" in harness
    assert "zlang_m35_reset_past_valid <= 1'b1;" in harness
    assert (
        "if (zlang_m35_reset_past_valid) assert "
        "((!($past(zlang_formal_reset_active))" in harness
    )
    assert "non-executable property report" not in harness
    assert "mode bmc" in emit_sby(connected)


def test_low_level_m35_connector_rejects_an_exact_reset_contract_mismatch() -> None:
    legacy_module = CompilationSession(
        ASYNC_COUNTER.replace("async reset arst @clk", "reset arst"),
    ).selected_ir
    executable = generate_properties(legacy_module)
    assert executable.non_executable_reason is None

    async_artifact = _formal_artifact(_counter())
    connected = connect_formal_design_low_level(executable, async_artifact)
    assert "unsupported or unresolved physical domains" in (
        connected.non_executable_reason or ""
    )
    assert all(
        "source and backend physical reset contracts do not match"
        in (item.non_executable_reason or "")
        for item in connected.properties
    )
    assert connected.connected_backend is None
    assert connected.connected_artifact_hash is None
    assert connected.implementation_text is None
    assert connected.dut_ports == ()

    # A low-level caller must not clear the semantic gate merely by supplying
    # an otherwise connectable legacy artifact.
    already_guarded = build_formal_design(_counter())
    legacy_artifact = _formal_artifact(legacy_module)
    rejected = connect_formal_design_low_level(already_guarded, legacy_artifact)
    assert rejected.connected_backend is None
    assert all(
        "requires an exact non-default physical domain manifest"
        in (item.non_executable_reason or "")
        for item in rejected.properties
    )








class _BoundAsyncVerifier:
    formal_route = "M36_direct_systemverilog"

    def __init__(self) -> None:
        self.calls: list[FormalPolicy] = []

    @staticmethod
    def _identity(candidate: object) -> dict[str, str]:
        implementation = getattr(candidate, "implementation_identity")
        return {
            "property_identity": f"m36.async.{implementation}",
            "reference_artifact_hash": "a" * 64,
            "implementation_artifact_hash": "b" * 64,
            "artifact_hash": "b" * 64,
            "harness_hash": "c" * 64,
            "backend_identity": "d" * 64,
            "assumptions_identity": "e" * 64,
        }

    def cache_identity(self, candidate: object, _config: object) -> dict[str, str]:
        return self._identity(candidate)

    def __call__(self, candidate: object, config: object) -> dict[str, object]:
        policy = FormalPolicy(getattr(config, "policy"))
        self.calls.append(policy)
        prove = policy is FormalPolicy.REQUIRED_PROVEN
        return {
            "status": FormalStatus.PROVEN if prove else FormalStatus.BOUNDED_PASS,
            "mode": ProofMode.PROVE if prove else ProofMode.BMC,
            "depth": getattr(config, "bmc_depth"),
            "backend": "direct_systemverilog",
            "engine": "sby",
            "solver": "z3",
            **self._identity(candidate),
        }


def test_m39_available_and_required_modes_use_the_async_m36_route() -> None:
    verifier = _BoundAsyncVerifier()

    available = CompilationSession(
        ASYNC_EXPLORE,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=verifier,
    )
    records = available.materialize().exploration_results[0].formal_records
    assert len(records) > 1
    assert records[0].status is FormalStatus.BOUNDED_PASS
    assert records[0].eligible
    assert records[0].cache_state == "executed"
    assert records[0].formal_route == "M36_direct_systemverilog"
    assert all(item.status is None for item in records[1:])
    # One canonical ``implement`` region has one M39 site; planner metadata
    # is not re-proved as standalone pipeline sites.
    assert verifier.calls == [FormalPolicy.AVAILABLE]

    required_bmc = _BoundAsyncVerifier()
    result = CompilationSession(
        ASYNC_EXPLORE,
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_verifier=required_bmc,
    ).materialize()
    assert result.exploration_results[0].formal_records[0].eligible
    assert required_bmc.calls == [FormalPolicy.REQUIRED_BMC]

    required_proven = _BoundAsyncVerifier()
    result = CompilationSession(
        ASYNC_EXPLORE,
        formal_policy=FormalPolicy.REQUIRED_PROVEN,
        formal_verifier=required_proven,
    ).materialize()
    required_records = result.exploration_results[0].formal_records
    assert [item.status for item in required_records] == [
        FormalStatus.BOUNDED_PASS,
        FormalStatus.PROVEN,
    ]
    assert required_records[-1].eligible
    assert required_proven.calls == [
        FormalPolicy.REQUIRED_BMC,
        FormalPolicy.REQUIRED_PROVEN,
    ]


def test_m39_external_regions_use_the_async_m36_route() -> None:

    def contribution(policy: FormalPolicy) -> ImplementationContribution:
        return ImplementationContribution(
            PolicyOrigin("test external profile"),
            transforms=TransformPolicy((TransformFamily.REASSOCIATE,)),
            formal_policy=policy,
        )

    verifier = _BoundAsyncVerifier()
    available = CompilationSession(
        ASYNC_PLAIN_VALUE,
        implementation_contributions=(contribution(FormalPolicy.AVAILABLE),),
        formal_verifier=verifier,
    ).materialize()
    assert len(available.exploration_results) == 1
    records = available.exploration_results[0].formal_records
    assert len(records) == 1
    assert records[0].status is FormalStatus.BOUNDED_PASS
    assert records[0].eligible
    assert records[0].cache_state == "executed"
    assert records[0].formal_route == "M36_direct_systemverilog"
    assert verifier.calls == [FormalPolicy.AVAILABLE]

    required = _BoundAsyncVerifier()
    result = CompilationSession(
        ASYNC_PLAIN_VALUE,
        implementation_contributions=(contribution(FormalPolicy.REQUIRED_BMC),),
        formal_verifier=required,
    ).materialize()
    assert result.exploration_results[0].formal_records[0].eligible
    assert required.calls == [FormalPolicy.REQUIRED_BMC]

def test_selected_bram_emission_fails_before_physical_rtl_publication() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "target_bram_memory.zhl"
    ).read_text().replace("reset rst", "async reset rst @clk")
    compilation = CompilationSession(
        source,
        target="xc7z030ffg676-1",
        architecture="Xilinx7BRAM36SimpleDualPort",
        architecture_mode="required",
    )
    with pytest.raises(
        SystemVerilogEmissionError,
        match="DSP/BRAM resource emission.*legacy rising-edge synchronous",
    ):
        emit_target(
            compilation.selected_ir,
            compilation.planning.implementation_graph,
        )


def test_selected_dsp48_emission_fails_before_physical_rtl_publication() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "symmetric_fixed_fir.zhl"
    ).read_text().replace("reset rst", "async reset rst @clk")
    compilation = CompilationSession(
        source,
        target="xc7z030ffg676-1",
        architecture="Xilinx7SymmetricDSPCascade",
        architecture_mode="required",
    )
    graph = compilation.planning.implementation_graph
    assert not graph.is_generic
    assert graph.resources
    with pytest.raises(
        SystemVerilogEmissionError,
        match="DSP/BRAM resource emission.*legacy rising-edge synchronous",
    ):
        emit_target(compilation.selected_ir, graph)


def test_elastic_pipeline_rejects_safe_async_reset_before_planning() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "elastic_pipeline_auto.zhl"
    ).read_text().replace("reset rst", "async reset rst @clk")
    session = CompilationSession(source)

    with patch(
        "zlang.compilation_session.plan_backend_implementations",
        side_effect=AssertionError("elastic reset rejection must precede planning"),
    ) as planner:
        with pytest.raises(
            SemanticError,
            match="elastic pipeline requires a supported direct-SV clock/reset",
        ):
            session.materialize()
    planner.assert_not_called()


def test_multidomain_cdc_rejects_nondefault_reset_before_emission() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "cdc_async_fifo.zhl"
    ).read_text().replace(
        "reset source_reset @ source_clock",
        "async reset source_reset @ source_clock",
    )
    with pytest.raises(
        SemanticError,
        match="multi-domain asynchronous reset is not supported",
    ):
        CompilationSession(source).selected_ir
