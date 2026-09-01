"""Independent standard-bus behavioral/reference models.

These models are deliberately kept outside production lowering.  Their plain
immutable records and explicit ``step`` transitions provide an oracle for the
eventual ordinary-ZLang implementations and must not become a hidden backend
or compiler primitive.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")

@dataclass(frozen=True)
class RegRequest:
    addr: int
    write: bool
    wdata: int = 0
    wstrb: int = 0xF

@dataclass(frozen=True)
class RegResponse:
    rdata: int = 0
    error: bool = False

@dataclass(frozen=True)
class AxiAw:
    addr: int

@dataclass(frozen=True)
class AxiW:
    data: int
    strb: int = 0xF

@dataclass(frozen=True)
class AxiB:
    resp: int = 0

@dataclass(frozen=True)
class AxiR:
    data: int = 0
    resp: int = 0

@dataclass(frozen=True)
class AxiLiteInput:
    aw_valid: bool = False
    aw: AxiAw = AxiAw(0)
    w_valid: bool = False
    w: AxiW = AxiW(0)
    b_ready: bool = False
    ar_valid: bool = False
    ar: AxiAw = AxiAw(0)
    r_ready: bool = False
    reset: bool = False

@dataclass(frozen=True)
class AxiLiteOutput:
    aw_ready: bool
    w_ready: bool
    b_valid: bool
    b: AxiB
    ar_ready: bool
    r_valid: bool
    r: AxiR

@dataclass(frozen=True)
class _AxiState:
    aw: AxiAw | None = None
    w: AxiW | None = None
    b: AxiB | None = None
    r: AxiR | None = None

class Axi4LiteToRegBus:
    """Single-outstanding AXI4-Lite slave frontend.

    AW and W are independent one-entry buffers.  A write is joined only after
    both transfers; complete writes have priority over AR in the transition.
    ``reg_access`` is the library boundary to a CSR/RegBus target and returns
    one response per accepted request.
    """
    def __init__(self, reg_access: Callable[[RegRequest], RegResponse]):
        self._state = _AxiState()
        self._access = reg_access

    @property
    def state(self) -> _AxiState:
        return self._state

    def outputs(self) -> AxiLiteOutput:
        s = self._state
        idle = s.b is None and s.r is None
        return AxiLiteOutput(s.aw is None and idle, s.w is None and idle,
                             s.b is not None, s.b or AxiB(),
                             idle and s.aw is None and s.w is None,
                             s.r is not None, s.r or AxiR())

    def step(self, i: AxiLiteInput) -> AxiLiteOutput:
        if i.reset:
            self._state = _AxiState()
            return self.outputs()
        before = self.outputs()
        s = self._state
        aw = s.aw if not (before.aw_ready and i.aw_valid) else i.aw
        w = s.w if not (before.w_ready and i.w_valid) else i.w
        b, r = s.b, s.r
        if b is not None and i.b_ready:
            b = None
        if r is not None and i.r_ready:
            r = None
        # only issue a new transaction when no response is held
        if b is None and r is None:
            if aw is not None and w is not None:
                response = self._access(RegRequest(aw.addr, True, w.data, w.strb))
                b = AxiB(2 if response.error else 0)
                aw, w = None, None
            elif i.ar_valid and before.ar_ready:
                response = self._access(RegRequest(i.ar.addr, False))
                r = AxiR(response.rdata, 2 if response.error else 0)
        self._state = _AxiState(aw, w, b, r)
        return self.outputs()

@dataclass(frozen=True)
class ApbInput:
    psel: bool = False
    penable: bool = False
    pwrite: bool = False
    paddr: int = 0
    pwdata: int = 0
    pready: bool = True
    reset: bool = False

@dataclass(frozen=True)
class ApbOutput:
    paddr: int
    pwdata: int
    pwrite: bool
    psel: bool
    penable: bool
    pready: bool
    prdata: int
    pslverr: bool

class ApbToRegBus:
    """APB3-like IDLE/SETUP/ACCESS bridge with stable wait controls."""
    def __init__(self, reg_access: Callable[[RegRequest], RegResponse]):
        self._access = reg_access
        self._phase = "IDLE"
        self._latched: RegRequest | None = None
        self._response = RegResponse()
        self._complete = False

    @property
    def phase(self) -> str:
        return self._phase

    def step(self, i: ApbInput) -> ApbOutput:
        if i.reset:
            self._phase, self._latched, self._response, self._complete = "IDLE", None, RegResponse(), False
            return ApbOutput(0, 0, False, False, False, False, 0, False)
        self._complete = False
        if self._phase == "IDLE" and i.psel and not i.penable:
            self._latched = RegRequest(i.paddr, i.pwrite, i.pwdata)
            self._phase = "ACCESS"
        elif self._phase == "ACCESS" and self._latched is not None and i.pready:
            self._response = self._access(self._latched)
            self._phase = "IDLE"
            self._latched = None
            self._complete = True
        req = self._latched or RegRequest(0, False)
        return ApbOutput(req.addr, req.wdata, req.write, self._phase == "ACCESS", self._phase == "ACCESS", self._complete, self._response.rdata, self._response.error)


@dataclass(frozen=True)
class WishboneInput:
    cyc: bool = False
    stb: bool = False
    we: bool = False
    adr: int = 0
    dat_w: int = 0
    sel: int = 0xF
    reset: bool = False


@dataclass(frozen=True)
class WishboneOutput:
    ack: bool
    err: bool
    stall: bool
    dat_r: int


class WishboneToRegBus:
    """Independent single-beat Wishbone B4 Classic reference model."""

    def __init__(self, reg_access: Callable[[RegRequest], RegResponse]):
        self._access = reg_access
        self._pending: RegRequest | None = None
        self._response: RegResponse | None = None

    def step(self, i: WishboneInput) -> WishboneOutput:
        if i.reset:
            self._pending = None
            self._response = None
            return WishboneOutput(False, False, False, 0)
        complete = bool(self._response is not None and i.cyc)
        response = self._response or RegResponse()
        if complete:
            self._response = None
        if self._pending is not None and self._response is None:
            self._response = self._access(self._pending)
            self._pending = None
        if i.cyc and i.stb and self._pending is None and self._response is None and not complete:
            self._pending = RegRequest(i.adr, i.we, i.dat_w, i.sel)
        return WishboneOutput(complete and not response.error,
                              response.error if complete else False,
                              self._pending is not None or self._response is not None,
                              response.rdata if complete else 0)


@dataclass(frozen=True)
class AxiStreamBeat:
    data: int
    keep: int
    strb: int
    last: bool


def axi_stream_transfer(valid: bool, ready: bool, beat: AxiStreamBeat) -> AxiStreamBeat | None:
    """Return the transferred beat; stalled cycles observe no transaction."""
    return beat if valid and ready else None

def axi_lite_safety_properties(prefix: str = "axi4lite") -> tuple[str, ...]:
    return (f"{prefix}.valid_stable_under_stall", f"{prefix}.payload_stable_under_stall", f"{prefix}.aw_w_join", f"{prefix}.one_b_per_write", f"{prefix}.one_r_per_read", f"{prefix}.reset_clears_buffers")

def apb_safety_properties(prefix: str = "apb") -> tuple[str, ...]:
    return (f"{prefix}.setup_before_access", f"{prefix}.controls_stable_wait", f"{prefix}.complete_on_pready", f"{prefix}.one_completion", f"{prefix}.reset_to_idle")


def wishbone_safety_properties(prefix: str = "wishbone") -> tuple[str, ...]:
    return (f"{prefix}.one_ack_or_err_per_request", f"{prefix}.stable_while_stalled",
            f"{prefix}.no_completion_without_cyc", f"{prefix}.reset_clears_transaction")
