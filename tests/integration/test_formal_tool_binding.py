# SPDX-License-Identifier: Apache-2.0
"""The real SBY children must execute the CLI selected during discovery."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex

from zlang.formal import FormalToolchainContext, run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode


def test_actual_rtl_proof_executes_selected_solver(tmp_path, monkeypatch):
    actual = shutil.which('z3')
    assert actual is not None, 'mandatory solver qualification needs Z3'
    actual = str(Path(actual).resolve())
    calls = tmp_path / 'solver-calls.txt'
    bin_directory = tmp_path / 'selected-solver'
    bin_directory.mkdir()
    proxy = bin_directory / 'z3'
    proxy.write_text(
        '#!/bin/sh\n'
        f"""printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
"""
        f'exec {shlex.quote(actual)} "$@"\n'
    )
    proxy.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_directory) + os.pathsep + os.environ['PATH'])
    context = FormalToolchainContext.discover(engine='sby', solver='z3')
    assert not context.missing, context.missing
    calls.write_text('')  # Version probes alone must not satisfy execution.
    result = run_verilog_formal(
        """module selected_solver(input clk, input [7:0] a);
wire [7:0] y = a ^ 8'haa;
always @(posedge clk) assert((y ^ 8'haa) == a);
endmodule
""", top='selected_solver', property_id='selected_solver.exact',
        mode=ProofMode.PROVE, depth=2, timeout_seconds=20,
        work_directory=tmp_path / 'proof', toolchain=context,
    )
    assert result.status is FormalStatus.PROVEN, result.reason
    arguments = [line.split() for line in calls.read_text().splitlines()]
    assert any('-smt2' in argv and '-in' in argv for argv in arguments), arguments
