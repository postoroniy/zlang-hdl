from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
STRUCTURAL_ROOT = ROOT / "examples" / "structural"


@dataclass(frozen=True)
class StructuralWitness:
    slug: str
    source: Path
    top: str
    purpose: str
    variants: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    egraph_observation: bool = False

    def source_text(self, variant: str = "small") -> str:
        text = self.source.read_text(encoding="utf-8")
        overrides = dict(dict(self.variants)[variant])
        header = re.search(
            rf"\bmodule\s+{re.escape(self.top)}\s*<(?P<parameters>[^>]*)>",
            text,
        )
        if overrides and header is None:
            raise ValueError(f"{self.slug} top '{self.top}' has no parameter list")
        parameters_start = header.start("parameters") if header is not None else 0
        parameters_end = header.end("parameters") if header is not None else 0
        parameters = text[parameters_start:parameters_end]
        for name, value in overrides.items():
            pattern = re.compile(rf"\b{re.escape(name)}\s*=\s*\d+")
            if pattern.search(parameters) is None:
                raise ValueError(
                    f"{self.slug} variant '{variant}' cannot find default for {name}"
                )
            parameters = pattern.sub(f"{name}={value}", parameters, count=1)
        if header is not None:
            text = text[:parameters_start] + parameters + text[parameters_end:]
        return text


def _case(
    slug: str,
    filename: str,
    top: str,
    purpose: str,
    *,
    small: dict[str, int] | None = None,
    medium: dict[str, int] | None = None,
    large: dict[str, int] | None = None,
    stress: dict[str, int] | None = None,
    egraph: bool = False,
) -> StructuralWitness:
    profiles = (
        ("small", tuple((small or {}).items())),
        ("medium", tuple((medium or small or {}).items())),
        ("large", tuple((large or medium or small or {}).items())),
        ("stress", tuple((stress or large or medium or small or {}).items())),
    )
    return StructuralWitness(
        slug,
        STRUCTURAL_ROOT / filename,
        top,
        purpose,
        profiles,
        egraph,
    )


WITNESSES = (
    _case(
        "barrel_shifter", "barrel_shifter.zhl", "BarrelShifterWitness",
        "runtime variable shift and mux-tree inference",
        small={"W": 16}, medium={"W": 128}, large={"W": 256}, stress={"W": 512},
    ),
    _case(
        "dynamic_permute", "dynamic_permute.zhl", "DynamicPermuteWitness",
        "independent runtime vector indexes",
        small={"N": 8, "IW": 3}, medium={"N": 16, "IW": 4},
        large={"N": 32, "IW": 5}, stress={"N": 64, "IW": 6},
    ),
    _case(
        "priority_encoder", "priority_encoder.zhl", "PriorityEncoderWitness",
        "ordered priority reduction and nested mux formation",
        small={"N": 16, "IW": 4}, medium={"N": 32, "IW": 5},
        large={"N": 64, "IW": 6}, stress={"N": 128, "IW": 7}, egraph=True,
    ),
    _case(
        "packet_compactor", "packet_compactor.zhl", "PacketCompactorWitness",
        "stable prefix-count compaction and dynamic placement",
        small={"N": 8, "IW": 3, "CW": 4},
        medium={"N": 16, "IW": 4, "CW": 5},
        large={"N": 32, "IW": 5, "CW": 6},
        stress={"N": 64, "IW": 6, "CW": 7},
    ),
    _case(
        "scatter_gather", "scatter_gather.zhl", "ScatterGatherWitness",
        "runtime gather and explicit OR-collision scatter",
        small={"N": 8, "IW": 3}, medium={"N": 16, "IW": 4},
        large={"N": 32, "IW": 5}, stress={"N": 64, "IW": 6},
    ),
    _case(
        "wide_arbiter", "wide_arbiter.zhl", "WideArbiterWitness",
        "wide fixed-priority grant",
        small={"N": 16, "IW": 4}, medium={"N": 32, "IW": 5},
        large={"N": 64, "IW": 6}, stress={"N": 128, "IW": 7},
    ),
    _case(
        "variable_slice", "variable_slice.zhl", "VariableSliceWitness",
        "runtime fixed-width selection from a wide aggregate",
        small={"N": 16, "IW": 4}, medium={"N": 32, "IW": 5},
        large={"N": 64, "IW": 6}, stress={"N": 128, "IW": 7},
    ),
    _case(
        "crossbar", "crossbar.zhl", "CrossbarWitness",
        "runtime N by N word selection",
        small={"N": 8, "IW": 3}, medium={"N": 16, "IW": 4},
        large={"N": 32, "IW": 5}, stress={"N": 64, "IW": 6},
    ),
    _case(
        "cam", "cam.zhl", "CAMWitness",
        "parallel equality plus ordered match selection",
        small={"N": 8, "IW": 3}, medium={"N": 16, "IW": 4},
        large={"N": 32, "IW": 5}, stress={"N": 64, "IW": 6},
    ),
    _case(
        "reduction_tree", "reduction_tree.zhl", "ReductionTreeWitness",
        "flat and nested associative reductions",
        small={"N": 32}, medium={"N": 128}, large={"N": 512}, stress={"N": 1024},
    ),
    _case(
        "generic_explosion", "generic_explosion.zhl", "GenericExplosionWitness",
        "nested generated generic specialization",
        small={"N": 8}, medium={"N": 16}, large={"N": 32}, stress={"N": 64},
        egraph=True,
    ),
    _case(
        "fir", "fir.zhl", "FIRWitness",
        "regular constant-coefficient arithmetic replication",
        small={"N": 16}, medium={"N": 32}, large={"N": 64}, stress={"N": 128},
    ),
    _case(
        "crc_parallel", "crc_parallel.zhl", "CRCParallelWitness",
        "overlapping generated XOR cones",
        small={"N": 32}, medium={"N": 64}, large={"N": 128}, stress={"N": 128},
        egraph=True,
    ),
    _case(
        "matrix_transpose", "matrix_transpose.zhl", "MatrixTransposeWitness",
        "compile-time-only multidimensional rewiring",
        small={"N": 8}, medium={"N": 16}, large={"N": 16}, stress={"N": 16},
    ),
    _case(
        "byte_lane_aligner", "byte_lane_aligner.zhl", "ByteLaneAlignerWitness",
        "correlated runtime byte selections",
        small={"N": 8}, medium={"N": 16}, large={"N": 32}, stress={"N": 32},
    ),
    _case(
        "one_hot_mux", "one_hot_mux.zhl", "OneHotMuxWitness",
        "arbitrary-select OR mux and explicit exclusivity observation",
        small={"N": 16}, medium={"N": 32}, large={"N": 64}, stress={"N": 128},
    ),
    _case(
        "prefix_network", "prefix_network.zhl", "PrefixNetworkWitness",
        "overlapping prefix OR and count reductions",
        small={"N": 16}, medium={"N": 32}, large={"N": 64}, stress={"N": 128},
        egraph=True,
    ),
    _case(
        "multidimensional_index", "multidimensional_index.zhl",
        "MultiDimensionalIndexWitness",
        "mixed static/runtime nested-vector indexing",
    ),
)


WITNESS_BY_SLUG = {witness.slug: witness for witness in WITNESSES}
