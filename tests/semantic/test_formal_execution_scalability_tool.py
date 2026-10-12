"""Deterministic evidence-tool coverage; solver execution remains opt-in."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tools.formal_execution_scalability import (
    _aggregate_source,
    aggregate_groups,
)
from zlang.compiler import compile_source
from zlang.verification_bundle import load_verification_bundle
from zlang.verification_publication import publish_compilation_verification_bundle


SOURCE = """
module FormalBenchmarkFixture {
    clock clk reset rst
    in a : u4
    out y : u4
    y = a

    assert same @ clk { y == a }
    assert bounded @ clk { y <= 15 }

    contract constrained @ clk {
        require low { a < 8 }
        assert still_same { y == a }
    }
}
"""


def _bundle(directory: Path):
    compilation = compile_source(
        SOURCE,
        top="FormalBenchmarkFixture",
    )
    publish_compilation_verification_bundle(compilation, directory)
    return load_verification_bundle(directory)


def test_benchmark_groups_only_exact_scope_and_assumption_sets(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    groups = aggregate_groups(bundle)

    assert sorted(len(item) for item in groups) == [1, 2]
    for group in groups:
        assert len({item.job.scope_id for item in group}) == 1
        assert len({item.job.assumption_ids for item in group}) == 1
    by_assumptions = {
        group[0].job.assumption_ids: len(group) for group in groups
    }
    assert by_assumptions[()] == 2
    constrained = tuple(
        value for assumptions, value in by_assumptions.items() if assumptions
    )
    assert constrained == (1,)


def test_aggregate_benchmark_is_deterministic_and_bundle_immutable(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "bundle"
    bundle = _bundle(directory)
    manifest = directory / "manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    group = next(item for item in aggregate_groups(bundle) if len(item) == 2)

    first_source, first_top = _aggregate_source(bundle, group)
    second_source, second_top = _aggregate_source(bundle, group)

    assert first_source == second_source
    assert first_top == second_top
    assert first_source.count("module " + first_top) == 1
    assert f"module {first_top}(" in first_source
    assert first_source.count(" dut (") == 1
    assert first_source.count("assert (") == 2
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
