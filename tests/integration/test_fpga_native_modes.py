# SPDX-License-Identifier: Apache-2.0
"""The physical qualifier checks native modes as well as multiplier counts."""

import json
import pytest
from tools.qualify_fpga_architectures import native_register_modes


@pytest.mark.parametrize("family", ("gowin", "ecp5"))
@pytest.mark.parametrize(
    "top,native",
    (
        ("Multiply1", 0),
        ("Multiply2", 1),
        ("Multiply3", 2),
        ("SymmetricFirAuto3", 0),
        ("Parallel8", 0),
    ),
)
def test_native_modes_reject_wrong_register_configuration(
    family, top, native, tmp_path
):
    if family == "gowin":
        primitive = "MULT18X18"
        params = {
            "AREG": 0,
            "BREG": 0,
            "PIPE_REG": int(native > 0),
            "OUT_REG": int(native > 1),
        }
        params = {k: format(v, "032b") for k, v in params.items()}
        key = "PIPE_REG"
    else:
        primitive = "MULT18X18D"
        params = {
            "REG_INPUTA_CLK": "NONE",
            "REG_INPUTB_CLK": "NONE",
            "REG_PIPELINE_CLK": "CLK0" if native else "NONE",
            "REG_OUTPUT_CLK": "CLK0" if native > 1 else "NONE",
            "RESETMODE": "SYNC",
        }
        key = "REG_PIPELINE_CLK"
    cell = {"type": primitive, "parameters": params}
    design = {"modules": {"top": {"cells": {"mult": cell}}}}
    path = tmp_path / "core.json"
    path.write_text(json.dumps(design))
    assert len(native_register_modes(path, family, top)) == 1
    cell["parameters"][key] = (
        "00000000000000000000000000000000"
        if native
        else "00000000000000000000000000000001"
    )
    if family == "ecp5":
        cell["parameters"][key] = "NONE" if native else "CLK0"
    path.write_text(json.dumps(design))
    with pytest.raises(ValueError, match="unqualified mapped"):
        native_register_modes(path, family, top)
