# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Freeze native endpoint comparisons independently of RTL spelling.

Native U/X comparisons use a bit-precise network. Their partial-known results
are deliberately checked directly rather than borrowed from an SV oracle.
"""

from pathlib import Path

import pytest

from zlang import sim


# Each pair is one relation and its operand-reversed spelling. Expected digits
# below follow this group order; they are manually derived native truth tables.
_MIRRORS = (
    ("le_max", "max_ge"),
    ("ge_min", "min_le"),
    ("lt_max", "max_gt"),
    ("gt_min", "min_lt"),
    ("ge_max", "max_le"),
    ("le_min", "min_ge"),
)
_OUTSIDE = ("gt_max", "max_lt", "lt_min", "min_gt")

_UNSIGNED = (
    ("000", "111001"),
    ("001", "111100"),
    ("010", "111100"),
    ("011", "111100"),
    ("100", "111100"),
    ("101", "111100"),
    ("110", "111100"),
    ("111", "110110"),
    ("uuu", "uuuuuu"),
    ("xxx", "xxxxxx"),
    ("0uu", "1u1u0u"),
    ("1uu", "u1u1u0"),
    ("0xx", "1x1x0x"),
    ("1xx", "x1x1x0"),
    ("u0x", "uxux0x"),
    ("u1x", "xuxux0"),
    ("01u", "111100"),
    ("10u", "111100"),
    ("00u", "1u1u0u"),
    ("11u", "u1u1u0"),
)
_SIGNED = (
    ("000", "111100"),
    ("001", "111100"),
    ("010", "111100"),
    ("011", "110110"),
    ("100", "111001"),
    ("101", "111100"),
    ("110", "111100"),
    ("111", "111100"),
    ("uuu", "uuuuuu"),
    ("xxx", "xxxxxx"),
    ("0uu", "u1u1u0"),
    ("1uu", "1u1u0u"),
    ("0xx", "x1x1x0"),
    ("1xx", "1x1x0x"),
    ("u0x", "uxux0x"),
    ("u1x", "xuxux0"),
    ("01u", "u1u1u0"),
    ("10u", "1u1u0u"),
    ("00u", "111100"),
    ("11u", "111100"),
)


def _source(signed: bool) -> str:
    type_name, minimum, maximum = ("s3", "-4", "3") if signed else ("u3", "0", "7")
    return f"""module NativeEndpoint {{
      in value:{type_name}
      out le_max,max_ge,ge_min,min_le:bit
      out lt_max,max_gt,gt_min,min_lt:bit
      out ge_max,max_le,le_min,min_ge:bit
      out gt_max,max_lt,lt_min,min_gt:bit
      le_max=value<={maximum} max_ge={maximum}>=value
      ge_min=value>={minimum} min_le={minimum}<=value
      lt_max=value<{maximum} max_gt={maximum}>value
      gt_min=value>{minimum} min_lt={minimum}<value
      ge_max=value>={maximum} max_le={maximum}<=value
      le_min=value<={minimum} min_ge={minimum}>=value
      gt_max=value>{maximum} max_lt={maximum}<value
      lt_min=value<{minimum} min_gt={minimum}>value
    }}"""


@pytest.mark.parametrize("signed", (False, True), ids=("unsigned", "signed"))
@pytest.mark.parametrize("logic_state", (False, True), ids=("binary", "logic"))
def test_native_endpoint_comparisons_keep_exact_partial_known_results(
    tmp_path: Path, signed: bool, logic_state: bool,
) -> None:
    source = tmp_path / "native_endpoint.zhl"
    source.write_text(_source(signed), encoding="utf-8")
    program = sim.compile(source, top="NativeEndpoint", logic_state=logic_state)
    matrix = _SIGNED if signed else _UNSIGNED
    with program.create() as instance:
        for spelling, expected in matrix:
            if logic_state:
                instance.set_logic("value", spelling)
            elif set(spelling) <= {"0", "1"}:
                value = int(spelling, 2)
                instance.set("value", value - 8 if signed and value >= 4 else value)
            else:
                continue
            outputs = instance.eval()
            actual = {
                name: value.to_bits() if logic_state else str(value)
                for name, value in outputs.items()
            }
            wanted = {
                name: bit
                for names, bit in zip(_MIRRORS, expected, strict=True)
                for name in names
            }
            wanted.update((name, "0") for name in _OUTSIDE)
            assert actual == wanted, (signed, logic_state, spelling, actual)
