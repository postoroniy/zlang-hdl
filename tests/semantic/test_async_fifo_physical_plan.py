from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zlang.async_fifo import build_async_fifo_physical_plan
from zlang.compiler import compile_source
from zlang.ir.storage import MemoryPortKind, MemoryResetPolicy
from zlang.targets import TargetArchitectureError, select_implementation_graph
from zlang.backend.systemverilog.target import emit_target


SOURCE = Path("examples/cdc_async_fifo.zhl").read_text(encoding="utf-8")
NATIVE_SOURCE = SOURCE.replace("rv<u8>", "rv<bits<9>>").replace(
    "async_fifo(4)", "async_fifo(1024)"
)


def _plan(source: str = SOURCE):
    module = compile_source(source, top="CdcAsyncFifo").ir
    return build_async_fifo_physical_plan(module, module.connections[0])


def test_async_fifo_decomposes_to_stable_typed_registered_1w1r_memory() -> None:
    first = _plan()
    assert first == _plan()
    assert first.memory.async_memory
    assert first.memory.read_latency == 1
    assert first.memory.contents_reset is MemoryResetPolicy.PRESERVE
    assert first.memory.read_data_reset is MemoryResetPolicy.CLEAR
    assert tuple(port.kind for port in first.memory.ports) == (
        MemoryPortKind.WRITE,
        MemoryPortKind.READ,
    )
    assert first.memory.ports[0].domain != first.memory.ports[1].domain
    assert first.memory.ports[1].read_enable is not None
    assert first.prefetch_policy == "one_slot_registered_read_v1"
    assert len(first.registers) == 10
    assert {item.domain for item in first.registers} == {
        first.source_domain,
        first.destination_domain,
    }
    assert len({item.identity for item in first.registers}) == 10


def test_async_fifo_physical_identity_tracks_depth_and_prefetch_policy() -> None:
    first = _plan()
    deeper = _plan(SOURCE.replace("async_fifo(4)", "async_fifo(8)"))
    assert first.identity != deeper.identity
    with pytest.raises(ValueError, match="prefetch policy"):
        replace(first, prefetch_policy="unsafe_first_sync_stage").validate()
    with pytest.raises(ValueError, match="one exact read cycle"):
        replace(first, memory=replace(first.memory, read_latency=2)).validate()
    with pytest.raises(ValueError, match="state layout"):
        replace(first, registers=first.registers[:-1]).validate()
    with pytest.raises(ValueError, match="controller equations"):
        replace(
            first,
            controller=replace(
                first.controller, equations=first.controller.equations[:-1]
            ),
        ).validate()


def test_fifo_controller_equations_drive_both_model_and_sv() -> None:
    controller = _plan().controller
    inputs = {
        "zlang_source_valid": 0,
        "zlang_destination_ready": 0,
        "zlang_source_reset": 0,
        "zlang_destination_reset": 0,
        "zlang_write_binary": 0,
        "zlang_write_gray": 0,
        "zlang_read_binary": 0,
        "zlang_read_gray": 0,
        "zlang_read_gray_sync2": 0,
        "zlang_write_gray_sync2": 3,
        "zlang_full": 0,
        "zlang_output_valid": 0,
    }
    first_fetch = controller.evaluate(inputs)
    assert first_fetch["zlang_fifo_prefetch"] == 1
    assert first_fetch["zlang_read_binary_next"] == 0
    stalled = controller.evaluate({**inputs, "zlang_output_valid": 1})
    assert stalled["zlang_fifo_prefetch"] == 0
    accepted = controller.evaluate(
        {**inputs, "zlang_output_valid": 1, "zlang_destination_ready": 1}
    )
    assert accepted["zlang_pop"] == 1
    assert accepted["zlang_read_binary_next"] == 1
    assert accepted["zlang_fifo_prefetch"] == 1
    assert (
        controller.evaluate({**inputs, "zlang_destination_reset": 1})[
            "zlang_fifo_prefetch"
        ]
        == 0
    )
    assert (
        controller.evaluate({**inputs, "zlang_write_binary": 4})["zlang_full_next"] == 1
    )

    rendered = controller.render_sv(
        {
            "zlang_source_reset": "rst_wr",
            "zlang_destination_reset": "rst_rd",
        }
    )
    assert len(rendered) == len(controller.equations)
    assert "assign zlang_fifo_prefetch" in "\n".join(rendered)
    assert "assign zlang_full_next" in "\n".join(rendered)
    assert "rst_rd" in "\n".join(rendered)


def test_amd7_native_fifo_storage_binding_is_exact_and_deterministic() -> None:
    module = compile_source(NATIVE_SOURCE, top="CdcAsyncFifo").ir
    graphs = tuple(
        select_implementation_graph(
            module,
            target="xc7z030ffg676-1",
            architecture="Xilinx7AsyncFifoRAMB18",
            mode="required",
        )
        for _ in range(2)
    )
    assert graphs[0] == graphs[1]
    assert graphs[0].latency == 1
    assert graphs[0].initiation_interval == 1
    assert graphs[0].semantic_region_identity == _plan(NATIVE_SOURCE).identity
    config = dict(graphs[0].resources[0].configuration)
    assert config["dorega"] == 0
    assert config["fifo_memory"] == 1
    emitted = emit_target(module, graphs[0])
    assert '(* ram_style = "block" *)' in emitted
    assert "fifo_storage_cells" in emitted
    assert "zlang_fifo_prefetch" in emitted
    assert "fifo_storage_rd_read_data <= fifo_storage_cells" in emitted


def test_amd7_native_fifo_binding_rejects_unsupported_shape_but_preferred_falls_back() -> (
    None
):
    module = compile_source(SOURCE, top="CdcAsyncFifo").ir
    with pytest.raises(TargetArchitectureError, match="does not support width 8"):
        select_implementation_graph(
            module,
            target="xc7z030ffg676-1",
            architecture="Xilinx7AsyncFifoRAMB18",
            mode="required",
        )
    fallback = select_implementation_graph(
        module,
        target="xc7z030ffg676-1",
        architecture="Xilinx7AsyncFifoRAMB18",
        mode="preferred",
    )
    assert fallback.is_generic
    assert any(
        "preferred architecture rejected" in item for item in fallback.legality_evidence
    )
