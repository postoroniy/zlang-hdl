"""The direct-SV CLI publishes one authoritative BackendArtifact per output."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import zlang.backend.systemverilog.emitter as generic_emitter
import zlang.backend.systemverilog.target as target_emitter
from zlang.cli import main


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("source", "top"),
    (
        (ROOT / "examples" / "alu.zhl", "ALU"),
        (ROOT / "examples" / "hierarchical_protocol_m40.zhl", "ProtocolTop"),
    ),
)
def test_generic_cli_renders_one_artifact_and_reuses_its_text_and_hash(
    source: Path,
    top: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = generic_emitter.emit

    def counted(module, **kwargs):
        calls.append(module.name)
        return original(module, **kwargs)

    monkeypatch.setattr(generic_emitter, "emit", counted)
    rtl = tmp_path / f"{top}.sv"
    manifest = tmp_path / f"{top}.artifact.json"
    source_map = tmp_path / f"{top}.source-map.json"

    assert main([
        str(source),
        "--top", top,
        "--systemverilog", str(rtl),
        "--implementation-manifest", str(manifest),
        "--source-map", str(source_map),
    ]) == 0

    assert calls == [top]
    artifact_data = json.loads(manifest.read_text())
    rtl_text = rtl.read_text()
    assert hashlib.sha256(rtl_text.encode()).hexdigest() == artifact_data["artifact_hash"]
    assert json.loads(source_map.read_text())["artifact_hash"] == artifact_data["artifact_hash"]


def test_target_cli_renders_selected_graph_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = target_emitter.emit_target

    def counted(module, graph, **kwargs):
        calls.append(graph.identity)
        return original(module, graph, **kwargs)

    monkeypatch.setattr(target_emitter, "emit_target", counted)
    rtl = tmp_path / "TargetBRAMMemory.sv"
    manifest = tmp_path / "TargetBRAMMemory.artifact.json"

    assert main([
        str(ROOT / "examples" / "target_bram_memory.zhl"),
        "--top", "TargetBRAMMemory",
        "--target", "xc7z030ffg676-1",
        "--target-architecture", "Xilinx7BRAM36SimpleDualPort",
        "--target-architecture-mode", "required",
        "--systemverilog", str(rtl),
        "--implementation-manifest", str(manifest),
    ]) == 0

    assert len(calls) == 1
    artifact_data = json.loads(manifest.read_text())
    assert hashlib.sha256(rtl.read_bytes()).hexdigest() == artifact_data["artifact_hash"]
    assert artifact_data["implementation"]["implementation_artifact_hash"] == (
        artifact_data["artifact_hash"]
    )
