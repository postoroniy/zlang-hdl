"""Backend-shared physical HDL identifier policy."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable


SYSTEMVERILOG_RESERVED = frozenset({
    "always", "always_comb", "always_ff", "assign", "automatic", "begin",
    "bit", "case", "default", "else", "end", "endcase", "endmodule",
    "event",
    "for", "function", "generate", "if", "input", "integer", "interface",
    "join", "join_any", "join_none", "local", "logic", "matches", "module",
    "output", "packed", "parameter", "reg", "sequence", "struct", "table",
    "typedef", "unpacked", "wire",
})


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
