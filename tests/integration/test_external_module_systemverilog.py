from dataclasses import replace
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.external import ExternalMappingError, ExternalPhysicalMapping
from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.compiler import compile_source

from tests.parser.test_external_modules import SOURCE


PHYSICAL_SOURCE = """module PhysicalAdd(
  input logic [7:0] lhs,
  input logic [7:0] rhs,
  output logic [8:0] result
);
  assign result = lhs + rhs;
endmodule
"""


def _module_and_mapping():
    module = compile_source(SOURCE).ir
    contract = module.children[0].external_contract
    mapping = ExternalPhysicalMapping.from_text(
        backend="direct_systemverilog",
        logical_extern_identity=contract.semantic_identity,
        physical_module_name="PhysicalAdd",
        port_map=(("a", "lhs"), ("b", "rhs"), ("y", "result")),
        source_text=PHYSICAL_SOURCE,
    )
    return module, mapping




def test_mapping_hash_and_exact_port_map_fail_before_publication() -> None:
    module, mapping = _module_and_mapping()
    with pytest.raises(ExternalMappingError, match="SHA-256"):
        replace(mapping, source_sha256="0" * 64)
    bad_ports = replace(
        mapping,
        port_map=(("a", "lhs"), ("y", "result"), ("b", "rhs")),
    )
    with pytest.raises(SystemVerilogEmissionError, match="must be"):
        emit_artifact(module, external_mappings=(bad_ports,))


def test_direct_sv_external_artifact_is_deterministic_and_lint_clean(
    tmp_path: Path,
) -> None:
    module, mapping = _module_and_mapping()
    first = emit_artifact(module, external_mappings=(mapping,))
    second = emit_artifact(module, external_mappings=(mapping,))
    assert first == second
    assert first.artifact_hash == second.artifact_hash
    assert PHYSICAL_SOURCE in first.text
    assert "PhysicalAdd external_impl" in first.text
    assert ".lhs(a)" in first.text
    assert ".result(y)" in first.text
    assert any(item.semantic_signal_id == "port:y" for item in first.bindings)
    mutated = ExternalPhysicalMapping.from_text(
        backend=mapping.backend,
        logical_extern_identity=mapping.logical_extern_identity,
        physical_module_name=mapping.physical_module_name,
        port_map=mapping.port_map,
        source_text=PHYSICAL_SOURCE.replace("lhs + rhs", "lhs - rhs"),
    )
    assert emit_artifact(
        module, external_mappings=(mutated,)
    ).artifact_hash != first.artifact_hash

    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    source = tmp_path / "external.sv"
    source.write_text(first.text)
    subprocess.run(
        [verilator, "--lint-only", "-Wall", "-Wno-DECLFILENAME", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    testbench = tmp_path / "tb.sv"
    testbench.write_text(
        """module tb;
  logic [7:0] a;
  logic [7:0] b;
  logic [8:0] y;
  Top dut(.a(a), .b(b), .y(y));
  initial begin
    a = 8'd255; b = 8'd1; #1;
    if (y !== 9'd256) $fatal(1, "external add mismatch");
    a = 8'd4; b = 8'd7; #1;
    if (y !== 9'd11) $fatal(1, "external add mismatch");
    $finish;
  end
endmodule
"""
    )
    object_dir = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        [
            verilator,
            "--binary",
            "--timing",
            "-Wall",
            "-Wno-DECLFILENAME",
            "--top-module",
            "tb",
            "--Mdir",
            str(object_dir),
            str(source),
            str(testbench),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        [str(object_dir / "Vtb")],
        check=True,
        capture_output=True,
        text=True,
    )
