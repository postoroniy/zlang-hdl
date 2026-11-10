# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Direct-SV instance boundaries must carry the exact typed input width."""

from dataclasses import replace
import re
import shutil
import subprocess

import pytest

from tests.support.typed_instance_inputs import source
from zlang.backend.systemverilog import emit_artifact
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.compiler import compile_source
from zlang.ir import UIntType


@pytest.mark.parametrize("width,type_,kind", (
    (5, "u5", "wire"), (5, "s5", "wire"), (65, "u65", "wire"),
    (170, "u170", "wire"), (8, "fixed<8,3>", "wire"),
    (5, "s5", "registered"), (5, "u5", "rv"),
))
def test_compound_child_input_is_exactly_sized_for_both_frontends(tmp_path, width, type_, kind):
    compiled = compile_source(source(width, type_, kind))
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    text = artifact.text
    name = "child_input_payload" if kind == "rv" else "child_x"
    assert re.search(rf"logic(?: signed)? \[{width - 1}:0\] {name};", text)
    assert f"assign {name} =" in text
    if type_.startswith(("s", "fixed")):
        assert f"logic signed [{width - 1}:0] {name};" in text
    rtl = tmp_path / "design.sv"
    rtl.write_text(text)
    commands = (
        ["iverilog", "-g2012", "-s", artifact.module, "-o", str(tmp_path / "out.vvp"), str(rtl)],
        ["verilator", "--lint-only", "--top-module", artifact.module, str(rtl)],
    )
    for command in commands:
        executable = shutil.which(command[0])
        assert executable, f"required frontend missing: {command[0]}"
        result = subprocess.run([executable, *command[1:]], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"warning|%Warning", result.stderr, re.IGNORECASE), result.stderr


def test_temporary_uses_the_existing_collision_safe_name_allocator():
    text = source()
    text = text.replace("out ready,valid:bit", "out ready,valid:bit out child_x:u5=0")
    rtl = emit_artifact(compile_source(text).ir).text
    assert "output logic [4:0] child_x" in rtl
    assert "logic [4:0] child_x;" not in rtl
    assert re.search(r"logic \[4:0\] child_x_\w+;", rtl)


def test_malformed_binding_width_is_rejected_before_emission():
    module = compile_source(source()).ir
    binding = next(item for item in module.instance_bindings if item.port == "x")
    bad = replace(binding, expression=replace(binding.expression, type=UIntType(6)))
    bindings = tuple(bad if item is binding else item for item in module.instance_bindings)
    with pytest.raises(SystemVerilogEmissionError, match="has width 6, expected 5"):
        emit_artifact(replace(module, instance_bindings=bindings))
