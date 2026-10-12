# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable native-simulation plan model and default bounded policy."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from typing import Any

from zlang.simulation_plan_policy import (
    DEFAULT_SIMULATION_PLAN_POLICY,
    SimulationPlanError,
    SimulationPlanPolicy,
)


@dataclass(frozen=True)
class SimulationPlan:
    """Strict canonical bytes plus their deterministic content identity."""

    payload: dict[str, Any]
    canonical_bytes: bytes
    identity: str

    @cached_property
    def execution_identity(self) -> str:
        executable = {
            name: value
            for name, value in self.payload.items()
            if name not in {"identity", "canonical_ir_identity"}
        }
        executable["nodes"] = [
            {name: value for name, value in node.items() if name != "origins"}
            for node in self.payload["nodes"]
        ]
        executable["events"] = [
            {
                "id": event["id"],
                "metadata": {
                    name: value
                    for name, value in event["metadata"].items()
                    if name != "source_origin"
                },
            }
            for event in self.payload["events"]
        ]
        return hashlib.sha256(
            canonical_json({
                "schema": "zlang-executable-plan-identity-v1",
                "program": executable,
            })
        ).hexdigest()

    def to_bytes(self) -> bytes:
        return self.canonical_bytes

    def to_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        policy: SimulationPlanPolicy = DEFAULT_SIMULATION_PLAN_POLICY,
    ) -> "SimulationPlan":
        if len(payload) > policy.max_bytes:
            raise SimulationPlanError(
                f"simulation plan exceeds {policy.max_bytes} encoded bytes"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SimulationPlanError(
                "simulation plan is not canonical UTF-8 JSON"
            ) from error
        if not isinstance(value, dict):
            raise SimulationPlanError("simulation plan root must be an object")
        canonical = canonical_json(value)
        if canonical != payload:
            raise SimulationPlanError("simulation plan bytes are not canonical")
        from zlang.simulation_plan_codec import validate_plan_payload

        validate_plan_payload(value, policy=policy)
        unsigned = dict(value)
        unsigned["identity"] = ""
        identity = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        if value["identity"] != identity:
            raise SimulationPlanError(
                "simulation plan identity does not match its bytes"
            )
        return cls(value, canonical, identity)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def identity_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    without_identity = dict(payload)
    without_identity["identity"] = ""
    digest = hashlib.sha256(canonical_json(without_identity)).hexdigest()
    payload = dict(payload)
    payload["identity"] = digest
    return canonical_json(payload), digest
