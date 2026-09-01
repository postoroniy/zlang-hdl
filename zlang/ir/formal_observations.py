"""Canonical semantic observation identities used by the formal stack.

These helpers allocate backend-independent IDs only.  RTL tokens remain
backend-published metadata and must never be recovered from these strings.
"""

from __future__ import annotations

from enum import Enum


class FormalObservationIdError(ValueError):
    """A semantic observation identity cannot be formed unambiguously."""


class RequestResponseObservationSignal(str, Enum):
    OUTSTANDING = "outstanding"
    REQUEST_ACCEPT = "request_accept"
    RESPONSE_CONSUME = "response_consume"
    REQUEST_OCCUPANCY = "request_occupancy"
    RESPONSE_OCCUPANCY = "response_occupancy"


def _required(value: str, description: str) -> str:
    if not value:
        raise FormalObservationIdError(f"{description} must not be empty")
    return value


def port_observation_id(port_name: str, signal: str | None = None) -> str:
    """Return the canonical ID for a scalar port or one protocol field."""

    base = f"port:{_required(port_name, 'port name')}"
    return base if signal is None else f"{base}.{_required(signal, 'port signal')}"


def register_observation_id(register_name: str) -> str:
    """Return the canonical ID for committed register state."""

    return f"register:{_required(register_name, 'register name')}"


def rule_fire_observation_id(rule_name: str) -> str:
    """Return the canonical ID for one scheduler-accepted rule firing.

    This is the accepted action-group decision produced by the existing
    ``ResolvedTransition`` scheduler, not the rule's raw guard or an inferred
    RTL-name convention.
    """

    return f"rule:{_required(rule_name, 'rule name')}.fire"


def fifo_observation_id(fifo_name: str, signal: str) -> str:
    """Return the canonical ID for one typed FIFO observation."""

    return (
        f"fifo:{_required(fifo_name, 'FIFO name')}."
        f"{_required(signal, 'FIFO signal')}"
    )


def request_response_observation_id(
    connection_semantic_id: str,
    signal: RequestResponseObservationSignal | str,
) -> str:
    """Append one observation suffix to an already-qualified RR identity."""

    connection = _required(
        connection_semantic_id, "request/response connection semantic ID"
    )
    if not connection.startswith("rr:"):
        raise FormalObservationIdError(
            "request/response connection semantic ID must be rr:-qualified"
        )
    try:
        normalized = RequestResponseObservationSignal(signal).value
    except ValueError as error:
        raise FormalObservationIdError(
            f"unsupported request/response observation signal: {signal}"
        ) from error
    return f"{connection}:{normalized}"


def recursive_observation_id(
    instance_identity: str, local_semantic_id: str
) -> str:
    """Qualify one local observation with its semantic physical instance."""

    return (
        f"{_required(instance_identity, 'instance identity')}:"
        f"{_required(local_semantic_id, 'local semantic observation ID')}"
    )


__all__ = [
    "FormalObservationIdError",
    "RequestResponseObservationSignal",
    "fifo_observation_id",
    "port_observation_id",
    "recursive_observation_id",
    "register_observation_id",
    "request_response_observation_id",
    "rule_fire_observation_id",
]
