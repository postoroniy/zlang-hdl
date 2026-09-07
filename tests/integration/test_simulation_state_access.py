from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.backend.naming import module_rtl_names
from zlang.backend.systemverilog.simulation_state import (
    SystemVerilogSimulationStateBundle,
    build_systemverilog_simulation_state_bundle,
)
from zlang.compiler import compile_source
from zlang.opt import OptimizationStage, canonical_ir_identity, lower, restore
from zlang.simulation_state import (
    SimulationStateCatalog,
    SimulationStateError,
    SimulationStateKind,
    SimulationStateSession,
    build_simulation_state_catalog,
)


SOURCE = """
module ReplayMemories {
    clock clk reset rst
    in read_address : u7
    out command : bits<1067>
    out command_matches_address : bit

    memory commands : mem<bits<1067>,128> {
        read_latency 0
        collision read_first
        reset {
            contents preserve
            read_data preserve
        }
    }
    commands.read_address = read_address
    commands.write_enable = 0
    commands.write_address = 0
    commands.write_data = 0
    command = commands.read_data
    command_matches_address = (
        commands.read_data[6:0] == bitcast<bits<7>>(read_address)
    )
}

module SampledMemory {
    clock clk reset rst
    out unused : u16
    memory sampled : mem<u16,2> {
        read_latency 1
        collision read_first
    }
    sampled.read_address = 0
    sampled.write_enable = 0
    sampled.write_address = 0
    sampled.write_data = 0
    unused = sampled.read_data
}

module StateReplay {
    clock clk reset rst
    in run : bit
    out cursor : u7
    out completed : u16
    out first_two : vec<2,u16>

    reg command_index : u7 = 0
    reg completion_count : u16 = 0
    reg tag_counts : vec<256,u16> = repeat(0)

    storage : ReplayMemories
    history : SampledMemory
    storage.read_address = command_index

    current_tag = extend<8>(command_index)
    next_tag_count = truncate<16>(tag_counts[current_tag] + 1)
    when run {
        tag_counts[current_tag] <- next_tag_count
        command_index <- truncate<7>(command_index + 1)
        completion_count <- truncate<16>(
            completion_count + bitcast<u1>(storage.command_matches_address)
        )
    }

    cursor = command_index
    completed = completion_count
    first_two = generate(i in 0..2) tag_counts[extend<8>(i)]
}
"""


def _compile():
    return compile_source(SOURCE, include_clash=False)


def _binding(catalog, kind, name, path=("StateReplay",)):
    return catalog.resolve(
        physical_instance_path=path,
        object_kind=kind,
        object_name=name,
    )


def test_catalog_round_trip_identity_shape_and_stale_rejection() -> None:
    compilation = _compile()
    catalog = build_simulation_state_catalog(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    restored = SimulationStateCatalog.from_json(catalog.to_json())
    canonical_module = restore(compilation.optimization_ir)
    canonical_catalog = build_simulation_state_catalog(
        canonical_module,
        selected_ir_identity=compilation.selected_ir_identity,
    )

    assert restored == catalog == canonical_catalog
    without_origins = replace(
        catalog,
        bindings=tuple(
            replace(binding, source_origin=None) for binding in catalog.bindings
        ),
    )
    without_origins.validate()
    assert without_origins.catalog_identity == catalog.catalog_identity
    session = SimulationStateSession(compilation, catalog=without_origins)
    assert session.catalog.catalog_identity == catalog.catalog_identity
    with pytest.raises(SimulationStateError, match="complete CompilationResult"):
        SimulationStateSession(compilation.ir)
    memory_path = ("StateReplay", "storage")
    commands = _binding(
        catalog, SimulationStateKind.MEMORY, "commands", memory_path
    )
    tags = _binding(catalog, SimulationStateKind.REGISTER, "tag_counts")
    sampled_data = _binding(
        catalog,
        SimulationStateKind.MEMORY_READ_DATA,
        "sampled",
        ("StateReplay", "history"),
    )
    assert (commands.length, commands.element_width, commands.packed_width) == (
        128, 1067, 136576
    )
    assert (tags.length, tags.element_width, tags.packed_width) == (256, 16, 4096)
    assert sampled_data.length is None and sampled_data.packed_width == 16
    assert len({item.binding_id for item in catalog.bindings}) == len(catalog.bindings)

    stale = replace(catalog, selected_ir_identity="stale")
    with pytest.raises(SimulationStateError, match="identity is stale"):
        stale.validate()
    malformed = catalog.to_data()
    malformed["bindings"][0]["packed_width"] += 1
    with pytest.raises(SimulationStateError, match="packed width"):
        SimulationStateCatalog.from_data(malformed)

    with pytest.raises(SimulationStateError, match="selected IR identity is stale"):
        build_simulation_state_catalog(
            compilation.ir, selected_ir_identity="selected:bogus"
        )
    with pytest.raises(SimulationStateError, match="selected IR identity is stale"):
        SimulationStateSession(SimpleNamespace(
            ir=compilation.ir,
            selected_ir_identity="selected:bogus",
        ))


def test_repeated_child_specializations_have_distinct_physical_state_ids() -> None:
    source = """
module Lane {
    clock clk reset rst
    in step : bit
    out value : u8
    reg count : u8 = 0
    when step { count <- truncate<8>(count + 1) }
    value = count
}
module LaneTop {
    clock clk reset rst
    in step : bit
    out values : vec<2,u8>
    inst lane[2] : Lane
    generate(i in 0..2) { lane[i].step = step }
    values = generate(i in 0..2) lane[i].value
}
"""
    compilation = compile_source(source, top="LaneTop", include_clash=False)
    catalog = build_simulation_state_catalog(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    lanes = tuple(
        item for item in catalog.bindings if item.object_name == "count"
    )
    assert tuple(item.physical_instance_path for item in lanes) == (
        ("LaneTop", "lane[0]"),
        ("LaneTop", "lane[1]"),
    )
    assert len({item.instance_identity for item in lanes}) == 2
    assert len({item.binding_id for item in lanes}) == 2
    assert len({item.specialization_identity for item in lanes}) == 1


def test_persistent_session_preloads_two_dependent_chunks_and_is_atomic() -> None:
    compilation = _compile()
    session = SimulationStateSession.from_compilation(compilation)
    memory_path = ("StateReplay", "storage")
    commands = _binding(
        session.catalog, SimulationStateKind.MEMORY, "commands", memory_path
    )
    tags = _binding(session.catalog, SimulationStateKind.REGISTER, "tag_counts")
    cursor = _binding(session.catalog, SimulationStateKind.REGISTER, "command_index")
    completed = _binding(
        session.catalog, SimulationStateKind.REGISTER, "completion_count"
    )
    sampled = _binding(
        session.catalog,
        SimulationStateKind.MEMORY_READ_DATA,
        "sampled",
        ("StateReplay", "history"),
    )
    sampled_cells = _binding(
        session.catalog,
        SimulationStateKind.MEMORY,
        "sampled",
        ("StateReplay", "history"),
    )

    session.step({"run": 0}, reset=True)
    session.step({"run": 0}, reset=False)
    session.preload({
        commands.binding_id: list(range(128)),
        tags.binding_id: [0] * 256,
        cursor.binding_id: 0,
        completed.binding_id: 0,
        sampled.binding_id: 0x1234,
    })
    first = session.run(({"run": 1} for _ in range(64)))
    assert first[0]["completed"] == 0
    assert session.read(completed.binding_id) == 64
    assert session.read(cursor.binding_id) == 64
    assert session.read(sampled.binding_id) == 0
    assert session.read(tags.binding_id)[:64] == [1] * 64

    # Chunk two is constructed from the retained completion image.  It must
    # observe and increment the same counters rather than starting a new epoch.
    first_counts = session.read(tags.binding_id)
    for index in range(64, 128):
        session.write(
            commands.binding_id,
            index if first_counts[index - 64] == 1 else 255,
            index=index,
        )
    session.run(({"run": 1} for _ in range(64)))
    assert session.read(tags.binding_id)[:128] == [1] * 128
    assert session.read(completed.binding_id) == 128
    assert session.read(cursor.binding_id) == 0

    snapshot = session.snapshot((commands.binding_id, tags.binding_id))
    snapshot[tags.binding_id][0] = 99
    assert session.read(tags.binding_id, index=0) == 1
    before = session.snapshot()
    with pytest.raises(SimulationStateError, match="does not fit exact type"):
        session.preload({cursor.binding_id: 0, tags.binding_id: [0] * 255})
    assert session.snapshot() == before

    # State access does not create a new reset policy.  The existing memory
    # contract preserves commands, while registers and the sampled memory clear.
    session.write(commands.binding_id, 77, index=0)
    session.write(sampled_cells.binding_id, 0x4321, index=0)
    session.write(sampled.binding_id, 0x1234)
    session.step({"run": 0}, reset=True)
    assert session.read(commands.binding_id, index=0) == 77
    assert session.read(tags.binding_id) == [0] * 256
    assert session.read(completed.binding_id) == 0
    assert session.read(sampled_cells.binding_id) == [0, 0]
    assert session.read(sampled.binding_id) == 0


def test_direct_bundle_is_deterministic_strict_and_does_not_change_rtl(
    tmp_path: Path,
) -> None:
    compilation = _compile()
    before = emit_artifact(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    bundle = build_systemverilog_simulation_state_bundle(compilation.ir, before)
    after = emit_artifact(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )

    assert before.text == after.text
    assert before.artifact_hash == after.artifact_hash
    assert before.build_identity == after.build_identity
    assert before.to_json() == after.to_json()
    assert bundle == SystemVerilogSimulationStateBundle.from_json(bundle.to_json())
    assert bundle.cpp_header() == bundle.cpp_header()
    assert all(
        item.vpi_path.startswith("TOP.StateReplay.zlang_top_core.")
        for item in bundle.locators
    )
    duplicate_path = replace(
        bundle,
        locators=(
            bundle.locators[0],
            replace(bundle.locators[1], vpi_path=bundle.locators[0].vpi_path),
            *bundle.locators[2:],
        ),
    )
    with pytest.raises(SimulationStateError, match="duplicate VPI path"):
        duplicate_path.validate()
    bundle.validate_artifact(before)
    with pytest.raises(SimulationStateError, match="does not match"):
        bundle.validate_artifact(replace(before, artifact_hash="stale"))
    changed_text = replace(before, text=before.text + "// modified after emission\n")
    with pytest.raises(SimulationStateError, match="does not match"):
        bundle.validate_artifact(changed_text)
    with pytest.raises(SimulationStateError, match="published hash"):
        build_systemverilog_simulation_state_bundle(compilation.ir, changed_text)
    stale_selected = replace(before, selected_ir_identity="selected:bogus")
    with pytest.raises(SimulationStateError, match="selected IR identity is stale"):
        build_systemverilog_simulation_state_bundle(
            compilation.ir, stale_selected
        )

    rtl = tmp_path / "StateReplay.sv"
    rtl.write_text(before.text)
    bundle.validate_rtl_file(rtl)
    rtl.write_text(before.text + "// stale file\n")
    with pytest.raises(SimulationStateError, match="published RTL file"):
        bundle.validate_rtl_file(rtl)


def test_wide_ztpu_entry_shape_has_unbounded_raw_word_access() -> None:
    compilation = compile_source("""
module WideReplay {
    clock clk reset rst
    in address : u1
    out command : bits<1067>
    memory commands : mem<bits<1067>,2> {
        read_latency 0
        collision read_first
        reset { contents preserve read_data preserve }
    }
    commands.read_address = address
    commands.write_enable = 0
    commands.write_address = 0
    commands.write_data = 0
    command = commands.read_data
}
""", include_clash=False)
    artifact = emit_artifact(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    bundle = build_systemverilog_simulation_state_bundle(compilation.ir, artifact)
    commands = _binding(
        bundle.catalog, SimulationStateKind.MEMORY, "commands", ("WideReplay",)
    )
    assert commands.element_width == 1067
    assert commands.packed_width == 2134
    assert "write_element_words" in bundle.cpp_header()
    assert "state element is wider than 64 bits" in bundle.cpp_header()


def test_multidomain_state_access_is_explicitly_unsupported() -> None:
    compilation = compile_source("""
module MultiDomainState {
    clock a reset ar @a
    clock b reset br @b
    out value : u8 @a
    reg count : u8 = 0 @a
    value = count
}
""", include_clash=False)
    with pytest.raises(SimulationStateError, match="exactly one root"):
        build_simulation_state_catalog(
            compilation.ir,
            selected_ir_identity=compilation.selected_ir_identity,
        )


def test_unsupported_state_and_malformed_catalogs_fail_closed() -> None:
    enum_compilation = compile_source("""
enum Phase { Idle Run }
module EnumState {
    clock clk reset rst
    out active : bit
    reg phase : Phase = Phase.Idle
    active = phase == Phase.Run
}
""", include_clash=False)
    with pytest.raises(SimulationStateError, match="unsupported by simulation state"):
        build_simulation_state_catalog(
            enum_compilation.ir,
            selected_ir_identity=enum_compilation.selected_ir_identity,
        )

    compilation = _compile()
    catalog = build_simulation_state_catalog(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    cursor = _binding(catalog, SimulationStateKind.REGISTER, "command_index")
    malformed_path = replace(
        catalog,
        bindings=tuple(
            replace(binding, physical_instance_path=())
            if binding.binding_id == cursor.binding_id else binding
            for binding in catalog.bindings
        ),
    )
    with pytest.raises(SimulationStateError, match="physical path"):
        malformed_path.validate()
    malformed_kind = replace(
        catalog,
        bindings=tuple(
            replace(binding, object_kind=SimulationStateKind.MEMORY)
            if binding.binding_id == cursor.binding_id else binding
            for binding in catalog.bindings
        ),
    )
    with pytest.raises(SimulationStateError, match="must be indexed"):
        malformed_kind.validate()
    with pytest.raises(SimulationStateError, match="identities must not be empty"):
        replace(catalog, selected_ir_identity=123).validate()

    transition = compilation.ir.resolved_transition
    assert transition is not None
    missing_resource_module = replace(
        compilation.ir,
        resolved_transition=replace(
            transition,
            resources=tuple(
                resource
                for resource in transition.resources
                if resource.name != "command_index"
            ),
        ),
    )
    selected = canonical_ir_identity(lower(
        missing_resource_module,
        stage=OptimizationStage.SELECTED_ARCHITECTURE,
    ))
    with pytest.raises(SimulationStateError, match="missing its resolved state resource"):
        build_simulation_state_catalog(
            missing_resource_module,
            selected_ir_identity=selected,
        )


def test_failed_step_does_not_consume_async_reset_release_edge() -> None:
    compilation = compile_source("""
module AsyncReplay {
    clock clk
    async reset rst @clk
    in step : bit
    out value : u8
    reg count : u8 = 0
    when step { count <- truncate<8>(count + 1) }
    value = count
}
""", include_clash=False)
    session = SimulationStateSession.from_compilation(compilation)
    count = _binding(
        session.catalog,
        SimulationStateKind.REGISTER,
        "count",
        ("AsyncReplay",),
    )
    session.step({"step": 1}, reset=True)
    assert session.read(count.binding_id) == 0
    with pytest.raises(SimulationStateError):
        session.step({}, reset=False)
    assert session.cycle == 1
    session.step({"step": 1}, reset=False)
    session.step({"step": 1}, reset=False)
    assert session.read(count.binding_id) == 0
    session.step({"step": 1}, reset=False)
    assert session.read(count.binding_id) == 1


@pytest.mark.parametrize("conflicting_declaration", (
    "reg foo_cells : u8 = 0",
    "reg foo_reset_index : u8 = 0",
    "in foo_cells : u8",
    "in foo_read_data : u8",
    "foo_cells : CollisionChild",
))
def test_module_namespace_collisions_reject_source_or_reallocate_child(
    conflicting_declaration: str,
) -> None:
    source = """
module CollisionChild {
    clock clk reset rst
    out value : u8
    value = 0
}
module StateNameCollision {
    clock clk reset rst
    in enable : bit
    in address : u1
    out result : u8
    __CONFLICTING_DECLARATION__
    memory foo : mem<u8,2> {
        read_latency 1
        collision read_first
    }
    rule access when enable {
        foo.read(address)
    }
    result = foo.read_data
}
""".replace("__CONFLICTING_DECLARATION__", conflicting_declaration)
    compilation = compile_source(
        source, top="StateNameCollision", include_clash=False
    )
    if conflicting_declaration == "foo_cells : CollisionChild":
        # A private child token may move; architectural source state retains
        # its name and cannot alias source ports/registers checked below.
        artifact = emit_artifact(
            compilation.ir, selected_ir_identity=compilation.selected_ir_identity
        )
        child = module_rtl_names(compilation.ir).instance("foo_cells")
        assert child != "foo_cells"
        assert f" {child} (" in artifact.text
        bundle = build_systemverilog_simulation_state_bundle(compilation.ir, artifact)
        assert any(item.vpi_path.endswith(".foo_cells") for item in bundle.locators)
        return
    with pytest.raises(
        SystemVerilogEmissionError,
        match="module identifier collision",
    ):
        emit_artifact(
            compilation.ir,
            selected_ir_identity=compilation.selected_ir_identity,
        )


def test_child_output_is_allocated_away_from_memory_state() -> None:
    compilation = compile_source("""
module CellsChild {
    clock clk reset rst
    out memory_cells : u8
    memory_cells = 0
}
module ChildOutputStateCollision {
    clock clk reset rst
    in enable : bit
    in address : u1
    out result : u8
    foo : CellsChild
    memory foo_memory : mem<u8,2> {
        read_latency 1
        collision read_first
    }
    rule access when enable {
        foo_memory.read(address)
    }
    result = foo.memory_cells
}
""", top="ChildOutputStateCollision", include_clash=False)

    artifact = emit_artifact(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    interconnect = module_rtl_names(compilation.ir).child_signal("foo", "memory_cells")
    assert interconnect != "foo_memory_cells"
    assert f"logic [7:0] {interconnect};" in artifact.text
    bundle = build_systemverilog_simulation_state_bundle(compilation.ir, artifact)
    assert any(item.vpi_path.endswith(".foo_memory_cells") for item in bundle.locators)


def test_unreferenced_legacy_fifo_observation_does_not_reserve_port_name() -> None:
    compilation = compile_source("""
module LegacyFifoPortName {
    clock clk reset rst
    in queue_front : u8
    out y : u8
    fifo queue : fifo<u8,2>
    queue.data = 0
    queue.push = 0
    queue.pop = 0
    y = queue_front
}
""", include_clash=False)

    artifact = emit_artifact(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    assert artifact.text.count("queue_front") == 2


def test_cli_publishes_separate_state_bundle(tmp_path: Path) -> None:
    source = tmp_path / "state.zhl"
    rtl = tmp_path / "state.sv"
    bundle = tmp_path / "state-access"
    source.write_text(SOURCE)
    from zlang.cli import main

    assert main([
        str(source),
        "--systemverilog", str(rtl),
        "--simulation-state-bundle", str(bundle),
    ]) == 0
    assert rtl.is_file()
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "simulation_state.hpp").is_file()
    loaded = SystemVerilogSimulationStateBundle.from_json(
        (bundle / "manifest.json").read_text()
    )
    assert loaded.artifact_hash == emit_artifact(
        _compile().ir, selected_ir_identity=_compile().selected_ir_identity
    ).artifact_hash


def test_real_verilator_vpi_preload_and_inspection(tmp_path: Path) -> None:
    verilator = shutil.which("verilator")
    assert verilator is not None, "Verilator is required by the ZL-006 acceptance"
    compilation = _compile()
    artifact = emit_artifact(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    bundle = build_systemverilog_simulation_state_bundle(compilation.ir, artifact)
    rtl = tmp_path / "StateReplay.sv"
    rtl.write_text(artifact.text)
    bundle.publish(tmp_path / "access")

    commands = _binding(
        bundle.catalog,
        SimulationStateKind.MEMORY,
        "commands",
        ("StateReplay", "storage"),
    )
    tags = _binding(bundle.catalog, SimulationStateKind.REGISTER, "tag_counts")
    cursor = _binding(bundle.catalog, SimulationStateKind.REGISTER, "command_index")
    completed = _binding(
        bundle.catalog, SimulationStateKind.REGISTER, "completion_count"
    )
    sampled = _binding(
        bundle.catalog,
        SimulationStateKind.MEMORY_READ_DATA,
        "sampled",
        ("StateReplay", "history"),
    )
    sampled_cells = _binding(
        bundle.catalog,
        SimulationStateKind.MEMORY,
        "sampled",
        ("StateReplay", "history"),
    )
    harness = tmp_path / "sim.cpp"
    harness.write_text(f"""
#include "VStateReplay.h"
#include "verilated.h"
#include "verilated_vpi.h"
#include "access/simulation_state.hpp"
#include <cstdint>

static void tick(VStateReplay& top) {{
  top.clk = 0; top.eval();
  top.clk = 1; top.eval();
  top.clk = 0; top.eval();
}}

int main(int argc, char** argv) {{
  VerilatedContext context;
  context.commandArgs(argc, argv);
  VStateReplay top{{&context}};
  top.clk = 0; top.rst = 1; top.run = 0; top.eval();
  tick(top);
  top.rst = 0; top.eval();
  zlang_simulation::StateAccess state;
  for (unsigned i = 0; i < 128; ++i) {{
    zlang_simulation::RawWords command(34, 0);
    command[0] = i;
    command[16] = 0x89abcdefU ^ i;
    command[33] = 0x400U;
    state.write_element_words("{commands.binding_id}", i, command);
  }}
  const auto command_127 =
      state.read_element_words("{commands.binding_id}", 127);
  if (command_127.size() != 34 || command_127[0] != 127 ||
      command_127[16] != (0x89abcdefU ^ 127U) ||
      command_127[33] != 0x400U) return 9;
  bool rejected = false;
  try {{ state.write_element_words("{commands.binding_id}", 0, {{0}}); }}
  catch (const std::runtime_error&) {{ rejected = true; }}
  if (!rejected) return 7;
  zlang_simulation::RawWords too_wide(34, 0);
  too_wide[33] = 0x800U;
  rejected = false;
  try {{ state.write_element_words("{commands.binding_id}", 0, too_wide); }}
  catch (const std::runtime_error&) {{ rejected = true; }}
  if (!rejected) return 8;
  for (unsigned i = 0; i < 256; ++i)
    state.write_element_u64("{tags.binding_id}", i, 0);
  state.write_u64("{cursor.binding_id}", 0);
  state.write_u64("{completed.binding_id}", 0);
  rejected = false;
  try {{ state.write_u64("{cursor.binding_id}", 128); }}
  catch (const std::runtime_error&) {{ rejected = true; }}
  if (!rejected) return 6;
  rejected = false;
  try {{ state.write_element_u64("{tags.binding_id}", 0, uint64_t{{1}} << 16U); }}
  catch (const std::runtime_error&) {{ rejected = true; }}
  if (!rejected) return 5;
  top.run = 1;
  for (unsigned i = 0; i < 64; ++i) tick(top);
  if (state.read_u64("{completed.binding_id}") != 64) return 10;
  if (state.read_u64("{cursor.binding_id}") != 64) return 11;
  for (unsigned i = 0; i < 64; ++i)
    if (state.read_element_u64("{tags.binding_id}", i) != 1) return 12;
  for (unsigned i = 64; i < 128; ++i) {{
    zlang_simulation::RawWords command(34, 0);
    command[0] =
        state.read_element_u64("{tags.binding_id}", i - 64) == 1 ? i : 255;
    command[20] = 0x13579bdfU;
    command[33] = 0x155U;
    state.write_element_words("{commands.binding_id}", i, command);
  }}
  for (unsigned i = 0; i < 64; ++i) tick(top);
  if (state.read_u64("{completed.binding_id}") != 128) return 20;
  if (state.read_u64("{cursor.binding_id}") != 0) return 21;
  for (unsigned i = 0; i < 128; ++i)
    if (state.read_element_u64("{tags.binding_id}", i) != 1) return 22;
  state.write_element_u64("{sampled_cells.binding_id}", 0, 0x4321);
  state.write_u64("{sampled.binding_id}", 0x1234);
  top.run = 0; top.rst = 1; tick(top);
  if (state.read_element_words("{commands.binding_id}", 0)[33] != 0x400U) return 30;
  if (state.read_element_u64("{tags.binding_id}", 0) != 0) return 31;
  if (state.read_u64("{cursor.binding_id}") != 0) return 32;
  if (state.read_u64("{completed.binding_id}") != 0) return 33;
  if (state.read_element_u64("{sampled_cells.binding_id}", 0) != 0) return 34;
  if (state.read_u64("{sampled.binding_id}") != 0) return 35;
  return 0;
}}
""")
    object_dir = tmp_path / "obj"
    command = [
        verilator,
        "--cc", "--exe", "--build", "--vpi", "--public-flat-rw",
        "--top-module", "StateReplay", "--Mdir", str(object_dir),
        "-o", "state_replay", str(rtl), str(harness),
        f"-I{tmp_path}",
        "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
    ]
    completed_build = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "CCACHE_DISABLE": "1"},
    )
    assert completed_build.returncode == 0, completed_build.stderr
    run = subprocess.run(
        [str(object_dir / "state_replay")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
