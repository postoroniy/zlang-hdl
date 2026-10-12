# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Deterministic policy and reusable helper RTL for functional OR scatters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product

from zlang.common import stable_digest


MAX_STRUCTURAL_SCATTER_CANDIDATES = 256
MAX_STRUCTURAL_SCATTER_BITS = 65_536
MAX_SCATTER_CHUNK_WRITES = 16
MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES = 24_576


class FunctionalScatterLoweringStrategy(Enum):
    """One deterministic physical lowering for an exact OR-scatter."""

    STRUCTURAL = "structural"
    CHUNKED_PROCEDURAL = "chunked_procedural"


@dataclass(frozen=True)
class FunctionalScatterLoweringChunk:
    """One independently accumulated subset of scatter candidates."""

    ordinal: int
    accumulator: str
    fixed_binders: tuple[tuple[str, int], ...]
    remaining_dimension_ordinals: tuple[int, ...]
    entry_start: int
    entry_stop: int
    write_count: int
    loop_variables: tuple[str, ...]
    enable_temporary: str
    address_temporary: str
    value_temporary: str


@dataclass(frozen=True)
class FunctionalScatterDimensionShape:
    """Compiler-owned binder shape needed by physical scatter partitioning."""

    identity: str
    start: int
    stop: int


@dataclass(frozen=True)
class FunctionalScatterLoweringPlan:
    """Single owner of the bounded direct-SV scatter lowering decision."""

    strategy: FunctionalScatterLoweringStrategy
    effective_candidate_count: int
    result_width: int
    chunks: tuple[FunctionalScatterLoweringChunk, ...]
    contributions_name: str
    reduction_names: tuple[tuple[str, ...], ...]

    @staticmethod
    def select_strategy(
        effective_candidate_count: int,
        result_width: int,
    ) -> FunctionalScatterLoweringStrategy:
        if (
            effective_candidate_count <= MAX_STRUCTURAL_SCATTER_CANDIDATES
            and effective_candidate_count * result_width
            <= MAX_STRUCTURAL_SCATTER_BITS
        ):
            return FunctionalScatterLoweringStrategy.STRUCTURAL
        return FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL


def _reduction_names(
    prefix: str,
    source_count: int,
) -> tuple[tuple[str, ...], ...]:
    levels: list[tuple[str, ...]] = []
    depth = 0
    while source_count > 1:
        target_count = (source_count + 1) // 2
        levels.append(
            tuple(
                f"zlang_scatter_reduce_{prefix}_{depth}_{ordinal}"
                for ordinal in range(target_count)
            )
        )
        source_count = target_count
        depth += 1
    return tuple(levels)


def plan_scatter_lowering(
    *,
    entry_count: int,
    result_width: int,
    dimensions: tuple[FunctionalScatterDimensionShape, ...],
    prefix: str,
) -> FunctionalScatterLoweringPlan:
    """Choose one bounded physical lowering without inspecting typed IR."""

    dimension_lengths = tuple(item.stop - item.start for item in dimensions)
    effective_candidate_count = entry_count
    for length in dimension_lengths:
        effective_candidate_count *= length
    if effective_candidate_count < 1 or result_width < 1:
        raise ValueError(
            "functional scatter has an empty physical candidate/result shape"
        )

    strategy = FunctionalScatterLoweringPlan.select_strategy(
        effective_candidate_count,
        result_width,
    )
    bit_limited_writes = max(
        1, MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES // result_width
    )
    writes_per_chunk = min(MAX_SCATTER_CHUNK_WRITES, bit_limited_writes)

    prefix_depth = len(dimension_lengths)
    suffix_candidates = 1
    for candidate_depth in range(len(dimension_lengths) + 1):
        candidate_suffix = 1
        for length in dimension_lengths[candidate_depth:]:
            candidate_suffix *= length
        if candidate_suffix <= writes_per_chunk:
            prefix_depth = candidate_depth
            suffix_candidates = candidate_suffix
            break
    entries_per_chunk = max(1, writes_per_chunk // suffix_candidates)
    prefix_ranges = tuple(
        range(dimension.start, dimension.stop)
        for dimension in dimensions[:prefix_depth]
    )
    fixed_value_sets = tuple(product(*prefix_ranges)) if prefix_ranges else ((),)
    chunks: list[FunctionalScatterLoweringChunk] = []
    for fixed_values in fixed_value_sets:
        fixed_binders = tuple(
            (dimensions[ordinal].identity, value)
            for ordinal, value in enumerate(fixed_values)
        )
        for entry_start in range(0, entry_count, entries_per_chunk):
            entry_stop = min(entry_count, entry_start + entries_per_chunk)
            ordinal = len(chunks)
            chunk_prefix = f"zlang_scatter_chunk_{prefix}_{ordinal}"
            remaining = tuple(range(prefix_depth, len(dimensions)))
            chunks.append(
                FunctionalScatterLoweringChunk(
                    ordinal=ordinal,
                    accumulator=chunk_prefix,
                    fixed_binders=fixed_binders,
                    remaining_dimension_ordinals=remaining,
                    entry_start=entry_start,
                    entry_stop=entry_stop,
                    write_count=(entry_stop - entry_start) * suffix_candidates,
                    loop_variables=tuple(
                        f"{chunk_prefix}_i_{dimension_ordinal}"
                        for dimension_ordinal in remaining
                    ),
                    enable_temporary=f"{chunk_prefix}_enable",
                    address_temporary=f"{chunk_prefix}_address",
                    value_temporary=f"{chunk_prefix}_value",
                )
            )
    if any(
        chunk.write_count > MAX_SCATTER_CHUNK_WRITES
        or (
            strategy is FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL
            and chunk.write_count * result_width
            > MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES
            and not (
                chunk.write_count == 1
                and result_width > MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES
            )
        )
        for chunk in chunks
    ):
        raise ValueError(
            "functional scatter chunk exceeds its bounded lowering budget"
        )
    return FunctionalScatterLoweringPlan(
        strategy,
        effective_candidate_count,
        result_width,
        tuple(chunks),
        f"zlang_scatter_chunks_{prefix}",
        _reduction_names(prefix, len(chunks)),
    )


def render_scatter_helper(
    *,
    destination_count: int,
    result_width: int,
    element_width: int,
    address_width: int,
    candidate_count: int,
    procedural_write: bool,
) -> tuple[str, str]:
    """Render one shared candidate-contribution helper and balanced OR tree."""

    schema = (
        "zlang-direct-sv-scatter-chunk-v1"
        if procedural_write
        else "zlang-direct-sv-structural-scatter-v1"
    )
    prefix = (
        "zlang_scatter_chunk_helper_"
        if procedural_write
        else "zlang_scatter_structural_helper_"
    )
    count_key = "write_count" if procedural_write else "candidate_count"
    identity = stable_digest(
        {
            "schema": schema,
            "destination_count": destination_count,
            "result_width": result_width,
            "element_width": element_width,
            "address_width": address_width,
            count_key: candidate_count,
        }
    )
    name = f"{prefix}{identity[:12]}"
    write_name = f"{name}_write"
    if element_width == 1:
        shift = "32'($unsigned(address))"
    elif element_width & (element_width - 1) == 0:
        shift = f"(32'($unsigned(address)) << {element_width.bit_length() - 1})"
    else:
        shift = f"(32'($unsigned(address)) * 32'd{element_width})"
    guard = "enable"
    if destination_count < 1 << address_width:
        guard += f" && ($unsigned(address) < {address_width}'d{destination_count})"

    contribution_port = (
        f"  output logic [{result_width - 1}:0] contribution"
        if procedural_write
        else f"  output wire logic [{result_width - 1}:0] contribution"
    )
    lines = [
        f"(* keep_hierarchy = \"yes\" *) module {write_name} (",
        "  input wire logic enable,",
        f"  input wire logic [{address_width - 1}:0] address,",
        f"  input wire logic [{element_width - 1}:0] value,",
        contribution_port,
        ");",
    ]
    if procedural_write:
        lines.extend(
            (
                "  always_comb begin",
                "    contribution = '0;",
                f"    if ({guard}) begin",
                f"      contribution[{shift} +: {element_width}] = value;",
                "    end",
                "  end",
            )
        )
    else:
        lines.append(
            f"  assign contribution = ({guard}) ? "
            f"({result_width}'($unsigned(value)) << {shift}) : '0;"
        )
    lines.extend(
        (
            "endmodule",
            "",
            f"(* keep_hierarchy = \"yes\" *) module {name} (",
            f"  input wire logic [{candidate_count - 1}:0] candidate_enable,",
            f"  input wire logic [{candidate_count * address_width - 1}:0] "
            "candidate_address,",
            f"  input wire logic [{candidate_count * element_width - 1}:0] "
            "candidate_value,",
            f"  output logic [{result_width - 1}:0] result",
            ");",
            f"  logic [{candidate_count * result_width - 1}:0] contributions;",
        )
    )
    if procedural_write:
        lines.extend(
            (
                "  // Every write has an independent contribution owner, so this chunk",
                "  // contains no sequential whole-vector read/modify chain.",
            )
        )
    lines.extend(
        (
            "  generate",
            f"    for (genvar candidate = 0; candidate < {candidate_count}; "
            "candidate = candidate + 1) begin : scatter_candidates",
            f"      {write_name} write_instance (",
            "        .enable(candidate_enable[candidate]),",
            f"        .address(candidate_address[candidate * {address_width} +: "
            f"{address_width}]),",
            f"        .value(candidate_value[candidate * {element_width} +: "
            f"{element_width}]),",
            f"        .contribution(contributions[candidate * {result_width} +: "
            f"{result_width}])",
            "      );",
            "    end",
            "  endgenerate",
        )
    )

    source = "contributions"
    source_count = candidate_count
    depth = 0
    while source_count > 1:
        target_count = (source_count + 1) // 2
        target = f"reduce_{depth}"
        lines.append(f"  logic [{target_count * result_width - 1}:0] {target};")
        for ordinal in range(target_count):
            left = f"{source}[{ordinal * 2 * result_width} +: {result_width}]"
            right_ordinal = ordinal * 2 + 1
            expression = left
            if right_ordinal < source_count:
                right = f"{source}[{right_ordinal * result_width} +: {result_width}]"
                expression = f"{left} | {right}"
            lines.append(
                f"  assign {target}[{ordinal * result_width} +: {result_width}] = "
                f"{expression};"
            )
        source = target
        source_count = target_count
        depth += 1
    if procedural_write:
        lines.extend(
            (
                "  always_comb begin",
                f"    result = {source}[0 +: {result_width}];",
                "  end",
            )
        )
    else:
        lines.append(f"  assign result = {source}[0 +: {result_width}];")
    lines.extend(("endmodule", ""))
    return name, "\n".join(lines)


__all__ = [
    "FunctionalScatterDimensionShape",
    "FunctionalScatterLoweringChunk",
    "FunctionalScatterLoweringPlan",
    "FunctionalScatterLoweringStrategy",
    "MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES",
    "MAX_SCATTER_CHUNK_WRITES",
    "MAX_STRUCTURAL_SCATTER_BITS",
    "MAX_STRUCTURAL_SCATTER_CANDIDATES",
    "plan_scatter_lowering",
    "render_scatter_helper",
]
