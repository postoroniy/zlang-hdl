import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source


VERILATOR = shutil.which("verilator")


def source(type_name: str) -> str:
    imported = "import std.math.complex\n" if type_name.startswith("Complex") else ""
    return (
        imported
        + "module Cell<type T>{in x:T out y:T y=x} "
        + f"module GenericTop{{in x:{type_name} out y:{type_name} "
        + f"inst c:Cell<T={type_name}>{{x}} y=c.y}}"
    )


def simulate(
    files: list[Path], root: Path, module_name: str, value: int, type_name: str
) -> None:
    harness = root / "harness.cpp"
    if type_name.startswith("Complex"):
        mask = (1 << 18) - 1
        drive = (
            f"d.x_re=0x{(value >> 18) & mask:x}U; "
            f"d.x_im=0x{value & mask:x}U; "
        )
        check = (
            f"((((unsigned long long)d.y_re << 18) | d.y_im) "
            f"== 0x{value:x}ULL)"
        )
    else:
        drive = f"d.x=0x{value:x}ULL; "
        check = f"d.y == 0x{value:x}ULL"
    harness.write_text(
        f'#include "V{module_name}.h"\n'
        f"int main(){{ V{module_name} d; "
        f"{drive}d.eval(); "
        f"return {check} ? 0 : 1; }}\n"
    )
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            module_name, "--Mdir", str(obj), "-o", "generic_type_sim",
            *map(str, files), str(harness),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(obj / "generic_type_sim"),),
        check=True,
        capture_output=True,
        text=True,
    )


CASES = (
    ("u8", 0xA5),
    ("fixed<18,16>", 0x2A55A),
    ("Complex<fixed<18,16>>", 0x812345678),
)


@pytest.mark.parametrize(("type_name", "value"), CASES)
@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_module_type_specialization_direct_sv_simulates(
    type_name: str, value: int
) -> None:
    module = compile_source(source(type_name), top="GenericTop").ir
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "generic.sv"
        rtl.write_text(emit_experimental(module))
        simulate([rtl], root, "GenericTop", value, type_name)
