from pathlib import Path
import shutil

import pytest

from tests.conformance.catalog import (
    EXPECTED_LANGUAGE_TOUR_TOPS,
    language_tour_source,
    language_tour_tops,
)
from tests.toolchain import CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.toolchain import generate_verilog, lint_with_verilator


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash and Verilator are required",
)
@pytest.mark.toolchain_smoke
@pytest.mark.exhaustive_toolchain
def test_every_all_syntax_module_generates_strict_lint_clean_clash_rtl(
    tmp_path: Path,
) -> None:
    source = language_tour_source()
    assert language_tour_tops() == EXPECTED_LANGUAGE_TOUR_TOPS
    for top in EXPECTED_LANGUAGE_TOUR_TOPS:
        result = compile_source(source, top=top)
        rtl = generate_verilog(
            result.clash,
            top,
            tmp_path / top,
            CLASH_EXECUTABLE,
        )
        lint_with_verilator(rtl, top)
