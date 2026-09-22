"""protocol equivalence protocol observational-equivalence and finite trace safety models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.common import canonical_identity
from zlang.ir.interfaces import ConnectionAdapter, InterfaceProtocol
from zlang.ir.type_codec import TypeCodecError, canonical_type_data


PROTOCOL_ENDPOINT_IDENTITY_SCHEMA = "zlang-protocol-endpoint-v2"
PROTOCOL_RELATION_IDENTITY_SCHEMA = "zlang-protocol-relation-v2"


def _payload_type_data(value: object) -> object:
    try:
        return canonical_type_data(value)  # type: ignore[arg-type]
    except (TypeCodecError, AttributeError, TypeError):
        if isinstance(value, (str, int, bool)) or value is None:
            return {"external": value}
        raise TypeError(
            "protocol payload type requires canonical hardware type data"
        ) from None


class LatencyModel(str, Enum):
    EXACT = "exact"
    BOUNDED = "bounded"
    VARIABLE = "variable"


@dataclass(frozen=True)
class ProtocolObservationContract:
    payload_sequence: bool = True
    ordering: str = "in_order"
    loss: str = "no_loss"
    duplication: str = "no_duplication"
    latency: LatencyModel = LatencyModel.VARIABLE
    throughput: int = 1
    backpressure: str = "timing_may_change"
    buffering: str = "opaque"
    reset: str = "epoch_empty"


@dataclass(frozen=True)
class ProtocolEndpoint:
    protocol: InterfaceProtocol
    payload_type: object
    direction: str
    clock_domain: str | None = None
    reset_domain: str | None = None
    parameter: int | None = None

    @property
    def identity(self) -> str:
        return canonical_identity(PROTOCOL_ENDPOINT_IDENTITY_SCHEMA, {
            "clock_domain": self.clock_domain,
            "direction": self.direction,
            "parameter": self.parameter,
            "payload_type": _payload_type_data(self.payload_type),
            "protocol": self.protocol.value,
            "reset_domain": self.reset_domain,
        })


@dataclass(frozen=True)
class ProtocolRelation:
    equivalent: bool
    preserved_observations: tuple[str, ...]
    changed_observations: tuple[str, ...]
    assumptions: tuple[str, ...]
    buffering: int
    latency_model: LatencyModel
    throughput_model: int
    proof: str
    reason: str
    identity: str
    payload_width: int = 1


class ProtocolLegalityError(ValueError):
    pass


def relate_protocol(source: ProtocolEndpoint, destination: ProtocolEndpoint,
                    *, adapter: ConnectionAdapter | None = None,
                    buffer_depth: int = 0,
                    contract: ProtocolObservationContract = ProtocolObservationContract()) -> ProtocolRelation:
    if source.payload_type != destination.payload_type:
        raise ProtocolLegalityError("incompatible payload type")
    if source.direction != destination.direction:
        raise ProtocolLegalityError("incompatible protocol direction")
    if source.clock_domain != destination.clock_domain:
        raise ProtocolLegalityError("clock-domain mismatch")
    if source.reset_domain != destination.reset_domain:
        raise ProtocolLegalityError("reset-domain mismatch")
    if contract.ordering != "in_order" or contract.loss != "no_loss" or contract.duplication != "no_duplication":
        raise ProtocolLegalityError("unsupported observation contract")
    expected = _expected_adapter(source.protocol, destination.protocol)
    if adapter is not expected:
        if expected is None and adapter is None:
            pass
        else:
            raise ProtocolLegalityError(f"unsupported protocol pair or adapter: expected {expected}")
    if adapter is ConnectionAdapter.CREDIT_TO_READY_VALID:
        if source.parameter is None or buffer_depth < source.parameter:
            raise ProtocolLegalityError("insufficient adapter buffer")
    changed = ("ready timing", "transaction latency") if adapter or buffer_depth else ()
    preserved = ("payload sequence", "ordering", "no loss", "no duplication")
    identity = canonical_identity(PROTOCOL_RELATION_IDENTITY_SCHEMA, {
        "adapter": adapter.value if adapter else None,
        "buffer_depth": buffer_depth,
        "contract": {
            "backpressure": contract.backpressure,
            "buffering": contract.buffering,
            "duplication": contract.duplication,
            "latency": contract.latency.value,
            "loss": contract.loss,
            "ordering": contract.ordering,
            "payload_sequence": contract.payload_sequence,
            "reset": contract.reset,
            "throughput": contract.throughput,
        },
        "destination": destination.identity,
        "source": source.identity,
    })
    width = int(getattr(source.payload_type, "width", 1))
    return ProtocolRelation(True, preserved, changed,
                            ("legal source behavior", "same clock/reset domain", "downstream may stall arbitrarily"),
                            buffer_depth, LatencyModel.VARIABLE if changed else LatencyModel.EXACT,
                            contract.throughput, "transaction trace conservation", "observationally equivalent under contract", identity,
                            width)


def _expected_adapter(source, destination):
    if source is destination:
        return None
    if source is InterfaceProtocol.READY_VALID and destination is InterfaceProtocol.CREDIT:
        return ConnectionAdapter.READY_VALID_TO_CREDIT
    if source is InterfaceProtocol.CREDIT and destination is InterfaceProtocol.READY_VALID:
        return ConnectionAdapter.CREDIT_TO_READY_VALID
    return "unsupported"


@dataclass(frozen=True)
class TraceCheck:
    input_payloads: tuple[object, ...]
    output_payloads: tuple[object, ...]
    buffered_payloads: tuple[object, ...]
    occupancy: int
    violations: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.violations


class ProtocolTraceChecker:
    """Finite-prefix conservation checker for RV-like transaction traces."""
    def __init__(self, depth: int):
        if depth < 1:
            raise ValueError("buffer depth must be positive")
        self.depth = depth
        self._accepted: list[object] = []
        self._emitted: list[object] = []
        self._buffer: list[object] = []
        self._violations: list[str] = []

    def reset(self) -> None:
        self._accepted.clear()
        self._emitted.clear()
        self._buffer.clear()

    def observe_input_transfer(self, payload: object) -> bool:
        if len(self._buffer) >= self.depth:
            return False
        self._accepted.append(payload)
        self._buffer.append(payload)
        return True

    def observe_output_transfer(self, payload: object) -> bool:
        if not self._buffer:
            self._violations.append("output transfer has no prior accepted input")
            return False
        expected = self._buffer.pop(0)
        if expected != payload:
            self._violations.append("payload ordering or duplication violation")
        self._emitted.append(payload)
        return expected == payload

    def finish(self) -> TraceCheck:
        if len(self._emitted) > len(self._accepted):
            self._violations.append("output exceeds accepted transaction count")
        return TraceCheck(tuple(self._accepted), tuple(self._emitted), tuple(self._buffer),
                          len(self._buffer), tuple(self._violations))


def drain_checker(checker: ProtocolTraceChecker) -> TraceCheck:
    while checker._buffer:
        checker.observe_output_transfer(checker._buffer[0])
    return checker.finish()


def candidate_cost(relation: ProtocolRelation):
    from zlang.costs import CandidateCost
    width = relation.payload_width
    return CandidateCost.estimate(lut=max(1, relation.buffering), ff=relation.buffering * width,
                                  latency=(0 if relation.latency_model is LatencyModel.EXACT else None),
                                  ii=relation.throughput_model,
                                  structural_cost=relation.buffering + 1)
