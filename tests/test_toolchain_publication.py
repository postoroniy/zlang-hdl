from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from zlang import compile_source
from zlang.toolchain import ToolchainError
