"""Executable Z3 contracts for AXI4 helpers and a real read endpoint.

These are solver results, unlike tests that only emit a formal artifact. The
full AXI4 endpoint proof matrix is tracked separately in the stdlib coverage
ledger; one held-payload property is not complete AXI4 compliance.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus, ProofMode
from tests.integration.test_axi4_source import AXI4_READ_STABILITY_SOURCE


FORMAL_TOOLS = all(
    shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
)
STDLIB_AXI4 = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4.zhl"
STDLIB_AXI4_SUBORDINATE = (
    Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
)


def _modern_z3_available() -> bool:
    """The distro's Z3 4.8.12 stalls on the endpoint's SMT step zero."""

    executable = shutil.which("z3")
    if not executable:
        return False
    version = subprocess.run(
        [executable, "--version"], check=False, capture_output=True, text=True,
    )
    if version.returncode:
        return False
    parts = version.stdout.strip().split()
    if len(parts) < 3:
        return False
    try:
        return tuple(int(part) for part in parts[2].split(".")[:3]) >= (4, 13, 4)
    except ValueError:
        return False


def _record_source_identity(record_property) -> None:
    record_property("stdlib_source", "stdlib/bus/axi4.zhl")
    record_property(
        "stdlib_source_sha256", hashlib.sha256(STDLIB_AXI4.read_bytes()).hexdigest()
    )


def _record_subordinate_identity(record_property) -> None:
    record_property("stdlib_source", "stdlib/bus/axi4_subordinate.zhl")
    record_property(
        "stdlib_source_sha256",
        hashlib.sha256(STDLIB_AXI4_SUBORDINATE.read_bytes()).hexdigest(),
    )


RESPONSE_CONTRACT = """
import std.bus.axi4
module Axi4ResponseContract {
    clock clk reset rst
    in response : bits<2>
    out is_error : bit
    is_error = axi4_response_is_error(response)
    assert response_code @ clk {
        ((response == 0) & ~is_error) |
        ((response == 1) & ~is_error) |
        ((response == 2) & is_error) |
        ((response == 3) & is_error)
    }
}
"""


GEOMETRY_CONTRACT = """
import std.bus.axi4
module Axi4GeometryContract {
    clock clk reset rst
    in request : Axi4Address<8,1>
    out legal : bit
    legal = axi4_address_valid<AW=8,DW=8,IW=1>(request)
    assert legal_size @ clk { ~legal | (request.size == 0) }
}
"""


def _run_contract(
    source: str, *, top: str, assertion: str, work_directory,
    mode: ProofMode = ProofMode.BMC, depth: int = 3,
) -> FormalStatus:
    compiled = compile_source(source, top=top)
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    design = connect_formal_design(compiled.formal_design, artifact)
    assert any(
        prop.generated_from == f"verification-assert:$module:{assertion}"
        for prop in design.properties
    )
    assert all(prop.non_executable_reason is None for prop in design.properties)
    result = run_verilog_formal(
        emit_harness(design, mode=mode, depth=depth),
        top=f"{top}__safety_verification_formal",
        property_id=f"stdlib.axi4.{assertion}",
        mode=mode,
        depth=depth,
        systemverilog=True,
        timeout_seconds=30,
        work_directory=work_directory,
    )
    return result.status


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
def test_axi4_response_code_contract_and_mutation_with_z3(
    tmp_path, record_property,
) -> None:
    _record_source_identity(record_property)
    assert _run_contract(
        RESPONSE_CONTRACT, top="Axi4ResponseContract", assertion="response_code",
        work_directory=tmp_path / "proof", mode=ProofMode.PROVE,
    ) is FormalStatus.PROVEN
    mutant = RESPONSE_CONTRACT.replace(
        "axi4_response_is_error(response)", "response == 2", 1
    )
    assert mutant != RESPONSE_CONTRACT
    assert _run_contract(
        mutant, top="Axi4ResponseContract", assertion="response_code",
        work_directory=tmp_path / "mutation",
    ) is FormalStatus.FAILED


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
def test_axi4_address_legality_size_implication_with_z3(
    tmp_path, record_property,
) -> None:
    _record_source_identity(record_property)
    assert _run_contract(
        GEOMETRY_CONTRACT, top="Axi4GeometryContract", assertion="legal_size",
        depth=4, work_directory=tmp_path / "pass",
    ) is FormalStatus.BOUNDED_PASS
    mutant = GEOMETRY_CONTRACT.replace(
        "axi4_address_valid<AW=8,DW=8,IW=1>(request)", "1", 1
    )
    assert mutant != GEOMETRY_CONTRACT
    assert _run_contract(
        mutant, top="Axi4GeometryContract", assertion="legal_size",
        depth=4, work_directory=tmp_path / "mutation",
    ) is FormalStatus.FAILED


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys/SBY/Z3 are required")
@pytest.mark.skipif(
    not _modern_z3_available(), reason="AXI4 endpoint requires Z3 >= 4.13.4",
)
def test_axi4_read_subordinate_stability_and_mutation_with_z3(
    tmp_path, record_property,
) -> None:
    _record_subordinate_identity(record_property)
    assert _run_contract(
        AXI4_READ_STABILITY_SOURCE,
        top="Axi4ReadStabilityProof",
        assertion="held_r",
        mode=ProofMode.PROVE,
        depth=6,
        work_directory=tmp_path / "proof",
    ) is FormalStatus.PROVEN
    mutant = AXI4_READ_STABILITY_SOURCE.replace(
        "result.payload = reader.axi.r.payload",
        "result.payload = Axi4ReadData { id=0 data=0 resp=0 last=0 }",
        1,
    )
    assert mutant != AXI4_READ_STABILITY_SOURCE
    assert _run_contract(
        mutant,
        top="Axi4ReadStabilityProof",
        assertion="held_r",
        depth=6,
        work_directory=tmp_path / "mutation",
    ) is FormalStatus.FAILED
