from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import ToolchainError, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "storage_instance_array.zhl").read_text()
