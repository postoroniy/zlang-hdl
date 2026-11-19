"""Protocol concepts preserved by the backend-independent typed IR."""

from __future__ import annotations

from enum import Enum


class InterfaceProtocol(str, Enum):
    WIRE = "wire"
    READY_VALID = "ready_valid"
    CREDIT = "credit"
    PACKET = "packet"
    VC_CREDIT = "vc_credit"


class ReadyValidSignal(str, Enum):
    PAYLOAD = "payload"
    VALID = "valid"
    READY = "ready"
    TRANSFER = "transfer"


def ready_valid_field_name(
    endpoint: str, signal: ReadyValidSignal | str,
) -> str:
    """Compiler-owned scalar identity for a typed ready/valid signal."""

    value = signal.value if isinstance(signal, ReadyValidSignal) else signal
    return f"$zlang_protocol:{endpoint}:{value}"


def parse_ready_valid_field_name(
    name: str,
) -> tuple[str, ReadyValidSignal] | None:
    """Decode one exact private signal identity; reject malformed names."""

    if not name.startswith("$zlang_protocol:"):
        return None
    endpoint, separator, field = name.removeprefix(
        "$zlang_protocol:"
    ).rpartition(":")
    if not separator or not endpoint or ":" in endpoint:
        return None
    try:
        signal = ReadyValidSignal(field)
    except ValueError:
        return None
    if signal is ReadyValidSignal.TRANSFER:
        return None
    if ready_valid_field_name(endpoint, signal) != name:
        return None
    return endpoint, signal


class CreditSignal(str, Enum):
    PAYLOAD = "payload"
    SEND = "send"
    RETURN = "return"
    TRANSFER = "transfer"
    CREDITS = "credits"


class PacketSignal(str, Enum):
    PAYLOAD = "payload"
    VALID = "valid"
    READY = "ready"
    LAST = "last"
    TRANSFER = "transfer"


class VirtualChannelCreditSignal(str, Enum):
    PAYLOAD = "payload"
    VC = "vc"
    SEND = "send"
    RETURN = "return"
    RETURN_VC = "return_vc"
    TRANSFER = "transfer"
    CREDITS = "credits"


InterfaceSignal = (
    ReadyValidSignal
    | CreditSignal
    | PacketSignal
    | VirtualChannelCreditSignal
)


class RequestResponseOrdering(str, Enum):
    IN_ORDER = "in_order"
    OUT_OF_ORDER = "out_of_order"


class RequestResponseRole(str, Enum):
    """Ownership of the two ready/valid halves of an interface.

    The original declaration syntax describes an initiator.  Composition also
    needs a responder, so the role is inferred from which existing channel
    fields the module owns; no new source syntax is required.
    """

    REQUESTER = "requester"
    RESPONDER = "responder"


class RequestResponseChannel(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"


def request_response_field_name(
    interface: str,
    channel: RequestResponseChannel | str,
    signal: ReadyValidSignal | str,
) -> str:
    """Return one private scalar request/response field name."""

    channel_name = channel.value if isinstance(channel, RequestResponseChannel) else channel
    signal_name = signal.value if isinstance(signal, ReadyValidSignal) else signal
    return f"$zlang_request_response:{interface}:{channel_name}:{signal_name}"


def parse_request_response_field_name(
    name: str,
) -> tuple[str, RequestResponseChannel, ReadyValidSignal] | None:
    prefix = "$zlang_request_response:"
    if not name.startswith(prefix):
        return None
    parts = name[len(prefix):].split(":")
    if len(parts) != 3 or not parts[0]:
        return None
    interface, channel_name, signal_name = parts
    try:
        channel = RequestResponseChannel(channel_name)
        signal = ReadyValidSignal(signal_name)
    except ValueError:
        return None
    if signal is ReadyValidSignal.TRANSFER:
        return None
    return interface, channel, signal


def request_response_signal_is_input(
    role: RequestResponseRole,
    channel: RequestResponseChannel,
    signal: ReadyValidSignal,
) -> bool:
    """Physical direction of an explicit request/response leaf."""
    outbound = (
        RequestResponseChannel.REQUEST if role is RequestResponseRole.REQUESTER
        else RequestResponseChannel.RESPONSE
    )
    return (signal is ReadyValidSignal.READY) == (channel is outbound)


class ConnectionAdapter(str, Enum):
    READY_VALID_TO_CREDIT = "rv_to_credit"
    CREDIT_TO_READY_VALID = "credit_to_rv"
