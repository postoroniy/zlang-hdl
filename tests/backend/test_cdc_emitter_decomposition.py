from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog.emitter import (
    emit as emit_systemverilog,
    emit_artifact as emit_systemverilog_artifact,
)
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")

# Captured from the monolithic emitters immediately before the mechanical CDC
# extraction.  These hashes make byte-for-byte rendering and artifact identity
# part of the decomposition's regression contract.
EXPECTED_HASHES = {
    ("systemverilog", "cdc_level.zhl"):
        "ebee5e933701fa027d0a6966b149f765152eedb48c556ece422ca3e0c55f1067",
    ("systemverilog", "cdc_pulse.zhl"):
        "460450861d8aca7600fa8653602b37bdec1dedee1608a450a0cfe483b316a16b",
    ("systemverilog", "cdc_handshake.zhl"):
        "1f2fe7014f8f73a6a67232b3374b829cc877ea4897299894399baaa29485aa59",
    ("systemverilog", "cdc_async_fifo.zhl"):
        "9706d65ec0a8ce629c6fc60a3309abb7e152f6241f719f3c3c1dd26fa21608a3",
}




@pytest.mark.parametrize(
    ("example", "top", "push_expression", "pop_expression"),
    (
        (
            "cdc_async_fifo.zhl",
            "CdcAsyncFifo",
            "source_valid && source_ready",
            "destination_valid && destination_ready",
        ),
        (
            "all_syntax.zhl",
            "AggregateProtocolSyntax",
            "i__t_valid && i__t_ready",
            "o__t_valid && o__t_ready",
        ),
    ),
)
def test_async_fifo_handshake_signals_use_explicit_continuous_assignments(
    example: str,
    top: str,
    push_expression: str,
    pop_expression: str,
) -> None:
    """Keep generated handshake nets portable and unambiguously combinational."""

    module = compile_source(
        (ROOT / "examples" / example).read_text(),
        top=top,
    ).ir
    text = emit_systemverilog(module)

    assert "  logic zlang_push, zlang_pop;" in text
    assert f"  assign zlang_push = {push_expression};" in text
    assert f"  assign zlang_pop = {pop_expression};" in text
    assert "wire logic zlang_push =" not in text
    assert "wire logic zlang_pop =" not in text
    assert "logic zlang_push =" not in text
    assert "logic zlang_pop =" not in text


def test_handshake_transfer_signals_use_explicit_continuous_assignments() -> None:
    module = compile_source(
        (ROOT / "examples" / "cdc_handshake.zhl").read_text(),
    ).ir
    text = emit_systemverilog(module)

    declaration = (
        "  logic zlang_source_transfer, zlang_destination_transfer;"
    )
    assert declaration in text
    assert (
        "  assign zlang_source_transfer = source_valid && source_ready;"
        in text
    )
    assert (
        "  assign zlang_destination_transfer = "
        "destination_valid && destination_ready;"
        in text
    )
    for signal in ("zlang_source_transfer", "zlang_destination_transfer"):
        assert f"wire logic {signal} =" not in text
        assert f"logic {signal} =" not in text


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
@pytest.mark.parametrize(
    "example",
    ("cdc_level.zhl", "cdc_pulse.zhl", "cdc_handshake.zhl", "cdc_async_fifo.zhl"),
)
def test_extracted_systemverilog_cdc_is_strict_lint_clean(
    example: str,
    tmp_path: Path,
) -> None:
    module = compile_source(
        (ROOT / "examples" / example).read_text(),
    ).ir
    rtl = tmp_path / f"{module.name}.sv"
    rtl.write_text(emit_systemverilog(module))
    completed = subprocess.run(
        (
            VERILATOR or "verilator",
            "--lint-only",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            module.name,
            str(rtl),
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
