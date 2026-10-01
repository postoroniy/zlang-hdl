"""Deterministic executable plan for native ZLang simulation.

The simulation plan is deliberately smaller than semantic IR.  It is built
from the exact post-planning module, retains a topologically ordered expression
DAG, and contains only information required by a simulator.  Native runtimes
must validate the complete plan before allocating executable memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from typing import Any



SIMULATION_PLAN_SCHEMA = "zlang-simulation-plan-v11"
SIMULATION_RUNTIME_ABI = "zlang-native-simulation-abi-v11"
CRANELIFT_VERSION = "0.135.2"
MAX_PLAN_BYTES = 16_777_216
MAX_PLAN_NODES = 32_768
MAX_PLAN_WIDTH = 8192
MAX_PLAN_ARITHMETIC_WIDTH = 512
# Memory cells use the same limb-backed representation as other packed values.
# Keep the per-cell bound aligned with the plan-wide packed-value contract so
# aggregate FIFO payloads (for example the 2065-bit 802.11a mapper packet) do
# not fail after otherwise valid primitive lowering.
MAX_PLAN_MEMORY_WIDTH = MAX_PLAN_WIDTH
MAX_PLAN_LIMB_WORK = 32768
MAX_PLAN_MEMORY_BITS = 16_777_216
MAX_PLAN_EVENTS = 4_096
MAX_PLAN_REGION_DEPTH = 8
MAX_PLAN_REGION_ITERATIONS = 1_000_000
MAX_PLAN_DYNAMIC_NODE_WORK = 8_000_000


class SimulationPlanError(ValueError):
    """A module cannot be represented by the native simulation plan."""


class JitUnsupportedFeatureError(SimulationPlanError):
    """The current native runtime does not implement an exact module feature."""


@dataclass(frozen=True)
class SimulationPlan:
    """Strict canonical bytes plus their deterministic content identity."""

    payload: dict[str, Any]
    canonical_bytes: bytes
    identity: str

    @cached_property
    def execution_identity(self) -> str:
        """Identity of native-executable bits, not source/debug provenance.

        The full plan identity stays source-specific.  Native code is reusable
        only when its primitive program, layout, initialization, event IDs and
        target recipe match exactly.  Metadata consumed solely by the Python
        diagnostic projection does not participate in machine-code selection.
        """

        executable = {name: value for name, value in self.payload.items()
                      if name not in {"identity", "canonical_ir_identity"}}
        executable["nodes"] = [
            {name: value for name, value in node.items() if name != "origins"}
            for node in self.payload["nodes"]
        ]
        executable["events"] = [
            {"id": event["id"], "metadata": {
                name: value for name, value in event["metadata"].items()
                if name != "source_origin"
            }}
            for event in self.payload["events"]
        ]
        return hashlib.sha256(_canonical_json({
            "schema": "zlang-executable-plan-identity-v1",
            "program": executable,
        })).hexdigest()

    def to_bytes(self) -> bytes:
        return self.canonical_bytes

    def to_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "SimulationPlan":
        if len(payload) > MAX_PLAN_BYTES:
            raise SimulationPlanError(
                f"simulation plan exceeds {MAX_PLAN_BYTES} encoded bytes"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SimulationPlanError(
                "simulation plan is not canonical UTF-8 JSON"
            ) from error
        if not isinstance(value, dict):
            raise SimulationPlanError("simulation plan root must be an object")
        canonical = _canonical_json(value)
        if canonical != payload:
            raise SimulationPlanError("simulation plan bytes are not canonical")
        _validate_plan_payload(value)
        unsigned = dict(value)
        unsigned["identity"] = ""
        identity = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        if value["identity"] != identity:
            raise SimulationPlanError(
                "simulation plan identity does not match its bytes"
            )
        return cls(value, canonical, identity)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _identity_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    without_identity = dict(payload)
    without_identity["identity"] = ""
    digest = hashlib.sha256(_canonical_json(without_identity)).hexdigest()
    payload = dict(payload)
    payload["identity"] = digest
    return _canonical_json(payload), digest


def build_simulation_plan(module: object) -> SimulationPlan:
    from zlang.simulation_plan_build import build_simulation_plan as build

    return build(module)


def _memory_read_register_name(
    semantic_id: str,
    port_name: str | None,
    stage: int,
) -> str:
    """Compatibility façade for compiler-owned native memory-state names."""

    from zlang.simulation_plan_build import _memory_read_register_name as name

    return name(semantic_id, port_name, stage)


def _build_leaf_simulation_plan(module: object) -> SimulationPlan:
    """Compatibility facade for the compiler-owned leaf-plan constructor.

    The implementation lives with plan construction, while profiling and
    tested internal callers retain the historical facade entry point.
    """

    from zlang.simulation_plan_build import _build_leaf_simulation_plan as build

    return build(module)


def _validate_plan_payload(payload: dict[str, Any]) -> None:
    from zlang.simulation_plan_codec import validate_plan_payload

    validate_plan_payload(payload)


__all__ = [
    "JitUnsupportedFeatureError",
    "CRANELIFT_VERSION",
    "MAX_PLAN_BYTES",
    "MAX_PLAN_NODES",
    "MAX_PLAN_WIDTH",
    "SIMULATION_PLAN_SCHEMA",
    "SIMULATION_RUNTIME_ABI",
    "SimulationPlan",
    "SimulationPlanError",
    "build_simulation_plan",
]
