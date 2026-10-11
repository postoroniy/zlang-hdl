"""Declarative project, simulation, and formal workflow contracts."""

from __future__ import annotations

from pathlib import Path

from zlang.compiler import compile_file
from zlang.implementation_request import parse_selected_profile
from zlang.platform_constraints import parse_platform_profile
from zlang.project import ProjectLock, ProjectManifest
from zlang.sim_cli import _read_events


ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "examples/projects/80211a_transmitter"
MANIFEST = PROJECT / "zlang.toml"
LOCK = PROJECT / "zlang.lock"
EVENTS = PROJECT / "simulation/demo-events.jsonl"
FORMAL = PROJECT / "src/formal.zhl"


def test_project_manifest_lock_and_compact_events_are_self_contained() -> None:
    manifest = ProjectManifest.load(MANIFEST)
    lock = ProjectLock.load(LOCK)
    assert manifest.package == "wifi80211a_transmitter"
    assert manifest.source_directory == PROJECT / "src"
    assert manifest.dependencies == ()
    assert lock.manifest_resolution_digest == manifest.resolution_digest
    assert lock.packages == ()

    profile = parse_selected_profile(manifest, "portable")
    assert profile.backend is not None
    assert profile.backend.kind.value == "systemverilog"
    assert profile.backend.mode.value == "required"
    platform = parse_platform_profile(manifest, "portable")
    assert platform is not None
    assert len(platform.clocks) == 1
    assert platform.clocks[0].semantic_clock == "clk"
    assert str(platform.clocks[0].period_ns) == "20"

    physical_lines = [line for line in EVENTS.read_text().splitlines() if line]
    events = _read_events(EVENTS)
    assert len(physical_lines) == 8
    assert len(events) == 400
    assert events[0]["reset"] == {"rst": True}
    assert events[1]["reset"] == {"rst": False}
    assert events[1]["set"]["command"]["valid"] == 1
    assert events[2]["set"]["psdu"]["payload"] == {
        "data": 0x55,
        "first": 1,
        "last": 1,
        "meta": 1,
    }
    ready = [event["set"]["output"]["ready"] for event in events]
    assert ready.count(0) == 27
    assert ready.count(1) == 373


def test_formal_witness_uses_executable_production_rate_logic() -> None:
    compilation = compile_file(FORMAL, top="Ieee80211aSignalFormal")
    properties = compilation.formal_design.properties
    assert {
        item.source_origin.construct
        for item in properties
        if item.source_origin is not None
    } == {"assert rate_encoding", "assert valid_rate", "assert valid_length"}
    assert all(item.non_executable_reason is None for item in properties)
    assert len(compilation.formal_design.covers) == 1
    cover = compilation.formal_design.covers[0]
    assert cover.source_origin is not None
    assert cover.source_origin.construct == "cover bpsk_one_byte"
    assert cover.non_executable_reason is None


def test_project_documentation_uses_zlang_commands_without_task_runner() -> None:
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    assert not (PROJECT / "Makefile").exists()
    assert not (PROJECT / "tools/demo.py").exists()
    assert "zlang lock update" in readme
    assert "zlang sim src/transmitter.zhl" in readme
    assert "--compare-with verilator" in readme
    assert "--compare-artifacts" not in readme
    assert "--trace build/wifi80211a.vcd" in readme
    assert "zlang src/formal.zhl" in readme
    assert "--profile portable" in readme
    assert "--constraints-sdc build/Ieee80211aTransmitter.sdc" in readme
    assert "make sim" not in readme
