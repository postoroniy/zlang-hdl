"""Deterministic SymbiYosys configuration rendering."""

from __future__ import annotations

from zlang.ir.formal import (
    FormalDesign,
    FormalError,
    ProofMode,
    cover_harness_top,
)
from zlang.formal_routes import formal_engine_route


def emit_sby(
    design: FormalDesign,
    *,
    depth: int = 20,
    top: str | None = None,
    mode: ProofMode = ProofMode.BMC,
    solver: str = "z3",
    route: str = "smtbmc",
    source_file: str | None = None,
) -> str:
    """Emit SBY only for an implementation-bound executable harness."""

    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        raise FormalError(
            "executable SBY output requires a connected backend formal artifact"
        )
    unavailable = next(
        (
            item
            for item in design.properties
            if item.non_executable_reason is not None or item.predicate is None
        ),
        None,
    )
    if unavailable is not None:
        why = unavailable.non_executable_reason or "structured predicate unavailable"
        raise FormalError(
            "combined executable SBY view requires every safety property and "
            f"assumption on one backend; '{unavailable.id}' is unavailable: "
            f"{why}. Use --verification-bundle for per-goal backend routing"
        )
    _validate_solver(solver)
    engine_route = formal_engine_route(route)
    engine_route.validate_request(solver=solver, mode=mode.value)
    top = top or f"{design.module_name}__safety_verification_formal"
    source_file = source_file or f"{top}.sv"
    _validate_source_file(source_file)
    return "\n".join(
        (
            "[options]",
            f"mode {mode.value}",
            f"depth {depth}",
            *engine_route.sby_options,
            "",
            "[engines]",
            engine_route.engine_line(solver=solver),
            "",
            "[script]",
            f"read_verilog -sv -formal {source_file}",
            f"prep -top {top}",
            "",
            "[files]",
            source_file,
            "",
        )
    )


def emit_cover_sby(
    design: FormalDesign,
    *,
    cover_id: str,
    depth: int = 20,
    top: str | None = None,
    solver: str = "z3",
    route: str = "smtbmc",
    source_file: str | None = None,
) -> str:
    """Emit a deterministic per-goal SBY cover configuration."""

    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        raise FormalError(
            "executable cover SBY output requires a connected backend formal artifact"
        )
    matches = tuple(item for item in design.covers if item.id == cover_id)
    if len(matches) != 1:
        message = "unknown" if not matches else "duplicate"
        raise FormalError(f"{message} cover property id: {cover_id}")
    if matches[0].non_executable_reason is not None:
        raise FormalError(
            f"cover property '{cover_id}' is not executable: "
            f"{matches[0].non_executable_reason}"
        )
    unavailable = next(
        (
            item
            for item in design.properties
            if item.kind.value == "assumption"
            and (item.non_executable_reason is not None or item.predicate is None)
        ),
        None,
    )
    if unavailable is not None:
        why = unavailable.non_executable_reason or "structured predicate unavailable"
        raise FormalError(
            f"cover property '{cover_id}' requires executable assumption "
            f"'{unavailable.id}': {why}"
        )
    _validate_solver(solver)
    engine_route = formal_engine_route(route)
    engine_route.validate_request(solver=solver, mode="cover")
    top = top or cover_harness_top(design, cover_id)
    source_file = source_file or f"{top}.sv"
    _validate_source_file(source_file)
    return "\n".join(
        (
            "[options]",
            "mode cover",
            f"depth {depth}",
            *engine_route.sby_options,
            "",
            "[engines]",
            engine_route.engine_line(solver=solver),
            "",
            "[script]",
            f"read_verilog -sv -formal {source_file}",
            f"prep -top {top}",
            "",
            "[files]",
            source_file,
            "",
        )
    )


def _validate_solver(solver: str) -> None:
    if not solver or any(character.isspace() for character in solver):
        raise FormalError("formal solver name must be one non-empty token")


def _validate_source_file(source_file: str) -> None:
    if not source_file or any(character in source_file for character in "\n\r"):
        raise FormalError("formal harness filename must be one non-empty line")
