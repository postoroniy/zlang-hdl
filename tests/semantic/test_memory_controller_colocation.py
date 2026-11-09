# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Global RAM and user-state owners compose without losing state."""

import pytest

from tests.support.memory_controller_cases import PROFILES, controller_source
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.opt import lower, restore
from zlang.semantic import SemanticError


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("ported", (False, True))
def test_memory_and_controller_preserve_typed_state_and_round_trip(profile, ported):
    module = compile_source(controller_source(*profile, ported=ported)).ir
    assert len(module.memories) == 1 and len(module.registers) == 2
    assert not module.memories[0].scheduled
    assert len(module.rules) == 2
    assert restore(lower(module)) == module
    artifact = emit_artifact(module)
    assert artifact.text.count("bank_cells [0:3]") == 1
    assert "bank_read_fire" not in artifact.text
    assert "bank_write_fire" not in artifact.text


@pytest.mark.parametrize("ported", (False, True))
def test_single_hierarchical_child_keeps_its_memory_and_controller(ported):
    module = compile_source(controller_source(ported=ported, parent=True)).ir
    child = module.children[0]
    assert len(child.registers) == 2 and len(child.memories) == 1
    assert emit_artifact(module).text


def test_global_and_rule_owned_controls_remain_mutually_exclusive():
    source = controller_source().replace("q=bank.read_data", "when sample {bank.read(address)} q=bank.read_data")
    with pytest.raises(SemanticError, match="cannot mix global controls"):
        compile_source(source)


def test_global_memory_clock_domain_checks_still_apply():
    source = controller_source().replace(
        "clock clk reset rst", "clock clk reset rst clock other reset ro @other",
    ).replace("out reg cursor:u2=0", "out reg cursor:u2 @other=0")
    with pytest.raises(SemanticError, match="clock-domain mismatch|in domain|requires an explicit clock"):
        compile_source(source)
