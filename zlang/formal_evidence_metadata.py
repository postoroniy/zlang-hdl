"""Typed route-owned context for formal evidence publication.

The proof runner owns status, mode, depth, and solver outcome.  A formal route
may additionally know semantic facts that cannot be reconstructed from solver
logs, such as a transaction relation, its reset contract, or its timing.  This
small immutable product carries only those facts to the evidence adapter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FormalEvidenceMetadata:
    """Facts known by one formal route but not by the generic proof runner."""

    relation: str | None = None
    unbounded: bool | None = None
    reset_contract_identity: str | None = None
    reset_contract: tuple[tuple[str, str | int], ...] = ()
    latency: int | None = None
    initiation_interval: int | None = None
    capacity: int | None = None
    same_edge_retire_reload: bool | None = None
    required_proven_supported: bool | None = None

    def __post_init__(self) -> None:
        if self.relation is not None and not self.relation:
            raise ValueError("formal evidence relation must be non-empty")
        if self.reset_contract_identity is not None and not self.reset_contract_identity:
            raise ValueError("formal evidence reset identity must be non-empty")
        keys = tuple(key for key, _ in self.reset_contract)
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("formal evidence reset contract keys must be unique")
        for value, label, minimum in (
            (self.latency, "latency", 0),
            (self.initiation_interval, "initiation interval", 1),
            (self.capacity, "capacity", 1),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < minimum
            ):
                raise ValueError(f"formal evidence {label} is invalid")

    def details(self) -> dict[str, object]:
        """Return stable detail fields for the common evidence record."""

        return {
            "unbounded": self.unbounded,
            "reset_contract_identity": self.reset_contract_identity,
            "reset_contract": dict(self.reset_contract) or None,
            "latency": self.latency,
            "ii": self.initiation_interval,
            "capacity": self.capacity,
            "same_edge_retire_reload": self.same_edge_retire_reload,
            "required_proven_supported": self.required_proven_supported,
        }


__all__ = ["FormalEvidenceMetadata"]
