"""One deterministic JSON projection for typed safety verification/semantic-reference equivalence counterexamples."""

from __future__ import annotations

from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import Counterexample


def counterexample_to_data(
    value: Counterexample | EquivalenceCounterexample | None,
) -> dict[str, object] | None:
    """Encode the closed counterexample union without inferring its family."""

    if value is None:
        return None
    if isinstance(value, Counterexample):
        return {
            "kind": "safety_verification",
            "property_id": value.property_id,
            "cycle": value.cycle,
            "values": [list(item) for item in value.values],
            "raw_trace": value.raw_trace,
        }
    if isinstance(value, EquivalenceCounterexample):
        return {
            "kind": "semantic_equivalence",
            "property_id": value.property_id,
            "failure_cycle": value.failure_cycle,
            "sample_cycle": value.sample_cycle,
            "values": [list(item) for item in value.values],
            "raw_trace": value.raw_trace,
        }
    raise TypeError("counterexample must be typed safety verification or semantic-reference equivalence data")


__all__ = ["counterexample_to_data"]
