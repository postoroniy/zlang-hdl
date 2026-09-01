"""Backend-independent public module timing contracts and derived timing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from zlang.source import SourceOrigin


class TimingKnowledge(str, Enum):
    """How precisely a scalar value's cycle latency is known."""

    TIMELESS = "timeless"
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ValueTiming:
    """Derived timing for one scalar value.

    Timeless values have no cycle of their own and may be combined with a
    known path. Known values carry one exact, non-negative latency. Unknown
    values deliberately carry a reason instead of an approximate latency.
    """

    knowledge: TimingKnowledge
    latency: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.knowledge, TimingKnowledge):
            raise ValueError("value timing knowledge must be a TimingKnowledge")
        if self.knowledge is TimingKnowledge.KNOWN:
            if (
                isinstance(self.latency, bool)
                or not isinstance(self.latency, int)
                or self.latency < 0
            ):
                raise ValueError("known value timing requires non-negative latency")
            if self.reason is not None:
                raise ValueError("known value timing must not carry an unknown reason")
            return
        if self.latency is not None:
            raise ValueError("timeless/unknown value timing must not carry latency")
        if self.knowledge is TimingKnowledge.TIMELESS and self.reason is not None:
            raise ValueError("timeless value timing must not carry an unknown reason")
        if self.knowledge is TimingKnowledge.UNKNOWN and not self.reason:
            raise ValueError("unknown value timing requires a reason")

    @classmethod
    def timeless(cls) -> "ValueTiming":
        return cls(TimingKnowledge.TIMELESS)

    @classmethod
    def known(cls, latency: int) -> "ValueTiming":
        return cls(TimingKnowledge.KNOWN, latency=latency)

    @classmethod
    def unknown(cls, reason: str) -> "ValueTiming":
        return cls(TimingKnowledge.UNKNOWN, reason=reason)


@dataclass(frozen=True)
class ModuleTimingContract:
    """One exact public timing contract shared by scalar wire outputs."""

    latency: int
    initiation_interval: int
    clock_domain: str | None = None
    reset_domain: str | None = None
    source_origin: SourceOrigin | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.latency, bool)
            or not isinstance(self.latency, int)
            or self.latency < 0
        ):
            raise ValueError("module timing latency must be a non-negative integer")
        if (
            isinstance(self.initiation_interval, bool)
            or not isinstance(self.initiation_interval, int)
            or self.initiation_interval < 1
        ):
            raise ValueError("module timing II must be a positive integer")


@dataclass(frozen=True)
class OutputTiming:
    """Derived timing for one public scalar output port."""

    port: str
    timing: ValueTiming

    def __post_init__(self) -> None:
        if not self.port:
            raise ValueError("output timing requires a port name")
        if not isinstance(self.timing, ValueTiming):
            raise ValueError("output timing requires typed value timing")


@dataclass(frozen=True)
class InstanceOutputTiming:
    """Derived timing at one physical child-instance output boundary."""

    instance: str
    port: str
    timing: ValueTiming

    def __post_init__(self) -> None:
        if not self.instance or not self.port:
            raise ValueError("instance output timing requires instance and port names")
        if not isinstance(self.timing, ValueTiming):
            raise ValueError("instance output timing requires typed value timing")
