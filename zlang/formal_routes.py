"""Immutable identities for compiler-supported formal execution routes."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir.formal import FormalError


@dataclass(frozen=True)
class FormalEngineRoute:
    """One exact orchestration/lowering route beneath a formal engine."""

    identity: str
    sby_engine: str
    required_tools: tuple[str, ...]
    fixed_solver: str | None = None
    supported_modes: tuple[str, ...] = ("bmc", "prove", "cover")
    sby_options: tuple[str, ...] = ()
    qualification_only: bool = False
    counterexample_clock_frames: bool = False

    def __post_init__(self) -> None:
        if not self.identity or any(character.isspace() for character in self.identity):
            raise FormalError("formal route identity must be one non-empty token")
        if not self.sby_engine:
            raise FormalError("formal route requires an SBY engine")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise FormalError("formal route required tools must be unique")
        if not self.supported_modes or any(
            mode not in {"bmc", "prove", "cover"} for mode in self.supported_modes
        ):
            raise FormalError("formal route has invalid supported modes")
        if len(set(self.supported_modes)) != len(self.supported_modes):
            raise FormalError("formal route supported modes must be unique")
        if self.fixed_solver is not None and (
            not self.fixed_solver
            or any(character.isspace() for character in self.fixed_solver)
        ):
            raise FormalError("formal route fixed solver must be one non-empty token")
        if any(not option or "\n" in option for option in self.sby_options):
            raise FormalError("formal route SBY options must be non-empty lines")

    def engine_line(self, *, solver: str) -> str:
        if not solver or any(character.isspace() for character in solver):
            raise FormalError("formal solver name must be one non-empty token")
        if self.fixed_solver is not None and solver != self.fixed_solver:
            raise FormalError(
                f"formal route '{self.identity}' requires solver '{self.fixed_solver}'"
            )
        return f"{self.sby_engine} {self.fixed_solver or solver}"

    def validate_request(self, *, solver: str, mode: str) -> None:
        self.engine_line(solver=solver)
        if mode not in self.supported_modes:
            raise FormalError(
                f"formal route '{self.identity}' does not support mode '{mode}'"
            )

    def tool_names(self, *, solver: str) -> tuple[str, ...]:
        self.engine_line(solver=solver)
        selected = () if self.fixed_solver is not None else (solver,)
        return tuple(dict.fromkeys((*self.required_tools, *selected)))


SMTBMC_ROUTE = FormalEngineRoute(
    identity="smtbmc",
    sby_engine="smtbmc",
    required_tools=("yosys", "sby", "yosys-smtbmc"),
)

# These routes are deliberately qualification-only.  They provide independent
# engine evidence without participating in required-formal selection until the
# complete positive, counterexample, trace and applicability gates are accepted.
ABC_PDR_ROUTE = FormalEngineRoute(
    identity="abc-pdr",
    sby_engine="abc",
    fixed_solver="pdr",
    supported_modes=("prove",),
    required_tools=(
        "yosys", "sby", "yosys-abc", "yosys-witness", "yosys-smtbmc", "yices",
    ),
    sby_options=("aigsmt yices",),
    qualification_only=True,
)
AIGER_AVY_ROUTE = FormalEngineRoute(
    identity="aiger-avy",
    sby_engine="aiger",
    fixed_solver="avy",
    supported_modes=("prove",),
    required_tools=(
        "yosys", "sby", "avy", "yosys-witness", "yosys-smtbmc", "yices",
    ),
    sby_options=("aigsmt yices",),
    qualification_only=True,
)
BTOR_PONO_ROUTE = FormalEngineRoute(
    identity="btor-pono",
    sby_engine="btor",
    fixed_solver="pono",
    supported_modes=("bmc",),
    required_tools=("yosys", "sby", "pono", "btorsim", "yosys-witness"),
    qualification_only=True,
    counterexample_clock_frames=True,
)
BTOR_BTORMC_ROUTE = FormalEngineRoute(
    identity="btor-btormc",
    sby_engine="btor",
    fixed_solver="btormc",
    supported_modes=("bmc",),
    required_tools=("yosys", "sby", "btormc", "btorsim", "yosys-witness"),
    qualification_only=True,
    counterexample_clock_frames=True,
)

_ROUTES = {
    route.identity: route
    for route in (
        SMTBMC_ROUTE,
        ABC_PDR_ROUTE,
        AIGER_AVY_ROUTE,
        BTOR_PONO_ROUTE,
        BTOR_BTORMC_ROUTE,
    )
}


def formal_engine_route(identity: str) -> FormalEngineRoute:
    """Resolve one supported route without guessing or compatibility aliases."""

    try:
        return _ROUTES[identity]
    except KeyError as error:
        raise FormalError(f"unsupported formal execution route '{identity}'") from error


def public_formal_route_identities() -> tuple[str, ...]:
    return tuple(
        route.identity for route in _ROUTES.values() if not route.qualification_only
    )


def qualification_formal_route_identities() -> tuple[str, ...]:
    return tuple(
        route.identity for route in _ROUTES.values() if route.qualification_only
    )


__all__ = [
    "ABC_PDR_ROUTE",
    "AIGER_AVY_ROUTE",
    "BTOR_BTORMC_ROUTE",
    "BTOR_PONO_ROUTE",
    "FormalEngineRoute",
    "SMTBMC_ROUTE",
    "formal_engine_route",
    "public_formal_route_identities",
    "qualification_formal_route_identities",
]
