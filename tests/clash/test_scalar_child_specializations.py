"""Clash scalar-child helper identity follows typed specialization identity."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = """
module C<W=8> {
    in x : uint<W>
    out y : uint<W>
    y = x
}

module Top {
    in a : u8
    in b : u16
    out y8 : u8
    out y16 : u16

    c8 : C<W=8> { x=a }
    c16 : C<W=16> { x=b }
    y8 = c8.y
    y16 = c16.y
}
"""


NESTED_SOURCE = """
module Leaf<W=8> {
    in x : uint<W>
    out y : uint<W>
    y = x
}

module Wrapper<W=8> {
    in x : uint<W>
    out y : uint<W>
    inst leaf_i : Leaf<W=W> { x=x }
    y = leaf_i.y
}

module Top {
    in a : u8
    in b : u16
    out y8 : u8
    out y16 : u16
    inst w8 : Wrapper<W=8> { x=a }
    inst w16 : Wrapper<W=16> { x=b }
    y8 = w8.y
    y16 = w16.y
}
"""


PARENT_NAME_COLLISION_SOURCE = """
module C { in x:u8 out y:u8 y=x }
module Top {
    in c:u8
    out y:u8
    inst child:C { x=c }
    y=child.y
}
"""


TOP_NAME_COLLISION_SOURCE = """
module TopEntity { in x:u8 out y:u8 y=x }
module Top {
    in x:u8
    out y:u8
    inst child:TopEntity { x=x }
    y=child.y
}
"""


CASE_COLLISION_SOURCE = """
module Foo { in x:u8 out y:u8 y=x }
module foo { in x:u8 out y:u8 y=x }
module Top {
    in a:u8 in b:u8
    out ya:u8 out yb:u8
    inst upper:Foo { x=a }
    inst lower:foo { x=b }
    ya=upper.y
    yb=lower.y
}
"""


SCHEDULED_OUTPUT_SOURCE = """
module Child {
    clock clk reset rst
    in fire:bit
    out qout:bit
    out pulse:bit
    reg q:bit=0
    priority {
        high_rule: when fire { q <- 1 }
        low_rule: when fire { q <- 0 pulse <- 1 }
    }
    qout=q
}

module Top {
    clock clk reset rst
    in fire:bit
    out qout:bit
    out pulse:bit
    inst child:Child { fire }
    qout=child.qout
    pulse=child.pulse
}
"""


MATERIALIZED_NAME_COLLISION_SOURCE = """
module Zlang_child_expr_0 { in x:u8 out y:u8 y=x }
module Wrapper {
    in a:u8 in b:u8 out y:bits<80>
    chunk:bits<16>=concat(bitcast<bits<8>>(a),bitcast<bits<8>>(b))
    inst coll:Zlang_child_expr_0 { x=a }
    y=concat(chunk,chunk,chunk,chunk,chunk)
}
module Top {
    in a:u8 in b:u8 out y:bits<80>
    inst w:Wrapper { a b }
    y=w.y
}
"""


DELAY_NAME_COLLISION_SOURCE = """
module Delay_0_s1 { in x:u8 out y:u8 y=x }
module Top {
    clock clk reset rst
    in x:u8 out y:u8
    inst child:Delay_0_s1 { x }
    y=delay<1>(child.y)
}
"""


RESIZE_NAME_COLLISION_SOURCE = """
module Resize { in x:u8 out y:u8 y=x }
module Top {
    in x:u8 out y:u9
    inst child:Resize { x }
    y=extend<9>(child.y)
}
"""


REGISTER_NAME_COLLISION_SOURCE = """
module Register {
    clock clk reset rst
    in x:u8 out y:u8
    reg q:u8=0
    rule capture when 1 { q <- x }
    y=q
}
module Top {
    clock clk reset rst
    in x:u8 out y:u8
    inst child:Register { x }
    y=child.y
}
"""


SCHEDULED_OUTPUT_HARNESS = r'''#include "VTop.h"
#include "verilated.h"
static void tick(VTop& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VTop d;
  d.fire=1; d.rst=1; tick(d);
  if (d.pulse != 0) return 1;
  d.rst=0; tick(d);
  if (d.pulse != 0) return 2;
  tick(d);
  if (d.pulse != 0) return 3;
  return 0;
}
'''


def _module():
    return compile_source(SOURCE, top="Top", include_clash=False).ir


def test_scalar_helpers_are_unique_per_semantic_specialization() -> None:
    module = _module()
    specializations = {
        item.instance.name: item.specialization_identity
        for item in module.elaborated_instances
    }
    assert specializations.keys() == {"c8", "c16"}
    assert specializations["c8"] != specializations["c16"]

    narrow = f"c_s{specializations['c8'][:8]}"
    wide = f"c_s{specializations['c16'][:8]}"
    clash = emit(module)

    assert clash.count(f"{narrow} :: Unsigned 8 -> Unsigned 8") == 1
    assert clash.count(f"{wide} :: Unsigned 16 -> Unsigned 16") == 1
    assert f"c8_y = {narrow} (a)" in clash
    assert f"c16_y = {wide} (b)" in clash
    # Reusable component names are semantic-specialization names.  Physical
    # instance names remain confined to their separate result bindings.
    assert "c8_" not in narrow
    assert "c16_" not in wide


def test_one_scalar_specialization_keeps_a_stable_short_suffix() -> None:
    source = SOURCE.replace(
        "    in b : u16\n    out y8 : u8\n    out y16 : u16\n\n"
        "    c8 : C<W=8> { x=a }\n"
        "    c16 : C<W=16> { x=b }\n"
        "    y8 = c8.y\n"
        "    y16 = c16.y\n",
        "    out y8 : u8\n\n"
        "    c8 : C<W=8> { x=a }\n"
        "    y8 = c8.y\n",
    )
    module = compile_source(source, top="Top", include_clash=False).ir
    clash = emit(module)
    helper = f"c_s{module.elaborated_instances[0].specialization_identity[:8]}"
    assert clash.count(f"{helper} :: Unsigned 8 -> Unsigned 8") == 1
    assert f"c8_y = {helper} (a)" in clash
    assert "\nc ::" not in clash


def test_transitive_helpers_share_one_global_specialization_catalog() -> None:
    module = compile_source(
        NESTED_SOURCE, top="Top", include_clash=False,
    ).ir
    wrappers = {
        item.instance.name: (item, child)
        for item, child in zip(
            module.elaborated_instances, module.children, strict=True,
        )
    }
    w8, wrapper8 = wrappers["w8"]
    w16, wrapper16 = wrappers["w16"]
    leaf8 = wrapper8.elaborated_instances[0]
    leaf16 = wrapper16.elaborated_instances[0]
    assert leaf8.specialization_identity != leaf16.specialization_identity

    wrapper8_name = f"wrapper_s{w8.specialization_identity[:8]}"
    wrapper16_name = f"wrapper_s{w16.specialization_identity[:8]}"
    leaf8_name = f"leaf_s{leaf8.specialization_identity[:8]}"
    leaf16_name = f"leaf_s{leaf16.specialization_identity[:8]}"
    clash = emit(module)

    assert clash.count(f"{leaf8_name} :: Unsigned 8 -> Unsigned 8") == 1
    assert clash.count(f"{leaf16_name} :: Unsigned 16 -> Unsigned 16") == 1
    assert clash.count(f"{wrapper8_name} :: Unsigned 8 -> Unsigned 8") == 1
    assert clash.count(f"{wrapper16_name} :: Unsigned 16 -> Unsigned 16") == 1
    assert f"leaf_i_y = {leaf8_name} (x)" in clash
    assert f"leaf_i_y = {leaf16_name} (x)" in clash
    assert f"w8_y = {wrapper8_name} (a)" in clash
    assert f"w16_y = {wrapper16_name} (b)" in clash
    assert "\nleaf ::" not in clash


def test_parent_binding_cannot_shadow_scalar_helper() -> None:
    module = compile_source(
        PARENT_NAME_COLLISION_SOURCE, top="Top", include_clash=False,
    ).ir
    specialization = module.elaborated_instances[0].specialization_identity
    helper = f"c_s{specialization[:8]}"
    clash = emit(module)

    assert clash.count(f"{helper} :: Unsigned 8 -> Unsigned 8") == 1
    assert f"child_y = {helper} (c)" in clash
    assert "\nc :: Unsigned 8 -> Unsigned 8" not in clash


def test_top_and_case_folded_helper_names_are_globally_unique() -> None:
    top_module = compile_source(
        TOP_NAME_COLLISION_SOURCE, top="Top", include_clash=False,
    ).ir
    top_specialization = top_module.elaborated_instances[0].specialization_identity
    top_clash = emit(top_module)
    assert top_clash.count("topEntity ::") == 1
    assert (
        f"topEntity_s{top_specialization[:8]} :: Unsigned 8 -> Unsigned 8"
        in top_clash
    )

    case_module = compile_source(
        CASE_COLLISION_SOURCE, top="Top", include_clash=False,
    ).ir
    case_clash = emit(case_module)
    names = [
        f"foo_s{item.specialization_identity[:8]}"
        for item in case_module.elaborated_instances
    ]
    assert names[0] != names[1]
    assert all(case_clash.count(f"{name} :: Unsigned 8 -> Unsigned 8") == 1 for name in names)
    assert f"upper_y = {names[0]} (a)" in case_clash
    assert f"lower_y = {names[1]} (b)" in case_clash


def test_generated_and_runtime_binders_cannot_shadow_helpers() -> None:
    materialized_module = compile_source(
        MATERIALIZED_NAME_COLLISION_SOURCE,
        top="Top",
        include_clash=False,
    ).ir
    wrapper = materialized_module.children[0]
    nested_identity = wrapper.elaborated_instances[0].specialization_identity
    materialized_helper = f"zlang_child_expr_0_s{nested_identity[:8]}"
    materialized_clash = emit(materialized_module)
    assert f"{materialized_helper} :: Unsigned 8 -> Unsigned 8" in materialized_clash
    assert f"  coll_y = {materialized_helper} (a)" in materialized_clash
    assert "  zlang_child_expr_0 :: BitVector 16" in materialized_clash
    assert "    zlang_child_expr_0 ::" not in materialized_clash

    for source, base in (
        (DELAY_NAME_COLLISION_SOURCE, "delay_0_s1"),
        (RESIZE_NAME_COLLISION_SOURCE, "resize"),
        (REGISTER_NAME_COLLISION_SOURCE, "register"),
    ):
        module = compile_source(source, top="Top", include_clash=False).ir
        identity = module.elaborated_instances[0].specialization_identity
        helper = f"{base}_s{identity[:8]}"
        clash = emit(module)
        assert f"{helper} ::" in clash
        assert f"\n{base} ::" not in clash


def test_sequential_child_output_actions_use_resolved_rule_schedule() -> None:
    module = compile_source(
        SCHEDULED_OUTPUT_SOURCE, top="Top", include_clash=False,
    ).ir
    trace = simulate_cycles(
        module,
        [{"fire": 1}, {"fire": 1}, {"fire": 1}],
        reset=[True, False, False],
    )
    assert [item["pulse"] for item in trace] == [0, 0, 0]

    clash = emit(module)
    assert "rule_high_rule_guard = fire" in clash
    assert "guard_0 == low && guard_1 == high" in clash
    assert "rule_low_rule_fire = (\\guard resetActive" not in clash

    direct_sv = emit_experimental(module)
    assert (
        "assign rule_low_rule_fire = !rst && "
        "((rule_high_rule_guard == 1'b0 && rule_low_rule_guard == 1'b1));"
        in direct_sv
    )


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_accepts_two_scalar_child_specializations(
    tmp_path: Path,
) -> None:
    module = _module()
    rtl = generate_verilog(
        emit(module), module.name, tmp_path / "rtl", CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, module.name)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    "source",
    (
        NESTED_SOURCE,
        PARENT_NAME_COLLISION_SOURCE,
        TOP_NAME_COLLISION_SOURCE,
        CASE_COLLISION_SOURCE,
        MATERIALIZED_NAME_COLLISION_SOURCE,
        DELAY_NAME_COLLISION_SOURCE,
        RESIZE_NAME_COLLISION_SOURCE,
        REGISTER_NAME_COLLISION_SOURCE,
    ),
)
def test_real_clash_accepts_global_scalar_helper_names(
    tmp_path: Path,
    source: str,
) -> None:
    module = compile_source(source, top="Top", include_clash=False).ir
    rtl = generate_verilog(
        emit(module), module.name, tmp_path / "rtl", CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, module.name)


def _simulate_scheduled_output_rtl(
    files: tuple[Path, ...],
    tmp_path: Path,
    tag: str,
) -> None:
    harness = tmp_path / f"scheduled_output_{tag}.cpp"
    harness.write_text(SCHEDULED_OUTPUT_HARNESS)
    object_directory = tmp_path / f"obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", "Top", "--Mdir", str(object_directory),
            "-o", "scheduled_output_sim",
            *(str(path) for path in files), str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_directory / "scheduled_output_sim"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="Verilator is required",
)
def test_direct_sv_scheduler_gates_child_output_actions(tmp_path: Path) -> None:
    module = compile_source(
        SCHEDULED_OUTPUT_SOURCE, top="Top", include_clash=False,
    ).ir
    rtl = tmp_path / "Top.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)
    _simulate_scheduled_output_rtl((rtl,), tmp_path, "sv")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_scheduler_gates_child_output_actions(tmp_path: Path) -> None:
    module = compile_source(
        SCHEDULED_OUTPUT_SOURCE, top="Top", include_clash=False,
    ).ir
    rtl = tuple(generate_verilog(
        emit(module), module.name, tmp_path / "rtl", CLASH_EXECUTABLE,
    ))
    lint_with_verilator(rtl, module.name)
    _simulate_scheduled_output_rtl(rtl, tmp_path, "clash")
