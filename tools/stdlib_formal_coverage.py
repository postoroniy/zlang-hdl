"""Report exact stdlib formal coverage without treating absence as success.

The parser owns declaration discovery.  Evidence is intentionally a small,
reviewed table of executable test cases, not an inference from names or prose.
Nightly CI runs those cases before publishing this inventory as an artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree

from zlang._version import __version__
from zlang.formal import FormalToolchainContext
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[1]
STDLIB = ROOT / "stdlib"

# A status applies only to the exact contract and specialization named here.
# New declarations are automatically reported as unverified, never as proven.
EVIDENCE: dict[str, dict[str, str]] = {
    "std.bus.axi4::function:axi4_response_is_error": {
        "status": "proven",
        "proof_level": "proven",
        "case": "tests/integration/test_stdlib_axi4_z3.py::"
        "test_axi4_response_code_contract_and_mutation_with_z3",
        "contract": "all four bits<2> response codes",
        "specialization": "response:bits<2>",
        "mode": "prove",
        "depth": "3",
        "assumptions": "initial reset; no AXI environment assumption",
    },
    "std.bus.axi4::function:axi4_address_valid": {
        "status": "partial",
        "proof_level": "bounded-pass:4",
        "case": "tests/integration/test_stdlib_axi4_z3.py::"
        "test_axi4_address_legality_size_implication_with_z3",
        "contract": "legal address implies size zero for an 8-bit data bus",
        "specialization": "AW=8,DW=8,IW=1",
        "mode": "bmc",
        "depth": "4",
        "assumptions": "initial reset; no AXI environment assumption",
    },
    "std.bus.axi4_subordinate::module:AXI4ReadSubordinate": {
        "status": "partial",
        "proof_level": "proven",
        "case": "tests/integration/test_stdlib_axi4_z3.py::"
        "test_axi4_read_subordinate_stability_and_mutation_with_z3",
        "contract": "held R payload/valid stability under backpressure",
        "specialization": "AW=8,DW=8,IW=1,D=2,BW=1",
        "mode": "prove",
        "depth": "6",
        "assumptions": "initial reset and fixed legal request/backend beat in harness",
    },
    "std.stream.core::module:RvRegisterSlice": {
        "status": "partial",
        "proof_level": "proven",
        "case": "tests/integration/test_stdlib_stream_z3.py::"
        "test_register_slice_ready_valid_proof_and_mutation",
        "contract": "ready/valid output stability for T=u8",
        "specialization": "T=u8",
        "mode": "prove",
        "depth": "4",
        "assumptions": "initial reset and generated input ready/valid contract",
    },
    "std.stream.core::module:RvMux2": {
        "status": "partial",
        "proof_level": "bounded-pass:3",
        "case": "tests/integration/test_stdlib_stream_z3.py::"
        "test_combinational_router_contract_and_mutation_with_z3[mux]",
        "contract": "selected payload plus selected valid/backpressure routing",
        "specialization": "T=u8",
        "mode": "bmc",
        "depth": "3",
        "assumptions": "initial reset and generated input ready/valid contracts",
    },
    "std.stream.core::module:RvDemux2": {
        "status": "partial",
        "proof_level": "bounded-pass:3",
        "case": "tests/integration/test_stdlib_stream_z3.py::"
        "test_combinational_router_contract_and_mutation_with_z3[demux]",
        "contract": "selected valid plus selected-sink backpressure routing",
        "specialization": "T=u8",
        "mode": "bmc",
        "depth": "3",
        "assumptions": "initial reset and generated input ready/valid contracts",
    },
    "std.stream.core::module:RvSkidBuffer": {
        "status": "partial",
        "proof_level": "bounded-pass:6",
        "case": "tests/integration/test_stdlib_stream_z3.py::"
        "test_bounded_fifo_ready_valid_and_mutation[stream_skid_buffer-skid]",
        "contract": "ready/valid output stability for T=u8,D=2",
        "specialization": "T=u8,D=2",
        "mode": "bmc",
        "depth": "6",
        "assumptions": "initial reset and generated input ready/valid contract",
    },
    "std.stream.core::module:RvFifo": {
        "status": "partial",
        "proof_level": "bounded-pass:6",
        "case": "tests/integration/test_stdlib_stream_z3.py::"
        "test_bounded_fifo_ready_valid_and_mutation[stream_core-queue]",
        "contract": "ready/valid output stability for T=FrameBeat<u8,u2>,D=4",
        "specialization": "T=FrameBeat<u8,u2>,D=4",
        "mode": "bmc",
        "depth": "6",
        "assumptions": "initial reset and generated input ready/valid contract",
    },
    "std.bus.axi_burst::module:AXI4BurstReader": {
        "status": "partial",
        "proof_level": "bounded-pass:6",
        "case": "tests/integration/test_ztpu_axi_burst.py::"
        "test_root_ready_valid_safety_verification_executes_with_real_sby_z3"
        "[ZtpuAxiBurstReader64x32-expected_assertions0]",
        "contract": "root ready/valid safety in 64x32 ZTPU reader composition",
        "specialization": "AW=64,DW=32,LW=16",
        "mode": "bmc",
        "depth": "6",
        "assumptions": "initial reset and generated input ready/valid contract",
    },
    "std.bus.axi_burst::module:AXI4BurstWriter": {
        "status": "partial",
        "proof_level": "bounded-pass:6",
        "case": "tests/integration/test_ztpu_axi_burst.py::"
        "test_root_ready_valid_safety_verification_executes_with_real_sby_z3"
        "[ZtpuAxiBurstWriter64x32-expected_assertions1]",
        "contract": "root ready/valid safety in 64x32 ZTPU writer composition",
        "specialization": "AW=64,DW=32,LW=16",
        "mode": "bmc",
        "depth": "6",
        "assumptions": "initial reset and generated input ready/valid contract",
    },
}

KNOWN_BLOCKERS = {
    "std.bus.axi4::module:AXI4ReadManager": (
        "blocked", "reduced 8x8 two-slot ready/valid Z3 BMC times out"
    ),
    "std.storage.core::module:StorageAsyncFifo": (
        "unsupported", "no end-to-end multi-domain CDC proof contract"
    ),
}


def confirmed_cases_from_junit(path: Path) -> dict[str, tuple[str, str]]:
    """Accept only passing cases that publish their exact stdlib source hash."""

    root = ElementTree.parse(path).getroot()
    confirmed: dict[str, tuple[str, str]] = {}
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            continue
        classname, name = case.get("classname"), case.get("name")
        if not classname or not name:
            continue
        source = classname.replace(".", "/") + ".py"
        properties = {
            prop.get("name"): prop.get("value")
            for prop in case.findall("./properties/property")
        }
        source_identity = properties.get("stdlib_source")
        source_hash = properties.get("stdlib_source_sha256")
        if source_identity and source_hash:
            confirmed[f"{source}::{name}"] = (source_identity, source_hash)
    return confirmed


def catalog(
    confirmed_cases: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, object]:
    confirmed_cases = confirmed_cases or {}
    entries: list[dict[str, str]] = []
    for source in sorted(STDLIB.rglob("*.zhl")):
        unit = "std." + ".".join(source.relative_to(STDLIB).with_suffix("").parts)
        source_bytes = source.read_bytes()
        syntax = parse(source_bytes.decode("utf-8"))
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        declarative = source.relative_to(STDLIB).parts[0] in {"arch", "target"}
        for module in (*syntax.submodules, syntax):
            key = f"{unit}::module:{module.name}"
            entry = {
                "identity": key,
                "source": str(source.relative_to(ROOT)),
                "source_sha256": source_hash,
            }
            if declarative:
                if any((
                    module.assignments, module.registers, module.instances,
                    module.rules, module.fifos, module.memories,
                )):
                    raise ValueError(f"executable module classified static-only: {key}")
                entry["status"] = "static-only"
            else:
                entry["status"] = "unverified"
            if key in KNOWN_BLOCKERS:
                entry["status"], entry["blocker"] = KNOWN_BLOCKERS[key]
            evidence = EVIDENCE.get(key)
            if evidence:
                entry.update(evidence)
                if confirmed_cases.get(evidence["case"]) != (
                    entry["source"], source_hash,
                ):
                    entry["status"] = "evidence-unchecked"
                    entry.pop("proof_level", None)
            entries.append(entry)
        for function in syntax.functions:
            key = f"{unit}::function:{function.name}"
            entry = {
                "identity": key,
                "source": str(source.relative_to(ROOT)),
                "source_sha256": source_hash,
                "status": "unverified",
            }
            evidence = EVIDENCE.get(key)
            if evidence:
                entry.update(evidence)
                if confirmed_cases.get(evidence["case"]) != (
                    entry["source"], source_hash,
                ):
                    entry["status"] = "evidence-unchecked"
                    entry.pop("proof_level", None)
            entries.append(entry)
        static_declarations = (
            ("struct", syntax.structs),
            ("enum", syntax.enums),
            ("protocol", syntax.protocols),
            ("type-alias", syntax.type_aliases),
            ("architecture", syntax.architecture_templates),
            ("resource", syntax.resource_definitions),
            ("target-family", syntax.target_families),
            ("target-instance", syntax.target_instances),
        )
        for kind, declarations in static_declarations:
            for declaration in declarations:
                entries.append({
                    "identity": f"{unit}::{kind}:{declaration.name}",
                    "source": str(source.relative_to(ROOT)),
                    "source_sha256": source_hash,
                    "status": "static-only",
                })
    entries.sort(key=lambda item: item["identity"])
    keys = {entry["identity"] for entry in entries}
    if len(keys) != len(entries):
        raise ValueError("stdlib formal inventory contains duplicate identities")
    stale = sorted(EVIDENCE.keys() - keys)
    if stale:
        raise ValueError("formal evidence names missing stdlib declarations: " + ", ".join(stale))
    stale_blockers = sorted(KNOWN_BLOCKERS.keys() - keys)
    if stale_blockers:
        raise ValueError(
            "formal blockers name missing stdlib declarations: "
            + ", ".join(stale_blockers)
        )
    context = FormalToolchainContext.discover()
    return {
        "schema": 1,
        "compiler_version": __version__,
        "tool_versions": dict(context.versions),
        "missing_tools": list(context.missing),
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path,
                        help="JUnit report from this exact checkout's formal run")
    args = parser.parse_args()
    confirmed = (
        confirmed_cases_from_junit(args.junit)
        if args.junit is not None else {}
    )
    report = catalog(confirmed)
    if args.junit is not None:
        missing = sorted(
            item["case"] for item in report["entries"]
            if item["status"] == "evidence-unchecked"
        )
        if missing:
            parser.error("formal evidence cases did not pass: " + ", ".join(missing))
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
