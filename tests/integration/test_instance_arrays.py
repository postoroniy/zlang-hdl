from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess

import pytest

from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "indexed_instance_array.zhl").read_text()
