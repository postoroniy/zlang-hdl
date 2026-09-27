"""The coverage ledger must include every current stdlib declaration."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

from tools.stdlib_formal_coverage import (
    EVIDENCE,
    ROOT,
    STDLIB,
    catalog,
    confirmed_cases_from_junit,
)


def test_stdlib_formal_catalog_is_complete_and_fail_closed() -> None:
    report = catalog()
    assert report["schema"] == 1
    entries = report["entries"]
    identities = [item["identity"] for item in entries]
    assert identities == sorted(set(identities))
    sources = {item["source"] for item in entries}
    assert sources == {
        str(path.relative_to(STDLIB.parent))
        for path in Path(STDLIB).rglob("*.zhl")
    }
    assert set(EVIDENCE) <= set(identities)
    assert all(
        item["source_sha256"]
        == hashlib.sha256((ROOT / item["source"]).read_bytes()).hexdigest()
        for item in entries
    )
    assert all(item["status"] in {
        "evidence-unchecked", "unverified", "static-only", "blocked",
        "unsupported",
    } for item in entries)
    assert all(
        "proof_level" not in item
        for item in entries if item["status"] == "evidence-unchecked"
    )
    assert any(item["status"] == "unverified" for item in entries)
    assert not any(
        item["identity"].startswith("std.bus.axi4::module:")
        and item["status"] == "proven"
        for item in entries
    )
    raw_by_identity = {item["identity"]: item for item in entries}
    verified = catalog({
        evidence["case"]: (
            raw_by_identity[identity]["source"],
            raw_by_identity[identity]["source_sha256"],
        )
        for identity, evidence in EVIDENCE.items()
    })
    by_identity = {item["identity"]: item for item in verified["entries"]}
    assert by_identity[
        "std.bus.axi4::function:axi4_response_is_error"
    ]["status"] == "proven"
    assert by_identity[
        "std.stream.core::module:RvRegisterSlice"
    ]["status"] == "partial"
    assert by_identity[
        "std.bus.axi4_subordinate::module:AXI4ReadSubordinate"
    ]["status"] == "partial"
    assert by_identity[
        "std.bus.axi4::module:AXI4ReadManager"
    ]["status"] == "blocked"
    assert by_identity[
        "std.storage.core::module:StorageAsyncFifo"
    ]["status"] == "unsupported"


def test_junit_evidence_rejects_failed_and_skipped_cases(tmp_path: Path) -> None:
    report = tmp_path / "result.xml"
    source = ROOT / "stdlib/bus/axi4.zhl"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.integration.test_stdlib_axi4_z3" '
        'name="test_axi4_response_code_contract_and_mutation_with_z3">'
        '<properties><property name="stdlib_source" '
        'value="stdlib/bus/axi4.zhl"/>'
        f'<property name="stdlib_source_sha256" value="{digest}"/>'
        '</properties></testcase>'
        '<testcase classname="tests.integration.test_stdlib_stream_z3" '
        'name="test_register_slice_ready_valid_proof_and_mutation">'
        '<failure/></testcase>'
        '<testcase classname="tests.integration.test_ztpu_axi_burst" '
        'name="skipped_case"><skipped/></testcase>'
        '</testsuite></testsuites>'
    )
    confirmed = confirmed_cases_from_junit(report)
    assert confirmed == {
        EVIDENCE["std.bus.axi4::function:axi4_response_is_error"]["case"]: (
            "stdlib/bus/axi4.zhl", digest,
        )
    }
    by_identity = {
        item["identity"]: item for item in catalog(confirmed)["entries"]
    }
    assert by_identity[
        "std.stream.core::module:RvRegisterSlice"
    ]["status"] == "evidence-unchecked"
    stale = dict(confirmed)
    case = EVIDENCE["std.bus.axi4::function:axi4_response_is_error"]["case"]
    stale[case] = ("stdlib/bus/axi4.zhl", "0" * 64)
    assert {
        item["identity"]: item for item in catalog(stale)["entries"]
    }["std.bus.axi4::function:axi4_response_is_error"]["status"] == (
        "evidence-unchecked"
    )
    command = subprocess.run(
        (sys.executable, str(ROOT / "tools/stdlib_formal_coverage.py"),
         "--junit", str(report)),
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert command.returncode != 0
    assert "formal evidence cases did not pass" in command.stderr
