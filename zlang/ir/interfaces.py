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


class ConnectionAdapter(str, Enum):
    READY_VALID_TO_CREDIT = "rv_to_credit"
    CREDIT_TO_READY_VALID = "credit_to_rv"
