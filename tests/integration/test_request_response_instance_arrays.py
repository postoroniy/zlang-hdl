"""Bounded compile-time arrays of in-order request/response children."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import (
    emit as emit_clash,
    emit_artifact as emit_clash_artifact,
)
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.interfaces import RequestResponseOrdering, RequestResponseRole
from zlang.opt import lower, restore
from zlang.opt.lowering import CanonicalizationError
from zlang.semantic import SemanticError
from zlang.simulate import simulate_request_response_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "hierarchy" / "request_response_instance_array.zhl"
).read_text()


def _module():
    return compile_source(
        SOURCE, top="RequestResponseArrayTop", include_clash=False
    ).ir


def test_array_roles_connections_and_physical_identities_are_exact() -> None:
    module = _module()
    restored = restore(lower(module))
    hierarchy = build_hierarchy_index(restored)

    assert restored == module
    assert [item.instance.name for item in module.elaborated_instances] == [
        "requester[0]", "requester[1]", "responder[0]", "responder[1]",
    ]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 4
    assert len({
        item.specialization_identity
        for item in module.elaborated_instances[:2]
    }) == 1
    assert len({
        item.specialization_identity
        for item in module.elaborated_instances[2:]
    }) == 1
    assert [item.physical_name for item in hierarchy.children_of((module.name,))] == [
        "requester[0]", "requester[1]", "responder[0]", "responder[1]",
    ]
    assert [
        (item.requester, item.responder, item.max_outstanding, item.ordering)
        for item in module.request_response_connections
    ] == [
        ("requester[0]", "responder[0]", 2, RequestResponseOrdering.IN_ORDER),
        ("requester[1]", "responder[1]", 2, RequestResponseOrdering.IN_ORDER),
    ]
    assert len({item.semantic_id for item in module.request_response_connections}) == 2
    assert all(
        child.request_responses[0].role is RequestResponseRole.REQUESTER
        for child in module.children[:2]
    )
    assert all(
        child.request_responses[0].role is RequestResponseRole.RESPONDER
        for child in module.children[2:]
    )


def test_canonical_reused_rr_specialization_rejects_role_drift() -> None:
    canonical = lower(_module())
    second = deepcopy(canonical.children[1])
    interface = second.request_responses[0]
    mutated = replace(
        second,
        request_responses=(
            replace(interface, role=RequestResponseRole.RESPONDER),
        ),
    )
    with pytest.raises(CanonicalizationError, match="specialization identity"):
        restore(replace(
            canonical,
            children=(canonical.children[0], mutated, *canonical.children[2:]),
        ))


def test_array_rejects_out_of_order_and_unindexed_connections() -> None:
    out_of_order = SOURCE.replace(
        "ordering in_order", "ordering out_of_order\n        match_by data"
    )
    with pytest.raises(SemanticError, match="ordering in_order"):
        compile_source(
            out_of_order,
            top="RequestResponseArrayTop",
            include_clash=False,
        )

    unindexed = SOURCE.replace(
        "connect requester[i].mem -> responder[i].mem",
        "connect requester.mem -> responder[i].mem",
    )
    with pytest.raises(SemanticError, match="requires a compile-time index"):
        compile_source(
            unindexed,
            top="RequestResponseArrayTop",
            include_clash=False,
        )

    mixed_protocol = SOURCE.replace(
        "    in consume : bit\n",
        "    in consume : bit\n    in side : rv<u8>\n",
        1,
    ).replace(
        "    response = mem.response.payload.data\n",
        "    response = mem.response.payload.data\n    side.ready = 1\n",
        1,
    )
    with pytest.raises(
        SemanticError,
        match="only scalar wire ports beside the request/response interface",
    ):
        compile_source(
            mixed_protocol,
            top="RequestResponseArrayTop",
            include_clash=False,
        )


def test_each_physical_child_has_an_independent_simulator_ledger() -> None:
    module = _module()
    first = module.children[0]
    second = module.children[1]
    assert first.request_responses[0].role is RequestResponseRole.REQUESTER
    assert second.request_responses[0].role is RequestResponseRole.REQUESTER

    lane0 = simulate_request_response_cycles(
        first,
        [
            {"data": 1, "issue": 1, "consume": 0,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
            {"data": 2, "issue": 1, "consume": 0,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
            {"data": 3, "issue": 1, "consume": 0,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
            {"data": 4, "issue": 0, "consume": 1,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 44}, "valid": 1}}},
        ],
    )
    lane1 = simulate_request_response_cycles(
        second,
        [
            {"data": 9, "issue": 0, "consume": 1,
             "mem": {"request": {"ready": 0},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
            {"data": 9, "issue": 1, "consume": 1,
             "mem": {"request": {"ready": 0},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
            {"data": 9, "issue": 1, "consume": 1,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 99}, "valid": 1}}},
            {"data": 9, "issue": 1, "consume": 1,
             "mem": {"request": {"ready": 1},
                     "response": {"payload": {"data": 0}, "valid": 0}}},
        ],
        reset=(True, False, False, False),
    )

    assert [item["mem"]["outstanding"] for item in lane0] == [0, 1, 2, 2]
    assert [item["mem"]["request"]["transfer"] for item in lane0] == [1, 1, 0, 0]
    assert lane0[-1]["response"] == 44
    assert [item["mem"]["outstanding"] for item in lane1] == [0, 0, 0, 0]
    assert [item["mem"]["request"]["transfer"] for item in lane1] == [0, 0, 1, 1]
    assert lane1[2]["mem"]["response"]["transfer"] == 1


def test_backend_artifacts_are_deterministic_and_bind_each_ledger() -> None:
    module = _module()
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert (first.text, first.artifact_hash, first.to_json()) == (
        second.text, second.artifact_hash, second.to_json()
    )
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.bindings == first.bindings
    clash_first = emit_clash_artifact(module)
    clash_second = emit_clash_artifact(module)
    assert (
        clash_first.text,
        clash_first.artifact_hash,
        clash_first.to_json(),
    ) == (
        clash_second.text,
        clash_second.artifact_hash,
        clash_second.to_json(),
    )
    assert BackendArtifact.from_json(clash_first.to_json()).bindings == (
        clash_first.bindings
    )
    assert first.text.count("module ArrayRequester__") == 1
    assert first.text.count("module ArrayResponder__") == 1
    assert first.text.count("ArrayRequester__") == 3
    assert first.text.count("ArrayResponder__") == 3
    tracker_ids = {
        f"{item.semantic_id}:outstanding"
        for item in module.request_response_connections
    }
    for artifact in (first, clash_first):
        tracker_bindings = {
            item.semantic_signal_id: item
            for item in artifact.bindings
            if item.semantic_signal_id in tracker_ids
        }
        assert set(tracker_bindings) == tracker_ids
        # Production ABIs do not recursively export hidden accounting state.
        # Preserve both semantic bindings but do not fabricate public locators;
        # the two actual ledgers must nevertheless remain distinct in emitted RTL.
        assert all(
            not item.physical_available for item in tracker_bindings.values()
        )
        assert all(item.rtl_path == "" for item in tracker_bindings.values())
    tracker_tokens = [
        line.strip().split()[-1].rstrip(";")
        for line in first.text.splitlines()
        if line.strip().startswith("logic [1:0] rr_")
    ]
    assert len(tracker_tokens) == 2
    assert len(set(tracker_tokens)) == 2
    for artifact in (first, clash_first):
        public = {
            item.semantic_signal_id: item for item in artifact.bindings
            if item.semantic_signal_id in {"port:responses", "port:accepted"}
        }
        assert set(public) == {"port:responses", "port:accepted"}
        assert all(item.physical_available for item in public.values())


BENCH = r"""
module tb;
  logic clk=0, rst=1;
  logic [7:0] request_data [0:1];
  logic issue [0:1], accept [0:1], consume [0:1], produce [0:1];
  logic [7:0] response_data [0:1];
  wire [7:0] responses [0:1];
  wire accepted [0:1];
  RequestResponseArrayTop dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    request_data[0]=8'h10; request_data[1]=8'h20;
    response_data[0]=8'ha0; response_data[1]=8'hb0;
    issue[0]=0; issue[1]=0; accept[0]=0; accept[1]=0;
    consume[0]=0; consume[1]=0; produce[0]=0; produce[1]=0;
    tick; if(accepted[0] || accepted[1]) $fatal(1,"reset transfer");
    rst=0; issue[0]=1; issue[1]=1; accept[0]=1; accept[1]=0;
    #1; if(!accepted[0] || accepted[1]) $fatal(1,"lane isolation"); tick;
    #1; if(!accepted[0] || accepted[1]) $fatal(1,"lane0 second request"); tick;
    #1; if(accepted[0]) $fatal(1,"lane0 outstanding limit");
    accept[1]=1; #1; if(!accepted[1]) $fatal(1,"lane1 independent ledger"); tick;
    issue[0]=0; issue[1]=0; produce[0]=1; produce[1]=1;
    consume[0]=0; consume[1]=1; #1;
    if(responses[0]!==8'ha0 || responses[1]!==8'hb0)
      $fatal(1,"response payload");
    tick;
    consume[0]=1; tick;
    rst=1; issue[0]=1; accept[0]=1; tick;
    if(accepted[0] || accepted[1]) $fatal(1,"mid-stream reset");
    rst=0; produce[0]=0; produce[1]=0; accept[0]=1;
    tick; if(!accepted[0]) $fatal(1,"new reset epoch");
    $finish;
  end
endmodule
"""


def _run_bench(paths: tuple[Path, ...], tmp_path: Path, suffix: str) -> None:
    bench = tmp_path / f"tb_{suffix}.sv"
    bench.write_text(BENCH)
    object_dir = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(object_dir), *(str(path) for path in paths), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(object_dir / "Vtb"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_array_is_strict_lint_clean_and_cycle_exact(tmp_path: Path) -> None:
    rtl = tmp_path / "RequestResponseArrayTop.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((rtl,), "RequestResponseArrayTop")
    _run_bench((rtl,), tmp_path, "direct")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_array_is_strict_lint_clean_and_cycle_exact(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE, top="RequestResponseArrayTop")
    rtl = generate_verilog(
        emit_clash(compilation.ir),
        compilation.ir.name,
        tmp_path / "clash",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    lint_with_verilator(rtl, "RequestResponseArrayTop")
    _run_bench(tuple(rtl), tmp_path, "clash")
