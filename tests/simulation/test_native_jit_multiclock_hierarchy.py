"""Multi-clock hierarchy is mapped and erased entirely by the compiler."""

from __future__ import annotations

from pathlib import Path

import pytest

import zlang
from tests.simulation.differential import run_differential


SOURCE_CLOCK = "source_clock"
DESTINATION_CLOCK = "destination_clock"


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def _cdc_level_hierarchy(tmp_path: Path, *, nested: bool) -> Path:
    middle = """
    module CdcMiddle {
      clock source_clock reset source_reset @source_clock
      clock destination_clock reset destination_reset @destination_clock
      in level:bit @source_clock
      out synced:bit @destination_clock
      inst leaf:CdcLeaf
      leaf.level=level
      synced=leaf.synced
    }
    """ if nested else ""
    child_type = "CdcMiddle" if nested else "CdcLeaf"
    return _source(
        tmp_path,
        "nested_cdc_level" if nested else "cdc_level_hierarchy",
        f"""
        module CdcLeaf {{
          clock source_clock reset source_reset @source_clock
          clock destination_clock reset destination_reset @destination_clock
          in level:bit @source_clock
          out synced:bit @destination_clock
          connect level -> synced {{ crossing sync_level }}
        }}
        {middle}
        module CdcTop {{
          clock source_clock reset source_reset @source_clock
          clock destination_clock reset destination_reset @destination_clock
          in level:bit @source_clock
          out synced:bit @destination_clock
          inst child:{child_type}
          child.level=level
          synced=child.synced
        }}
        """,
    )


def _async_fifo_hierarchy(tmp_path: Path) -> Path:
    return _source(
        tmp_path,
        "async_fifo_hierarchy",
        """
        module AsyncFifoChild {
          clock source_clock reset source_reset @source_clock
          clock destination_clock reset destination_reset @destination_clock
          in source:rv<u8> @source_clock
          out destination:rv<u8> @destination_clock
          connect source -> destination { crossing async_fifo(4) }
        }
        module AsyncFifoTop {
          clock source_clock reset source_reset @source_clock
          clock destination_clock reset destination_reset @destination_clock
          in source:rv<u8> @source_clock
          out destination:rv<u8> @destination_clock
          inst bridge:AsyncFifoChild
          connect source -> bridge.source
          connect bridge.destination -> destination
        }
        """,
    )


EVENTS = (
    {
        "set": {"level": 0},
        "reset": {"source_reset": True, "destination_reset": True},
        "edges": [SOURCE_CLOCK, DESTINATION_CLOCK],
    },
    {
        "set": {"level": 1},
        "reset": {"source_reset": False, "destination_reset": False},
        "edges": [SOURCE_CLOCK],
    },
    {"edges": [DESTINATION_CLOCK]},
    {"edges": [DESTINATION_CLOCK]},
    {"edges": [SOURCE_CLOCK, DESTINATION_CLOCK]},
)


@pytest.mark.parametrize("nested", (False, True))
def test_multiclock_child_matches_reference_native_and_direct_sv(
    tmp_path: Path,
    nested: bool,
) -> None:
    source = _cdc_level_hierarchy(tmp_path, nested=nested)
    trace = run_differential(
        source,
        top="CdcTop",
        events=EVENTS,
        directory=tmp_path / ("nested_rtl" if nested else "direct_rtl"),
    )

    assert trace.reference == trace.native == trace.direct_sv
    assert trace.native[-1]["synced"] == 1


def test_multiclock_hierarchy_plan_has_only_root_domains_and_primitives(
    tmp_path: Path,
) -> None:
    source = _cdc_level_hierarchy(tmp_path, nested=True)
    first = zlang.sim.compile(source, top="CdcTop", engine="reference").plan
    second = zlang.sim.compile(source, top="CdcTop", engine="reference").plan

    assert first.to_bytes() == second.to_bytes()
    assert first.payload["canonical_ir_identity"].startswith("hierarchical:")
    assert [item["clock"] for item in first.payload["domains"]] == [
        SOURCE_CLOCK,
        DESTINATION_CLOCK,
    ]
    assert {item["clock"] for item in first.payload["edge_programs"]} == {
        SOURCE_CLOCK,
        DESTINATION_CLOCK,
    }
    assert not {
        "sync_level",
        "pulse_toggle",
        "handshake",
        "async_fifo",
        "module",
        "instance",
    } & {item["op"] for item in first.payload["nodes"]}


def test_async_fifo_child_matches_reference_native_and_direct_sv(
    tmp_path: Path,
) -> None:
    source = _async_fifo_hierarchy(tmp_path)
    events = (
        {
            "set": {
                "source": {"payload": 0, "valid": 0},
                "destination": {"ready": 0},
            },
            "reset": {"source_reset": True, "destination_reset": True},
            "edges": [SOURCE_CLOCK, DESTINATION_CLOCK],
        },
        {
            "set": {"source": {"payload": 0x31, "valid": 1}},
            "reset": {"source_reset": False, "destination_reset": False},
            "edges": [SOURCE_CLOCK],
        },
        {
            "set": {"source": {"payload": 0x52, "valid": 1}},
            "edges": [SOURCE_CLOCK],
        },
        {"set": {"source": {"payload": 0, "valid": 0}}, "edges": [SOURCE_CLOCK]},
        {"edges": [DESTINATION_CLOCK]},
        {"edges": [DESTINATION_CLOCK]},
        {"set": {"destination": {"ready": 1}}, "edges": [DESTINATION_CLOCK]},
        {"edges": [SOURCE_CLOCK, DESTINATION_CLOCK]},
        {"edges": [SOURCE_CLOCK, DESTINATION_CLOCK]},
    )
    trace = run_differential(
        source,
        top="AsyncFifoTop",
        events=events,
        directory=tmp_path / "async_fifo_rtl",
    )

    assert trace.reference == trace.native == trace.direct_sv
    transferred = [
        item["destination_payload"]
        for item in trace.native
        if item["destination_valid"]
    ]
    assert transferred == [0x31, 0x52]
