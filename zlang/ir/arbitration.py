"""Typed packet arbitration policy and ownership state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zlang.ir.module import Port


class ArbitrationPolicy(str, Enum):
    FIXED_PRIORITY = "fixed_priority"
    ROUND_ROBIN = "round_robin"


class GrantScope(str, Enum):
    BEAT = "beat"
    PACKET = "packet"


@dataclass(frozen=True)
class PacketArbiter:
    sources: tuple[Port, ...]
    destination: Port
    policy: ArbitrationPolicy
    grant_scope: GrantScope
