from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


DIRECT = """
import std.storage.core
module Top {
    clock clk reset rst
    in address:u3
    out data:u8
    image:vec<8,u8>=generate(i in 0..8) i
    inst bank:StorageRom<T=u8,N=8,IW=3,image=image>
    bank.address=address
    data=bank.data
}
"""


GENERATED = """
import std.storage.core
fn make_image<type T,N>() {
    generate(i in 0..N) extend<8>(i)
}
module Top {
    clock clk reset rst
    in address:u3
    out data:u8
    inst bank:StorageGeneratedRom<
        T=u8,N=8,IW=3,producer=fn make_image<T=u8,N=8>
    >
    bank.address=address
    data=bank.data
}
"""


FORWARDED_CALLABLE = """
fn widen_value(x:u8)->u9 {
    truncate<9>(extend<9>(x) + 1)
}
module Apply<type A,type B,operation:fn(A)->B> {
    in x:A out y:B
    y=operation(x)
}
module Forward<type A,type B,operation:fn(A)->B> {
    in x:A out y:B
    inst child:Apply<A=A,B=B,operation=fn operation>
    child.x=x
    y=child.y
}
module Top {
    in x:u8 out y:u9
    inst forward:Forward<A=u8,B=u9,operation=fn widen_value>
    forward.x=x
    y=forward.y
}
"""


HARNESS = r"""
#include "VTop.h"
#include "verilated.h"
static void tick(VTop& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}
int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VTop d;
  d.address = 3; d.rst = 1; tick(d);
  if (d.data != 0) return 1;
  d.rst = 0; d.address = 1;
  d.eval(); if (d.data != 0) return 2;
  tick(d); if (d.data != 1) return 3;
  d.address = 7;
  d.eval(); if (d.data != 1) return 4;
  tick(d); if (d.data != 7) return 5;
  d.address = 2; d.rst = 1; tick(d);
  if (d.data != 0) return 6;
  d.rst = 0; tick(d);
  return d.data == 2 ? 0 : 7;
}
"""


CALLABLE_HARNESS = r"""
#include "VTop.h"
#include "verilated.h"
int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VTop d;
  const unsigned values[] = {0, 1, 127, 255};
  for (unsigned value : values) {
    d.x = value; d.eval();
    if (d.y != value + 1) return 1;
  }
  return 0;
}
"""


def _simulate_rtl(
    rtl: tuple[Path, ...], root: Path, *, clash_index_waiver: bool = False
) -> None:
    harness = root / "generic_rom_test.cpp"
    harness.write_text(HARNESS)
    object_dir = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    command = [
        "verilator", "--cc", "--exe", "--build", "--top-module", "Top",
        "--Mdir", str(object_dir), "-o", "rom_sim",
    ]
    if clash_index_waiver:
        command.append("-Wno-WIDTHTRUNC")
    completed = subprocess.run(
        (*command, *(str(path) for path in rtl), str(harness)),
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "rom_sim"),),
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def _simulate_forwarded_callable(
    rtl: tuple[Path, ...], root: Path, *, tag: str
) -> None:
    harness = root / f"forwarded_callable_{tag}.cpp"
    harness.write_text(CALLABLE_HARNESS)
    object_dir = root / f"callable_obj_{tag}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module", "Top",
            "--Mdir", str(object_dir), "-o", "callable_sim",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "callable_sim"),),
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.parametrize("source", (DIRECT, GENERATED))
@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_generic_rom_wrappers_emit_strict_direct_sv(
    source: str, tmp_path: Path
) -> None:
    module = compile_source(source, include_clash=False).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / "Top.sv"
    rtl.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    assert len(artifact.companions) == 1
    lint_with_verilator((rtl,), "Top")
    _simulate_rtl((rtl,), tmp_path)


@pytest.mark.parametrize("source", (DIRECT, GENERATED))
@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash and Verilator are required",
)
def test_generic_rom_wrappers_compile_with_real_clash_1_11(
    source: str, tmp_path: Path
) -> None:
    module = compile_source(source).ir
    artifact = emit_clash_artifact(module)
    rtl = generate_verilog(
        artifact.text,
        "Top",
        tmp_path / "rtl",
        companions=artifact.companions,
    )
    assert len(artifact.companions) == 1
    assert (tmp_path / "rtl" / artifact.companions[0].logical_path).is_file()
    # Clash 1.11's upstream romFile selector uses a host-width Enum index.
    # Its sole WIDTHTRUNC warning is already documented by the base ROM slice;
    # use the same narrow waiver and keep every other warning fatal.
    verilator = shutil.which("verilator")
    assert verilator is not None
    completed = subprocess.run(
        (
            verilator,
            "--lint-only",
            "-Wno-WIDTHTRUNC",
            "--top-module",
            "Top",
            *(str(path) for path in rtl),
        ),
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    _simulate_rtl(rtl, tmp_path, clash_index_waiver=True)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_forwarded_callable_parameter_is_behavioral_in_direct_sv(
    tmp_path: Path,
) -> None:
    module = compile_source(
        FORWARDED_CALLABLE, top="Top", include_clash=False
    ).ir
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    rtl = tmp_path / "Top.sv"
    rtl.write_text(first.text)
    lint_with_verilator((rtl,), "Top")
    _simulate_forwarded_callable((rtl,), tmp_path, tag="direct")


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash and Verilator are required",
)
def test_forwarded_callable_parameter_is_behavioral_in_real_clash(
    tmp_path: Path,
) -> None:
    module = compile_source(FORWARDED_CALLABLE, top="Top").ir
    artifact = emit_clash_artifact(module)
    rtl = generate_verilog(artifact.text, "Top", tmp_path / "clash")
    lint_with_verilator(rtl, "Top")
    _simulate_forwarded_callable(rtl, tmp_path, tag="clash")
