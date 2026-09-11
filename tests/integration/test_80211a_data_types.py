"""IEEE-facing nominal rate boundary for the Wi-Fi hierarchy."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_file
from zlang.ir.types import EnumType
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "data_types.zhl"
)




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_wifi_rate_boundary_direct_sv_passes_strict_verilator(
    tmp_path: Path,
) -> None:
    module = compile_file(
        SOURCE,
        top="WifiRateCodec",
    ).ir
    rtl = tmp_path / "WifiRateCodec.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)
