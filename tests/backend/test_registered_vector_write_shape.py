# SPDX-License-Identifier: Apache-2.0
"""Indexed output state keeps the compiler-owned packed boundary layout."""

import pytest

from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source


@pytest.mark.parametrize("length", (1, 3, 4))
@pytest.mark.parametrize("type_name", ("bit", "u8", "s8", "u64", "bits<5>", "fixed<8,4>"))
def test_public_vector_state_uses_element_selects(length, type_name):
    text = f"""module Top {{ clock clk reset rst
 in index:u64 in data:{type_name} in enable:bit
 out reg state:vec<{length},{type_name}>=generate(i in 0..{length}) 0
 when enable {{ when index < {length} {{ state[index] <- data }} }} }}"""
    module = compile_source(text, top="Top").ir
    rtl = emit(module)
    width = max(1, (length - 1).bit_length())
    assert f"state[{width}'(" in rtl
    writes = [line for line in rtl.splitlines() if "state[" in line and " <= " in line]
    assert len(writes) == 1
    assert "+:" not in writes[0]
    assert emit(module) == rtl
