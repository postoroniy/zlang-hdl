from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import FunctionalRegion
from zlang.opt.lowering import lower
from zlang.toolchain import lint_with_verilator


LIFTED_SOURCE = """
fn c6<K>() -> u6 { K }
fn selected<D>(value:u6)->bit { value == c6<K=D>() }
module LiftedBackend {
    in value:u6
    out hits:vec<32,bit>
    hits = generate(dst in 0..32) selected<D=dst*2+1>(value)
}
"""


def test_lifted_generic_has_deterministic_canonical_and_rtl_identity() -> None:
    first = compile_source(LIFTED_SOURCE, top="LiftedBackend").ir
    second = compile_source(LIFTED_SOURCE, top="LiftedBackend").ir
    assert isinstance(first.assignments[0].expression, FunctionalRegion)
    assert lower(first) == lower(second)
    assert emit_experimental(first) == emit_experimental(second)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_lifted_generic_direct_sv_passes_strict_verilator(tmp_path: Path) -> None:
    module = compile_source(LIFTED_SOURCE, top="LiftedBackend").ir
    rtl = tmp_path / "LiftedBackend.sv"
    rtl.write_text(emit_experimental(module), encoding="utf-8")
    lint_with_verilator((rtl,), module.name)
