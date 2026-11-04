"""Backend-shared physical HDL identifier policy."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable


# Identifier keywords recognized by the pinned Verilator 6388139f7 lexer
# (Verilog 1995 through SystemVerilog 2017; excludes AMS and $system tasks).
# Keep this policy shared by emission, manifests and formal locators.
SYSTEMVERILOG_RESERVED = frozenset("""
accept_on alias always always_comb always_ff always_latch and assert assign assume
automatic before begin bind bins binsof bit break buf bufif0 bufif1 byte case casex
casez cell chandle checker class clocking cmos config const constraint context continue
cover covergroup coverpoint cross deassign default defparam design disable dist do edge
else end endcase endchecker endclass endclocking endconfig endfunction endgenerate
endgroup endinterface endmodule endpackage endprimitive endprogram endproperty
endsequence endspecify endtable endtask enum event eventually expect export extends
extern final first_match for force foreach forever fork forkjoin function generate
genvar global highz0 highz1 if iff ifnone ignore_bins illegal_bins implements implies
import incdir include initial inout input inside instance int integer interconnect
interface intersect join join_any join_none large let liblist library local localparam
logic longint macromodule matches medium modport module nand negedge nettype new
nexttime nmos nor noshowcancelled not notif0 notif1 null or output package packed
parameter pmos posedge primitive priority program property protected pull0 pull1
pulldown pullup pulsestyle_ondetect pulsestyle_onevent pure rand randc randcase
randomize randsequence rcmos real realtime ref reg reject_on release repeat restrict
return rnmos rpmos rtran rtranif0 rtranif1 s_always s_eventually s_nexttime s_until
s_until_with scalared sequence shortint shortreal showcancelled signed small soft solve
specify specparam static string strong strong0 strong1 struct super supply0 supply1
sync_accept_on sync_reject_on table tagged task this throughout time timeprecision
timeunit tran tranif0 tranif1 tri tri0 tri1 triand trior trireg type typedef union
unique unique0 unsigned until until_with untyped use uwire var vectored virtual void
wait wait_order wand weak weak0 weak1 while wildcard wire with within wor xnor xor
""".split())


def rtl_identifier(name: str) -> str:
    """Return the deterministic physical HDL spelling for one leaf name."""

    return f"zlang_{name}" if name in SYSTEMVERILOG_RESERVED else name


def rtl_instance_identifier(name: str) -> str:
    """Return the deterministic RTL token for one physical instance name.

    Compile-time instance-array elements retain source paths such as
    ``lane[0]`` in typed hierarchy IR.  HDL identifiers cannot contain those
    brackets.  This function supplies only the readable *preferred* spelling;
    final emission/locator consumers use the containing module's shared naming
    plan to resolve collisions with source names such as ``lane_0``.
    """

    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[([0-9]+)\]", name)
    if match is None:
        return rtl_identifier(name)
    return rtl_identifier(f"{match.group(1)}_{match.group(2)}")


def rtl_register_state_identifier(name: str) -> str:
    """Return the exact direct-SV token which stores one user register."""

    return rtl_identifier(name)


def rtl_memory_cells_identifier(name: str) -> str:
    """Return the exact direct-SV unpacked-array token for writable cells."""

    return f"{rtl_identifier(name)}_cells"


def rtl_memory_read_data_identifier(name: str) -> str:
    """Return the exact direct-SV token for a one-cycle memory read latch."""

    return f"{rtl_identifier(name)}_read_data"


@dataclass(frozen=True)
class RtlIdentifierCollision:
    """Two logical public leaves which share one physical HDL spelling."""

    physical_name: str
    first_external_name: str
    first_semantic_id: str
    second_external_name: str
    second_semantic_id: str


def first_rtl_leaf_identifier_collision(
    leaves: Iterable[object],
) -> RtlIdentifierCollision | None:
    """Return the first deterministic collision after whole-name mangling.

    Public ABI producers validate logical external names independently.  HDL
    reserved-word escaping is a second namespace projection, so backends must
    also reject e.g. logical leaves ``module`` and ``zlang_module``.  Mangling
    is intentionally applied to each *complete flattened name*, never to path
    segments independently.
    """

    physical_names: dict[str, object] = {}
    for leaf in leaves:
        external_name = str(leaf.external_name)
        physical = rtl_identifier(external_name)
        previous = physical_names.get(physical)
        if previous is not None:
            return RtlIdentifierCollision(
                physical,
                str(previous.external_name),
                str(previous.leaf_semantic_id),
                external_name,
                str(leaf.leaf_semantic_id),
            )
        physical_names[physical] = leaf
    return None


def allocate_private_rtl_identifier(
    preferred: str,
    *,
    semantic_identity: str,
    used: set[str],
) -> str:
    """Allocate a deterministic backend-private name beside public leaves.

    Preserve established generated text when the preferred spelling is free.
    A stable semantic digest is added only for a real collision; an ordinal is
    then used defensively if distinct private objects share that identity.
    """

    candidate = rtl_identifier(preferred)
    if candidate not in used:
        used.add(candidate)
        return candidate
    digest = hashlib.sha256(semantic_identity.encode()).hexdigest()[:8]
    candidate = f"{rtl_identifier(preferred)}_{digest}"
    ordinal = 2
    while candidate in used:
        candidate = f"{rtl_identifier(preferred)}_{digest}_{ordinal}"
        ordinal += 1
    used.add(candidate)
    return candidate


__all__ = [
    "RtlIdentifierCollision",
    "SYSTEMVERILOG_RESERVED",
    "allocate_private_rtl_identifier",
    "first_rtl_leaf_identifier_collision",
    "rtl_identifier",
    "rtl_instance_identifier",
    "rtl_memory_cells_identifier",
    "rtl_memory_read_data_identifier",
    "rtl_register_state_identifier",
]
