"""Typed, deterministic publication of bounded platform clock constraints.

This module intentionally lives outside semantic and implementation-request IR.
The source clock/reset contract describes hardware behavior; a selected project
profile supplies the physical period used by an implementation tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Mapping

from zlang.backend.manifest import BackendArtifact
from zlang.backend.manifest_codec import validate_artifact_links
from zlang.backend.publication import SafePublicationError, publish_relative_files
from zlang.common import stable_digest
from zlang.ir.equivalence import BindingSide, SignalRole
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.module import Module
from zlang.project import ProjectManifest


class PlatformConstraintError(ValueError):
    """A physical constraint request or backend binding is not exact."""


class ConstraintFormat(str, Enum):
    XDC = "xdc"
    SDC = "sdc"


_SAFE_TCL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_SUPPORTED_BACKENDS = frozenset({"direct_systemverilog"})


@dataclass(frozen=True)
class PlatformClockConstraint:
    semantic_clock: str
    period_ns: Decimal
    profile: str

    def __post_init__(self) -> None:
        if not self.semantic_clock:
            raise PlatformConstraintError("platform clock name must not be empty")
        if not self.profile:
            raise PlatformConstraintError("platform profile name must not be empty")
        try:
            period = Decimal(str(self.period_ns))
        except (InvalidOperation, ValueError) as error:
            raise PlatformConstraintError("clock period-ns must be finite and positive") from error
        if not period.is_finite() or period <= 0:
            raise PlatformConstraintError("clock period-ns must be finite and positive")
        object.__setattr__(self, "period_ns", period)

    @property
    def identity(self) -> str:
        return stable_digest({
            "schema": "zlang-platform-clock-constraint-v1",
            "semantic_clock": self.semantic_clock,
            "period_ns": _decimal_text(self.period_ns),
            "profile": self.profile,
        })


@dataclass(frozen=True)
class PlatformConstraintProfile:
    name: str
    clocks: tuple[PlatformClockConstraint, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise PlatformConstraintError("platform profile name must not be empty")
        names = tuple(item.semantic_clock for item in self.clocks)
        if len(names) != len(set(names)):
            raise PlatformConstraintError("platform clock declarations must be unique")

    @property
    def identity(self) -> str:
        return stable_digest({
            "schema": "zlang-platform-constraint-profile-v1",
            "name": self.name,
            "clocks": [item.identity for item in self.clocks],
        })


@dataclass(frozen=True)
class ConstraintArtifact:
    format: ConstraintFormat
    backend: str
    module: str
    semantic_clock: str
    rtl_clock: str
    period_ns: Decimal
    backend_artifact_hash: str
    selected_ir_identity: str
    clock_edge: str
    reset: str
    rtl_reset: str
    reset_mode: str
    reset_polarity: str
    reset_release_mode: str
    reset_release_cycles: int
    power_up: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", ConstraintFormat(self.format))
        object.__setattr__(self, "period_ns", Decimal(str(self.period_ns)))
        for name in (
            "backend", "module", "semantic_clock", "rtl_clock",
            "backend_artifact_hash", "selected_ir_identity", "clock_edge",
            "reset", "rtl_reset", "reset_mode", "reset_polarity", "power_up",
            "reset_release_mode",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise PlatformConstraintError(f"constraint artifact {name} is missing")
        if self.backend not in _SUPPORTED_BACKENDS:
            raise PlatformConstraintError(
                f"constraint artifact backend '{self.backend}' is unsupported"
            )
        for enum_type, value, label in (
            (ClockEdge, self.clock_edge, "clock edge"),
            (ResetMode, self.reset_mode, "reset mode"),
            (ResetPolarity, self.reset_polarity, "reset polarity"),
            (ResetReleaseMode, self.reset_release_mode, "reset release mode"),
            (PowerUpPolicy, self.power_up, "power-up policy"),
        ):
            try:
                enum_type(value)
            except ValueError as error:
                raise PlatformConstraintError(
                    f"constraint artifact {label} '{value}' is unsupported"
                ) from error
        if (
            isinstance(self.reset_release_cycles, bool)
            or not isinstance(self.reset_release_cycles, int)
        ):
            raise PlatformConstraintError(
                "constraint artifact reset release cycles must be an integer"
            )
        try:
            ClockDomain(
                self.semantic_clock,
                self.reset,
                edge=ClockEdge(self.clock_edge),
                reset_mode=ResetMode(self.reset_mode),
                reset_polarity=ResetPolarity(self.reset_polarity),
                power_up=PowerUpPolicy(self.power_up),
                reset_release_mode=ResetReleaseMode(self.reset_release_mode),
                reset_release_cycles=self.reset_release_cycles,
            )
        except ValueError as error:
            raise PlatformConstraintError(
                f"constraint artifact physical reset contract is invalid: {error}"
            ) from error
        if not self.period_ns.is_finite() or self.period_ns <= 0:
            raise PlatformConstraintError("constraint artifact period must be finite and positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.backend_artifact_hash):
            raise PlatformConstraintError("constraint artifact backend hash is invalid")
        if not isinstance(self.text, str):
            raise PlatformConstraintError("constraint artifact text must be text")
        try:
            self.text.encode("ascii")
        except UnicodeEncodeError as error:
            raise PlatformConstraintError("constraint artifact text must be ASCII") from error

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("ascii")).hexdigest()

    @property
    def identity(self) -> str:
        return stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-platform-constraint-artifact-v2",
            "format": self.format.value,
            "backend": self.backend,
            "module": self.module,
            "semantic_clock": self.semantic_clock,
            "rtl_clock": self.rtl_clock,
            "period_ns": _decimal_text(self.period_ns),
            "backend_artifact_hash": self.backend_artifact_hash,
            "selected_ir_identity": self.selected_ir_identity,
            "clock_edge": self.clock_edge,
            "reset": self.reset,
            "rtl_reset": self.rtl_reset,
            "reset_mode": self.reset_mode,
            "reset_polarity": self.reset_polarity,
            "reset_release_mode": self.reset_release_mode,
            "reset_release_cycles": self.reset_release_cycles,
            "power_up": self.power_up,
            "content_hash": self.content_hash,
        }

    def to_json(self) -> str:
        return json.dumps(
            {**self.identity_data(), "identity": self.identity, "text": self.text},
            indent=2,
            sort_keys=True,
        ) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "ConstraintArtifact":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as error:
            raise PlatformConstraintError("invalid constraint artifact JSON") from error
        required = {
            "schema", "format", "backend", "module", "semantic_clock",
            "rtl_clock", "period_ns", "backend_artifact_hash",
            "selected_ir_identity", "clock_edge", "reset", "rtl_reset",
            "reset_mode", "reset_polarity", "reset_release_mode",
            "reset_release_cycles", "power_up", "content_hash",
            "identity", "text",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise PlatformConstraintError("constraint artifact JSON fields are invalid")
        if data["schema"] != "zlang-platform-constraint-artifact-v2":
            raise PlatformConstraintError("unsupported constraint artifact schema")
        string_fields = required - {"period_ns", "reset_release_cycles"}
        if any(
            not isinstance(data[name], str) or not data[name]
            for name in string_fields
        ) or not isinstance(data["period_ns"], str) or (
            isinstance(data["reset_release_cycles"], bool)
            or not isinstance(data["reset_release_cycles"], int)
        ):
            raise PlatformConstraintError("constraint artifact JSON values are invalid")
        try:
            artifact = cls(
                ConstraintFormat(data["format"]), data["backend"],
                data["module"], data["semantic_clock"], data["rtl_clock"],
                Decimal(data["period_ns"]), data["backend_artifact_hash"],
                data["selected_ir_identity"], data["clock_edge"], data["reset"],
                data["rtl_reset"], data["reset_mode"], data["reset_polarity"],
                data["reset_release_mode"], data["reset_release_cycles"],
                data["power_up"], data["text"],
            )
        except PlatformConstraintError:
            raise
        except (ValueError, InvalidOperation) as error:
            raise PlatformConstraintError("constraint artifact JSON values are invalid") from error
        if data["content_hash"] != artifact.content_hash:
            raise PlatformConstraintError("constraint artifact content hash does not match")
        if data["identity"] != artifact.identity:
            raise PlatformConstraintError("constraint artifact identity does not match")
        return artifact


def parse_platform_profile(
    manifest: ProjectManifest,
    profile_name: str,
) -> PlatformConstraintProfile | None:
    """Parse only the platform portion of one selected strict profile."""

    if profile_name not in manifest.profiles:
        raise PlatformConstraintError(f"unknown implementation profile '{profile_name}'")
    raw_profile = _mapping(manifest.profiles[profile_name], f"profile '{profile_name}'")
    if "platform" not in raw_profile:
        return None
    platform = _mapping(raw_profile["platform"], "profile platform")
    _exact_keys(platform, {"clocks"}, "profile platform")
    clocks = _mapping(platform["clocks"], "profile platform clocks")
    if len(clocks) != 1:
        raise PlatformConstraintError(
            "the bounded platform slice requires exactly one clock declaration"
        )
    parsed: list[PlatformClockConstraint] = []
    for name in sorted(clocks):
        if not _SAFE_TCL_IDENTIFIER.fullmatch(name):
            raise PlatformConstraintError(f"invalid platform clock name '{name}'")
        record = _mapping(clocks[name], f"platform clock '{name}'")
        _exact_keys(record, {"period-ns"}, f"platform clock '{name}'")
        value = record["period-ns"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlatformConstraintError(
                f"platform clock '{name}' period-ns must be numeric"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise PlatformConstraintError(
                f"platform clock '{name}' period-ns must be finite and positive"
            )
        parsed.append(PlatformClockConstraint(name, Decimal(str(value)), profile_name))
    return PlatformConstraintProfile(profile_name, tuple(parsed))


def build_constraint_artifact(
    module: Module,
    backend_artifact: BackendArtifact,
    constraint: PlatformClockConstraint,
    format: ConstraintFormat,
) -> ConstraintArtifact:
    """Bind one typed domain to one validated backend top-level clock port."""

    if len(module.clock_domains) != 1:
        raise PlatformConstraintError(
            "platform constraint publication requires exactly one clock/reset domain"
        )
    domain = module.clock_domains[0]
    if constraint.semantic_clock != domain.clock:
        raise PlatformConstraintError(
            f"profile clock '{constraint.semantic_clock}' does not match typed clock "
            f"'{domain.clock}'"
        )
    if backend_artifact.module != module.name:
        raise PlatformConstraintError("backend artifact module does not match typed module")
    if backend_artifact.selected_ir_identity == "" or backend_artifact.artifact_hash == "":
        raise PlatformConstraintError("backend artifact identity is incomplete")
    physical_domains = tuple(getattr(backend_artifact, "physical_domains", ()))
    matching_domains = tuple(
        item for item in physical_domains
        if item.clock == domain.clock and item.reset == domain.reset
    )
    if len(matching_domains) != 1 and (
        physical_domains or not domain.is_legacy_default
    ):
        raise PlatformConstraintError(
            "backend artifact requires exactly one physical-domain manifest for "
            f"'{domain.clock}/{domain.reset}'"
        )
    published_domain = matching_domains[0] if matching_domains else None
    expected_contract = {
        "clock": domain.clock,
        "reset": domain.reset,
        "clock_edge": domain.edge.value,
        "reset_mode": domain.reset_mode.value,
        "reset_polarity": domain.reset_polarity.value,
        "reset_release_mode": domain.reset_release_mode.value,
        "reset_release_cycles": domain.reset_release_cycles,
        "power_up": domain.power_up.value,
    }
    published_contract = None if published_domain is None else {
        "clock": published_domain.clock,
        "reset": published_domain.reset,
        "clock_edge": published_domain.clock_edge,
        "reset_mode": published_domain.reset_mode,
        "reset_polarity": published_domain.reset_polarity,
        "reset_release_mode": published_domain.reset_release_mode,
        "reset_release_cycles": published_domain.reset_release_cycles,
        "power_up": published_domain.power_up,
    }
    if published_contract is not None and published_contract != expected_contract:
        raise PlatformConstraintError(
            "backend physical-domain manifest does not match typed reset contract"
        )
    try:
        validate_artifact_links(backend_artifact)
    except ValueError as error:
        raise PlatformConstraintError(
            f"backend artifact link validation failed: {error}"
        ) from error
    clock_binding = _one_binding(backend_artifact, SignalRole.CLOCK, domain.clock)
    reset_binding = _one_binding(backend_artifact, SignalRole.RESET, domain.reset)
    if published_domain is not None and (
        clock_binding.rtl_path != published_domain.rtl_clock_path
        or reset_binding.rtl_path != published_domain.rtl_reset_path
    ):
        raise PlatformConstraintError(
            "backend physical-domain paths do not match the selected clock/reset "
            "bindings"
        )
    for label, binding in (("clock", clock_binding), ("reset", reset_binding)):
        if binding.width != 1 or binding.signedness not in {"bit", "bits", "unsigned"}:
            raise PlatformConstraintError(f"backend {label} binding must be one bit")
        if not binding.physical_available:
            raise PlatformConstraintError(f"backend {label} binding is not physically available")
        if binding.artifact_hash != backend_artifact.artifact_hash:
            raise PlatformConstraintError(f"backend {label} binding artifact hash is stale")
        if binding.selected_ir_identity != backend_artifact.selected_ir_identity:
            raise PlatformConstraintError(f"backend {label} binding selected-IR identity is stale")
        if binding.rtl_module != backend_artifact.module:
            raise PlatformConstraintError(f"backend {label} binding is not on the top module")
        if not _SAFE_TCL_IDENTIFIER.fullmatch(binding.rtl_path):
            raise PlatformConstraintError(
                f"backend {label} port '{binding.rtl_path}' cannot be safely emitted"
            )
    period = _decimal_text(constraint.period_ns)
    text = (
        f"create_clock -name {constraint.semantic_clock} -period {period} "
        f"[get_ports {{{clock_binding.rtl_path}}}]\n"
    )
    return ConstraintArtifact(
        ConstraintFormat(format), backend_artifact.backend, module.name,
        domain.clock, clock_binding.rtl_path, constraint.period_ns,
        backend_artifact.artifact_hash, backend_artifact.selected_ir_identity,
        domain.edge.value, domain.reset, reset_binding.rtl_path,
        domain.reset_mode.value, domain.reset_polarity.value,
        domain.reset_release_mode.value, domain.reset_release_cycles,
        domain.power_up.value, text,
    )


def publish_constraint_artifact(artifact: ConstraintArtifact, path: Path) -> Path:
    """Atomically publish one constraint file without following symlinks."""

    path = Path(path)
    expected_suffix = "." + artifact.format.value
    if path.suffix.lower() != expected_suffix:
        raise PlatformConstraintError(
            f"{artifact.format.value.upper()} output must use '{expected_suffix}' suffix"
        )
    try:
        return publish_relative_files(
            path.parent,
            ((Path(path.name), artifact.text.encode("ascii")),),
            existing="replace",
        )[0]
    except SafePublicationError as error:
        raise PlatformConstraintError(f"constraint publication failed: {error}") from error


def _one_binding(artifact: BackendArtifact, role: SignalRole, domain_name: str):
    matches = tuple(
        item for item in artifact.bindings
        if item.side is BindingSide.IMPLEMENTATION
        and item.role is role
        and (
            item.clock_domain == domain_name
            if role is SignalRole.CLOCK
            else item.reset_domain == domain_name
        )
    )
    if len(matches) != 1:
        raise PlatformConstraintError(
            f"backend artifact requires exactly one typed {role.value} binding "
            f"for '{domain_name}'"
        )
    return matches[0]


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PlatformConstraintError(f"{label} must be a table")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise PlatformConstraintError(f"{label} is missing '{sorted(missing)[0]}'")
    if unknown:
        raise PlatformConstraintError(f"{label} has unknown key '{sorted(unknown)[0]}'")


__all__ = [
    "ConstraintArtifact",
    "ConstraintFormat",
    "PlatformClockConstraint",
    "PlatformConstraintError",
    "PlatformConstraintProfile",
    "build_constraint_artifact",
    "parse_platform_profile",
    "publish_constraint_artifact",
]
