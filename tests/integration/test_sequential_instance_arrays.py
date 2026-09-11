from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "sequential_instance_array.zhl").read_text()

CONSTANT_SOURCE = """
module ConstantLane {
    clock clk reset rst
    in enable:bit in step:u8 out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count + step) }
    value=count
}
module ConstantStateLaneArray {
    clock clk reset rst
    in enables:vec<2,bit> out values:vec<2,u8>
    inst lane[2]:ConstantLane
    generate(i in 0..2) { lane[i].enable=enables[i] lane[i].step=1 }
    values=generate(i in 0..2) lane[i].value
}
"""
