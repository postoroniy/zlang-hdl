"""IEEE-facing nominal rate boundary for the Wi-Fi hierarchy."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.clash import emit as emit_clash
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
    / "data_types.zl"
)


def test_wifi_rate_boundary_accepts_only_declared_sparse_codes() -> None:
    module = compile_file(
        SOURCE,
        top="WifiRateCodec",
        include_clash=False,
    ).ir
    (rate_type,) = module.enums
    assert isinstance(rate_type, EnumType)
    assert rate_type.width == 3
    assert rate_type.members == (
        "Continue",
        "Bpsk6",
        "Qpsk12",
        "Qam16_24",
    )
    assert rate_type.codes == (0, 1, 2, 4)

    for raw in range(8):
        result = simulate(module, raw_rate=raw)
        if raw in {0, 1, 2, 4}:
            assert result == {
                "result": {
                    "valid": 1,
                    "decoded": raw,
                    "encoded": raw,
                },
            }
        else:
            assert result == {
                "result": {
                    "valid": 0,
                    "decoded": 0,
                    "encoded": 0,
                },
            }

    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    assert emit_experimental(module) == emit_experimental(module)
    assert emit_clash(module) == emit_clash(module)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_wifi_rate_boundary_direct_sv_passes_strict_verilator(
    tmp_path: Path,
) -> None:
    module = compile_file(
        SOURCE,
        top="WifiRateCodec",
        include_clash=False,
    ).ir
    rtl = tmp_path / "WifiRateCodec.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)
