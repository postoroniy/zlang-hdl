"""IEEE-facing nominal rate boundary for the Wi-Fi hierarchy."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_file
from zlang.native_simulation import simulate
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "controller.zhl"
)


def test_wifi_rate_boundary_has_one_typed_sparse_decoder() -> None:
    module = compile_file(SOURCE, top="IeeeSignalHeader24").ir
    for raw in range(8):
        result = simulate(module, raw_rate=raw, length=1)
        assert result["valid"] == int(raw in {1, 2, 4})
        if result["valid"]:
            assert result["header"] & 0xF in {0b1101, 0b0101, 0b1001}


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_wifi_rate_boundary_direct_sv_passes_strict_verilator(
    tmp_path: Path,
) -> None:
    module = compile_file(
        SOURCE,
        top="IeeeSignalHeader24",
    ).ir
    rtl = tmp_path / "IeeeSignalHeader24.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)
