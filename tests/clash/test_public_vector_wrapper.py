from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang import compile_source
from zlang.backend.clash import emit_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.manifest import BackendArtifact
from zlang.cli import main
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = """
module PublicVectorTop {
    in values : vec<2,u8>
    out echoed : vec<2,u8>
    echoed = values
}
"""


VEC_STRUCT_SOURCE = """
struct Lane {
    value : u8
    valid : bit
}

module PublicVecStructTop {
    in lanes : vec<2,Lane>
    out echoed : vec<2,Lane>
    echoed = lanes
}
"""


RESERVED_STRUCT_ROOT_SOURCE = """
struct ComplexWord {
    re : u8
    im : u8
}

module ReservedStructRootTop {
    in zlang_top_core : u8
    in zlang_top_core_output_re : u8
    in input : ComplexWord
    out output : ComplexWord
    output = input
}
"""


def test_wrapper_plan_and_manifest_use_real_public_array_ports() -> None:
    compilation = compile_source(SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)

    assert wrapper.module_name == "PublicVectorTop"
    assert wrapper.core_module_name == "zlang_core_PublicVectorTop"
    assert "input wire [7:0] values [0:1]" in wrapper.text
    assert "output wire [7:0] echoed [0:1]" in wrapper.text
    assert ".values({values[0], values[1]})" in wrapper.text
    assert "assign echoed[0] = zlang_top_core_echoed[15:8];" in wrapper.text
    assert "assign echoed[1] = zlang_top_core_echoed[7:0];" in wrapper.text

    artifact = bind_artifact_to_public_wrapper(
        emit_artifact(compilation.ir), wrapper
    )
    restored = BackendArtifact.from_json(artifact.to_json())
    bindings = {
        binding.semantic_signal_id: binding
        for binding in restored.bindings
    }
    for semantic, path in (("port:values", "values"), ("port:echoed", "echoed")):
        assert bindings[semantic].rtl_module == "PublicVectorTop"
        assert bindings[semantic].rtl_path == path
        assert bindings[semantic].physical_available
        assert path in wrapper.text


def test_vec_of_struct_maps_field_arrays_to_one_packed_clash_port() -> None:
    compilation = compile_source(VEC_STRUCT_SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)

    assert "input wire [7:0] lanes_value [0:1]" in wrapper.text
    assert "input wire lanes_valid [0:1]" in wrapper.text
    assert "output wire [7:0] echoed_value [0:1]" in wrapper.text
    assert "output wire echoed_valid [0:1]" in wrapper.text
    assert (
        ".lanes({lanes_value[0], lanes_valid[0], "
        "lanes_value[1], lanes_valid[1]})"
    ) in wrapper.text
    assert ".echoed(zlang_top_core_echoed)" in wrapper.text
    assert "zlang_top_core_echoed[17:10]" in wrapper.text
    assert "zlang_top_core_echoed[9]" in wrapper.text
    assert "zlang_top_core_echoed[8:1]" in wrapper.text
    assert "zlang_top_core_echoed[0]" in wrapper.text

    artifact = bind_artifact_to_public_wrapper(
        emit_artifact(compilation.ir), wrapper
    )
    artifact = BackendArtifact.from_json(artifact.to_json())
    leaves = {
        binding.semantic_signal_id: binding
        for binding in artifact.bindings
        if binding.semantic_signal_id in {
            "port:lanes.value", "port:lanes.valid",
            "port:echoed.value", "port:echoed.valid",
        }
    }
    assert set(leaves) == {
        "port:lanes.value", "port:lanes.valid",
        "port:echoed.value", "port:echoed.valid",
    }
    assert {item.rtl_path for item in leaves.values()} == {
        "lanes_value", "lanes_valid", "echoed_value", "echoed_valid",
    }
    assert all(item.physical_available for item in leaves.values())


def test_reserved_struct_root_uses_flattened_core_names_and_private_allocation() -> None:
    compilation = compile_source(RESERVED_STRUCT_ROOT_SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)

    # Clash's PortProduct concatenates the raw root and field labels first.
    assert ".output_re(" in wrapper.text
    assert ".output_im(" in wrapper.text
    assert ".zlang_output_re(" not in wrapper.text
    assert ".zlang_output_im(" not in wrapper.text

    # Legal public leaves may use the old private preferred spellings.  Both
    # the instance and output temporary are then renamed deterministically.
    assert "input wire [7:0] zlang_top_core" in wrapper.text
    assert "input wire [7:0] zlang_top_core_output_re" in wrapper.text
    assert "zlang_core_ReservedStructRootTop zlang_top_core_" in wrapper.text
    assert "wire [7:0] zlang_top_core_output_re_" in wrapper.text
    assert "zlang_top_core__" not in wrapper.text
    assert wrapper.text == ClashPublicTopWrapper.build(compilation.ir).text


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_core_is_packed_but_returned_top_is_native_array(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )

    assert tuple(path.relative_to(tmp_path / "rtl").as_posix() for path in rtl) == (
        "PublicVectorTop.sv",
        "PublicVectorTop.topEntity/zlang_core_PublicVectorTop.v",
    )
    public = rtl[0].read_text()
    core = rtl[1].read_text()
    assert "module PublicVectorTop (" in public
    assert "input wire [7:0] values [0:1]" in public
    assert "output wire [7:0] echoed [0:1]" in public
    assert "module zlang_core_PublicVectorTop" not in public
    assert "module zlang_core_PublicVectorTop" in core
    assert "input wire [15:0] values" in core
    assert "output wire [15:0] echoed" in core
    assert "[0:1]" not in core
    lint_with_verilator(rtl, "PublicVectorTop")

    bench = tmp_path / "tb.sv"
    bench.write_text("""
`timescale 100fs/100fs
module tb;
  logic [7:0] values [0:1];
  wire [7:0] echoed [0:1];
  PublicVectorTop dut(.values, .echoed);
  initial begin
    values[0] = 8'h12;
    values[1] = 8'ha5;
    #1;
    if (echoed[0] !== 8'h12 || echoed[1] !== 8'ha5)
      $fatal(1, "native array ordering");
    $finish;
  end
endmodule
""")
    object_dir = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(object_dir), *(str(path) for path in rtl), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(object_dir / "Vtb"),), cwd=tmp_path, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_vec_of_struct_wrapper_gathers_and_scatters_fields(
    tmp_path: Path,
) -> None:
    compilation = compile_source(VEC_STRUCT_SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "rtl_vec_struct",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    public = next(path for path in rtl if path.suffix == ".sv").read_text()
    core = next(path for path in rtl if path.suffix == ".v").read_text()
    assert "lanes_value [0:1]" in public
    assert "lanes_valid [0:1]" in public
    assert "input wire [17:0] lanes" in core
    assert "lanes_value" not in core
    assert "lanes_valid" not in core
    lint_with_verilator(rtl, "PublicVecStructTop")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_reserved_struct_root_wrapper_uses_existing_core_ports(
    tmp_path: Path,
) -> None:
    compilation = compile_source(RESERVED_STRUCT_ROOT_SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "rtl_reserved_struct",
        CLASH_EXECUTABLE,
        public_wrapper=wrapper,
    )
    public = next(path for path in rtl if path.suffix == ".sv").read_text()
    core = next(path for path in rtl if path.suffix == ".v").read_text()
    assert "output wire [7:0] output_re" in public
    assert "output wire [7:0] output_im" in public
    assert "output wire [7:0] output_re" in core
    assert "output wire [7:0] output_im" in core
    assert "zlang_output_re" not in core
    lint_with_verilator(rtl, "ReservedStructRootTop")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_cli_verilog_directory_always_publishes_public_wrapper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "public_vector.zhl"
    source.write_text(SOURCE)
    output = tmp_path / "rtl"

    status = main((
        str(source),
        "--verilog-dir", str(output),
        "--clash", str(CLASH_EXECUTABLE),
        "--verilator-lint",
    ))

    assert status == 0
    assert capsys.readouterr().out == ""
    assert (output / "PublicVectorTop.sv").is_file()
    assert (output / "PublicVectorTop.topEntity" / "zlang_core_PublicVectorTop.v").is_file()
