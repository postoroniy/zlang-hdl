"""Exact, bounded scheduler selection for ZTPU-shaped scalar control logic."""

from __future__ import annotations

from itertools import product
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import zlang.sim
import zlang.ir.state as state_ir
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.expressions import Constant, InputRef
from zlang.ir.state import (
    ActionGroup,
    ResolvedTransition,
    StateAction,
    StateActionKind,
    StateResource,
    StateResourceKind,
    FifoOccupancy,
    StateSelectionLimitError,
    actions_conflict,
    conditional_activation_predicates,
    ordered_groups,
    select_action_groups,
    selection_regions,
    selection_regions_for_transition,
)
from zlang.ir.types import BitType
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.lowering import lower
from zlang.simulation_plan import build_simulation_plan
from tests.simulation.differential import run_differential


ROOT = Path(__file__).resolve().parents[2]
ZTPU_FIXTURE = ROOT / "tests" / "fixtures" / "ztpu_zl038"
ZTPU_CANONICAL_IDENTITIES = {
    "fsm": "high-level:97f4d1c880c26723ee0c12f579f3fedecc02d3c74851a0006c7654d74b7079d6",
    "flat": "high-level:5996ec117ccb64bf985b3eb38aa3dda39067870f68209c01921b60502256bdd9",
    "hierarchy": "high-level:cab751d8ab06bf77f68c187eb0fee05f4a5781babeb2193c8df10e9fc4667f8d",
}


def _transition(group_count: int) -> ResolvedTransition:
    bit = BitType()
    resources = tuple(
        StateResource(f"register:r{index}", f"r{index}", StateResourceKind.REGISTER, bit, "clk")
        for index in range(3)
    )
    groups = tuple(
        ActionGroup(
            f"group:g{index}",
            f"g{index}",
            Constant(1, bit),
            (
                StateAction(
                    f"action:g{index}",
                    resources[index % 3].semantic_id,
                    StateActionKind.REGISTER_WRITE,
                    (Constant(1, bit),),
                    f"group:g{index}",
                ),
            ),
            domain="clk",
        )
        for index in range(group_count)
    )
    return ResolvedTransition(
        "transition:many", "clk", "rst", resources, groups,
        tuple((f"g{index}", f"g{index + 1}") for index in range(group_count - 1)),
    )


def _covered(region: tuple[bool | None, ...], guards: tuple[bool, ...]) -> bool:
    return all(want is None or want == got for want, got in zip(region, guards, strict=True))


def test_no_fifo_selection_matches_independent_exhaustive_oracle() -> None:
    transition = _transition(8)
    groups = ordered_groups(transition)
    regions = {group.rule_name: selection_regions(transition, group.rule_name) for group in groups}
    for bits in product((False, True), repeat=len(groups)):
        selected: list[str] = []
        used_resources: set[str] = set()
        for group, enabled in zip(groups, bits, strict=True):
            resource_id = group.actions[0].resource_id
            if enabled and resource_id not in used_resources:
                selected.append(group.rule_name)
                used_resources.add(resource_id)
        guards = {group.rule_name: enabled for group, enabled in zip(groups, bits, strict=True)}
        assert select_action_groups(transition, guards, {}) == tuple(selected)
        assert {
            name for name, cubes in regions.items() if any(_covered(cube, bits) for cube in cubes)
        } == set(selected)


def test_large_guard_only_transition_never_enumerates_selection_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = _transition(22)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("guard-only regions must not enumerate guard combinations")

    monkeypatch.setattr(state_ir, "select_action_groups", forbidden)
    regions = selection_regions(transition, "g21")
    assert regions
    assert all(len(region) == 22 for region in regions)


def _fifo_selection_oracle(
    transition: ResolvedTransition,
    guards: dict[str, bool],
    counts: dict[str, int],
    activations: dict[str, bool],
) -> tuple[str, ...]:
    """Independent small-case exhaustive specification, not the production solver."""
    groups = ordered_groups(transition)
    best: tuple[str, ...] = ()
    best_score: tuple[int, ...] | None = None
    for bits in product((False, True), repeat=len(groups)):
        if any(bit and not guards[group.rule_name]
               for bit, group in zip(bits, groups, strict=True)):
            continue
        actions = [
            tuple(
                action for action in group.actions
                if action.activation is None or activations[action.semantic_id]
            )
            for group in groups
        ]
        if any(bit and not group_actions
               for bit, group_actions in zip(bits, actions, strict=True)):
            continue
        if any(
            actions_conflict(left, right)
            for i in range(len(groups)) if bits[i]
            for j in range(i + 1, len(groups)) if bits[j]
            for left in actions[i] for right in actions[j]
        ):
            continue
        selected_actions = [
            action for bit, group_actions in zip(bits, actions, strict=True)
            if bit for action in group_actions
        ]
        legal = True
        for resource in transition.resources:
            if resource.kind is not StateResourceKind.FIFO:
                continue
            kinds = {
                action.kind for action in selected_actions
                if action.resource_id == resource.semantic_id
            }
            pop = StateActionKind.FIFO_POP in kinds
            push = StateActionKind.FIFO_PUSH in kinds
            count = counts[resource.name]
            if (pop and count == 0) or (
                push and count == resource.depth and not pop
            ):
                legal = False
        if legal and (best_score is None or bits > best_score):
            best_score = bits
            best = tuple(group.rule_name for bit, group in zip(bits, groups, strict=True) if bit)
    return best


def test_fifo_symbolic_regions_match_independent_exhaustive_oracle() -> None:
    bit = BitType()
    q = StateResource("fifo:q", "q", StateResourceKind.FIFO, bit, "clk", depth=2)
    p = StateResource("fifo:p", "p", StateResourceKind.FIFO, bit, "clk", depth=1)
    register = StateResource("register:r", "r", StateResourceKind.REGISTER, bit, "clk")
    predicate = InputRef("conditional", bit)
    effects = (
        ((q, StateActionKind.FIFO_PUSH, None),),
        ((register, StateActionKind.REGISTER_WRITE, predicate),),
        ((q, StateActionKind.FIFO_POP, None),
         (p, StateActionKind.FIFO_PUSH, predicate)),
        ((p, StateActionKind.FIFO_POP, None),
         (register, StateActionKind.REGISTER_WRITE, predicate)),
    )
    groups = tuple(
        ActionGroup(
            f"group:g{index}", f"g{index}", Constant(1, bit),
            tuple(
                StateAction(
                    f"action:g{index}:{action_index}", resource.semantic_id,
                    kind, (Constant(1, bit),) if kind is not StateActionKind.FIFO_POP else (),
                    f"group:g{index}", activation=activation,
                )
                for action_index, (resource, kind, activation) in enumerate(group_effects)
            ),
            domain="clk",
        )
        for index, group_effects in enumerate(effects)
    )
    transition = ResolvedTransition(
        "transition:fifo-symbolic", "clk", "rst", (q, p, register), groups,
        tuple((f"g{index}", f"g{index + 1}") for index in range(3)),
    )
    regions = selection_regions_for_transition(transition)
    assert conditional_activation_predicates(transition) == (predicate,)
    for q_count, p_count, guard_bits, enabled in product(
        range(3), range(2), product((False, True), repeat=4), (False, True),
    ):
        guards = {group.rule_name: guard for group, guard in zip(groups, guard_bits, strict=True)}
        activations = {
            action.semantic_id: enabled
            for group in groups for action in group.actions
            if action.activation is not None
        }
        counts = {"q": q_count, "p": p_count}
        expected = _fifo_selection_oracle(transition, guards, counts, activations)
        assert select_action_groups(transition, guards, counts, activations) == expected
        occupancy = (
            (FifoOccupancy.EMPTY if q_count == 0 else
             FifoOccupancy.FULL if q_count == 2 else FifoOccupancy.MIDDLE),
            FifoOccupancy.EMPTY if p_count == 0 else FifoOccupancy.FULL,
        )
        inputs = (*occupancy, *guard_bits, enabled)
        actual = {
            name for name, cubes in regions.items()
            if any(
                all(want is None or want == got
                    for want, got in zip(cube, inputs, strict=True))
                for cube in cubes
            )
        }
        assert actual == set(expected)


def test_full_fifo_higher_push_can_require_lower_pop() -> None:
    source = """module FullReplacement {
      clock clk reset rst in fire:bit out count:u2
      fifo q:fifo<u8,2>
      rule push when fire { q.push(7) }
      rule pop when fire { q.pop() }
      priority push > pop
      count=q.count
    }"""
    transition = compile_source(source, top="FullReplacement").ir.resolved_transition
    assert transition is not None
    assert select_action_groups(
        transition, {"push": True, "pop": True}, {"q": 2},
    ) == ("push", "pop")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_full_fifo_replacement_matches_expected_native_and_direct_sv(
    tmp_path: Path,
) -> None:
    source = tmp_path / "full_replacement.zhl"
    source.write_text("""module FullReplacement {
      clock clk reset rst
      in fill, replace : bit
      in x : u8
      out front : u8
      out count : u2
      fifo q : fifo<u8,2>
      rule fill_q when fill { q.push(x) }
      rule replace_push when replace { q.push(x) }
      rule replace_pop when replace { q.pop() }
      priority fill_q > replace_push > replace_pop
      front = q.front
      count = q.count
    }""")
    events = [
        {"set": {"fill": 1, "replace": 0, "x": 10}, "edges": ("clk",)},
        {"set": {"fill": 1, "replace": 0, "x": 20}, "edges": ("clk",)},
        {"set": {"fill": 0, "replace": 1, "x": 30}, "edges": ("clk",)},
        {"set": {"fill": 0, "replace": 0, "x": 0}, "edges": ("clk",)},
    ]
    trace = run_differential(
        source, top="FullReplacement", events=events, directory=tmp_path,
    )
    assert trace.native == trace.direct_sv
    assert [(sample["front"], sample["count"]) for sample in trace.native] == [
        (10, 1), (10, 2), (20, 2), (20, 2),
    ]


def test_twenty_fifo_rules_build_bounded_regions_without_subset_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 20
    ports = " ".join(f"in g{index}:bit" for index in range(count))
    rules = "\n".join(
        f"rule r{index} when g{index} {{ q.{'push(1)' if index % 2 else 'pop()'} }}"
        for index in range(count)
    )
    priorities = "\n".join(
        f"priority r{index} > r{index + 1}"
        for index in range(count - 1)
    )
    transition = compile_source(
        f"module T {{ clock clk reset rst {ports} out y:u3 "
        f"fifo q:fifo<u8,4> {rules} {priorities} y=q.count }}",
        top="T",
    ).ir.resolved_transition
    assert transition is not None

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("symbolic regions must not enumerate rule subsets")

    monkeypatch.setattr(state_ir, "select_action_groups", forbidden)
    regions = selection_regions_for_transition(transition)
    assert set(regions) == {f"r{index}" for index in range(count)}
    assert sum(map(len, regions.values())) <= 200
    assert selection_regions_for_transition(transition) == regions


def test_scheduler_complexity_limit_is_a_structured_diagnostic() -> None:
    original = _transition(130)
    fifo = StateResource(
        "fifo:q", "q", StateResourceKind.FIFO, BitType(), "clk", depth=2,
    )
    transition = ResolvedTransition(
        original.semantic_id, original.domain, original.reset,
        (*original.resources, fifo), original.action_groups, original.priorities,
    )
    with pytest.raises(StateSelectionLimitError) as captured:
        selection_regions_for_transition(transition)
    assert captured.value.code == "ZL-STATE-SCHEDULER-LIMIT"
    assert "256 decision variables" in str(captured.value)


def _control_source(group_count: int, *, sticky: bool) -> str:
    registers = "reg reader_error : bit = 0 reg writer_error : bit = 0" if sticky else ""
    rules = "\n".join(
        f"rule phase_{index} when state=={index} {{ state <- {index + 1} }}"
        for index in range(group_count)
    )
    priorities = "\n".join(
        f"priority phase_{index} > phase_{index + 1}"
        for index in range(group_count - 1)
    )
    sticky_rules = (
        "rule read_fault when fault_read { reader_error <- 1 }\n"
        "rule write_fault when fault_write { writer_error <- 1 }\n"
        if sticky else ""
    )
    return f"""
module Control {{
    clock clk reset rst
    in fault_read, fault_write : bit
    out value : u8
    out fault : bit
    reg state : u8 = 0
    {registers}
    {rules}
    {sticky_rules}
    {priorities}
    value = state
    fault = {"reader_error | writer_error" if sticky else "0"}
}}
"""


@pytest.mark.parametrize("group_count,sticky", ((22, False), (12, True)))
def test_ztpu_shaped_control_builds_native_plan_and_direct_sv(
    group_count: int, sticky: bool,
) -> None:
    module = compile_source(_control_source(group_count, sticky=sticky), top="Control").ir
    transition = module.resolved_transition
    assert transition is not None
    assert len(transition.action_groups) == group_count + (2 if sticky else 0)
    plan = build_simulation_plan(module)
    artifact = emit_artifact(module)
    assert plan.identity
    assert build_simulation_plan(module).canonical_bytes == plan.canonical_bytes
    assert "module Control" in artifact.text
    assert emit_artifact(module).text == artifact.text


@pytest.mark.parametrize("group_count,sticky", ((22, False), (12, True)))
def test_ztpu_shaped_control_native_instances_are_deterministic(
    tmp_path: Path, group_count: int, sticky: bool,
) -> None:
    source = tmp_path / "control.zhl"
    source.write_text(_control_source(group_count, sticky=sticky))
    engines = [
        zlang.sim.load(source, top="Control", engine="native") for _ in range(2)
    ]
    with engines[0], engines[1]:
        for cycle in range(group_count + 3):
            for instance in engines:
                instance.set("fault_read", int(sticky and cycle == 2))
                instance.set("fault_write", int(sticky and cycle == 4))
            assert engines[0].eval() == engines[1].eval()
            assert engines[0].edge("clk") == engines[1].edge("clk")


@pytest.mark.parametrize("variant", ("fsm", "flat"))
def test_exact_ztpu_zl038_reproducers_are_bounded_and_cycle_equivalent(
    tmp_path: Path, variant: str,
) -> None:
    source_text = (ZTPU_FIXTURE / variant / "layout_dma_core.zhl").read_text()
    semantic = compile_source(
        source_text, top="ZtpuLayoutDmaControl64x32",
        source_unit="src/layout_dma_core.zhl",
    ).ir
    assert canonical_ir_identity(lower(semantic)) == ZTPU_CANONICAL_IDENTITIES[variant]

    project = tmp_path / "ztpu"
    source_root = project / "src"
    source_root.mkdir(parents=True)
    shutil.copyfile(ZTPU_FIXTURE / variant / "layout_dma_core.zhl", source_root / "layout_dma_core.zhl")
    shutil.copyfile(ZTPU_FIXTURE / "layout_dma.zhl", source_root / "layout_dma.zhl")
    shutil.copyfile(ZTPU_FIXTURE / "normal-copy.jsonl", project / "normal-copy.jsonl")
    (project / "zlang.toml").write_text(
        'schema = 1\n[project]\nname = "ztpu"\nversion = "0.1.0"\nsource-root = "src"\n'
    )
    (project / "zlang.lock").write_text(
        'schema = 2\nmanifest-resolution-digest = '
        '"fbfb4742458480f2d931bd830351e0ad9a20fac0b0a83ac35c98773c97fc7f7b"\n'
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    command = [
        sys.executable, "-c", "from zlang.cli import main; raise SystemExit(main())",
    ]
    simulation_command = [
        *command,
        "sim", "src/layout_dma_core.zhl", "--project", "zlang.toml",
        "--top", "ZtpuLayoutDmaControl64x32", "--engine", "native",
        "--events", "normal-copy.jsonl", "--json",
    ]
    if shutil.which("verilator") is not None:
        simulation_command.extend(
            (
                "--compare-with", "verilator",
                "--compare-artifacts", str(project / "comparison"),
            )
        )
    result = subprocess.run(
        simulation_command,
        cwd=project, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    trace = json.loads(result.stdout)
    assert len(trace) == 11
    assert (trace[3]["read_ar_valid"], trace[3]["read_ar_addr"]) == (1, 0x100)
    assert (trace[6]["write_aw_valid"], trace[6]["write_aw_addr"]) == (1, 0x200)
    assert (
        trace[7]["write_w_valid"], trace[7]["write_w_data"], trace[7]["write_w_last"]
    ) == (1, 0x12345678, 1)
    assert trace[10]["done"] == 1
    assert all(sample["error"] == 0 for sample in trace)
    assert trace[0]["write_w_last"] == (1 if variant == "fsm" else 0)

    rtl = project / "layout_dma.sv"
    result = subprocess.run(
        [
            *command,
            "src/layout_dma.zhl", "--project", "zlang.toml",
            "--top", "ZtpuLayoutDmaCopy64x32", "--systemverilog", str(rtl),
        ],
        cwd=project, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "module ZtpuLayoutDmaCopy64x32" in rtl.read_text()


def test_exact_ztpu_reader_controller_writer_hierarchy_emits_sv_bounded(
    tmp_path: Path,
) -> None:
    source_text = (ZTPU_FIXTURE / "hierarchy" / "layout_dma.zhl").read_text()
    semantic = compile_source(
        source_text, top="ZtpuLayoutDmaCopy64x32",
        source_unit="src/layout_dma.zhl",
    ).ir
    assert canonical_ir_identity(lower(semantic)) == ZTPU_CANONICAL_IDENTITIES["hierarchy"]

    project = tmp_path / "ztpu"
    source_root = project / "src"
    source_root.mkdir(parents=True)
    shutil.copyfile(
        ZTPU_FIXTURE / "hierarchy" / "layout_dma.zhl", source_root / "layout_dma.zhl"
    )
    (project / "zlang.toml").write_text(
        'schema = 1\n[project]\nname = "ztpu"\nversion = "0.1.0"\nsource-root = "src"\n'
    )
    (project / "zlang.lock").write_text(
        'schema = 2\nmanifest-resolution-digest = '
        '"fbfb4742458480f2d931bd830351e0ad9a20fac0b0a83ac35c98773c97fc7f7b"\n'
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    rtl = project / "layout_dma.sv"
    result = subprocess.run(
        [
            sys.executable, "-c", "from zlang.cli import main; raise SystemExit(main())",
            "src/layout_dma.zhl", "--project", "zlang.toml",
            "--top", "ZtpuLayoutDmaCopy64x32", "--systemverilog", str(rtl),
        ],
        cwd=project, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    text = rtl.read_text()
    assert "module ZtpuLayoutDmaCopy64x32" in text
    assert "module AXI4SingleBeatReader" in text
    assert "module AXI4SingleBeatWriter" in text
