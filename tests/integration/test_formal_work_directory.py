"""Actual SBY execution must not depend on work-directory path spelling."""

from pathlib import Path

import pytest

from zlang.formal import run_verilog_cover, run_verilog_formal
from zlang.ir.formal import CoverStatus, FormalStatus, ProofMode


@pytest.mark.parametrize("spelling", ("absolute", "relative", "nested", "spaces"))
@pytest.mark.parametrize("cover", (False, True))
def test_formal_work_directory_paths(spelling, cover, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    work = {
        "absolute": tmp_path / "abs",
        "relative": Path("rel"),
        "nested": Path("deep/nested/run"),
        "spaces": Path("with spaces/run dir"),
    }[spelling]
    if cover:
        source = "module path_miter(input a); always @* cover(a); endmodule"
        runner = run_verilog_cover
        options = {}
        expected = CoverStatus.WITNESSED
    else:
        source = "module path_miter(input a); always @* assert(a == a); endmodule"
        runner = run_verilog_formal
        options = {"mode": ProofMode.PROVE}
        expected = FormalStatus.PROVEN
    for _ in range(2):
        result = runner(source, top="path_miter", property_id="paths", depth=2,
                        timeout_seconds=20, work_directory=work, **options)
        assert result.status is expected, result.reason
    assert (work / "path_miter.sby").is_file()
    assert (work / "solver.stdout.log").is_file()
    resolved = work.resolve()
    elsewhere = tmp_path / "other invocation cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    replay = runner(source, top="path_miter", property_id="paths", depth=2,
                    timeout_seconds=20, work_directory=resolved, **options)
    assert replay.status is expected, replay.reason
