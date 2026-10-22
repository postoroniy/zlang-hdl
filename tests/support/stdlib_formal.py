"""Bounded tool execution policy for source-authored stdlib RTL contracts."""

from zlang.formal import run_verilog_formal
from zlang.ir.formal import ProofMode


def run(rtl, contract, directory, mode):
    return run_verilog_formal(
        rtl + contract,
        top="contract",
        property_id="stdlib.component",
        mode=mode,
        depth=8,
        timeout_seconds=30,
        route="abc-pdr" if mode is ProofMode.PROVE else "smtbmc",
        solver="pdr" if mode is ProofMode.PROVE else "z3",
        systemverilog=True,
        flatten=True,
        work_directory=directory,
    )
