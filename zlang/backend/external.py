"""Backend-neutral, hash-validated physical mappings for external modules."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping

from zlang.ir.external import ExternalModuleContract
from zlang.ir.module import Module
from zlang.project import ProjectLock, ProjectManifest


class ExternalMappingError(ValueError):
    """A physical external mapping does not match its typed semantic contract."""


@dataclass(frozen=True)
class ExternalPhysicalMapping:
    backend: str
    logical_extern_identity: str
    physical_module_name: str
    port_map: tuple[tuple[str, str], ...]
    source: bytes
    source_sha256: str

    @classmethod
    def from_text(
        cls,
        *,
        backend: str,
        logical_extern_identity: str,
        physical_module_name: str,
        port_map: tuple[tuple[str, str], ...],
        source_text: str,
    ) -> "ExternalPhysicalMapping":
        payload = source_text.encode("utf-8")
        return cls(
            backend,
            logical_extern_identity,
            physical_module_name,
            port_map,
            payload,
            hashlib.sha256(payload).hexdigest(),
        )

    @property
    def source_text(self) -> str:
        try:
            return self.source.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExternalMappingError(
                "external HDL source must be UTF-8 text"
            ) from error

    def __post_init__(self) -> None:
        if self.backend not in {"direct_systemverilog", "clash"}:
            raise ExternalMappingError(f"unsupported external backend '{self.backend}'")
        if not self.logical_extern_identity:
            raise ExternalMappingError("external logical identity must not be empty")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", self.physical_module_name) is None:
            raise ExternalMappingError("external physical module name is not an HDL identifier")
        if hashlib.sha256(self.source).hexdigest() != self.source_sha256:
            raise ExternalMappingError("external source SHA-256 does not match source bytes")
        semantic_names = tuple(name for name, _ in self.port_map)
        physical_names = tuple(name for _, name in self.port_map)
        if len(semantic_names) != len(set(semantic_names)):
            raise ExternalMappingError("external semantic port map contains duplicates")
        if len(physical_names) != len(set(physical_names)):
            raise ExternalMappingError("external physical port map contains duplicates")
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) is None
            for name in physical_names
        ):
            raise ExternalMappingError("external physical port name is not an HDL identifier")

    def validate(self, contract: ExternalModuleContract, *, backend: str) -> None:
        if self.backend != backend:
            raise ExternalMappingError(
                f"external mapping backend is '{self.backend}', expected '{backend}'"
            )
        if self.logical_extern_identity != contract.semantic_identity:
            raise ExternalMappingError(
                f"external mapping identity does not match '{contract.logical_name}'"
            )
        expected = tuple(port.name for port in contract.signature.ports)
        actual = tuple(name for name, _ in self.port_map)
        if actual != expected:
            raise ExternalMappingError(
                f"external mapping ports for '{contract.logical_name}' must be "
                f"{expected}, got {actual}"
            )
        self.source_text


def load_profile_external_mappings(
    manifest: ProjectManifest,
    lock: ProjectLock,
    profile: str | None,
    module: Module,
) -> tuple[ExternalPhysicalMapping, ...]:
    """Resolve one selected profile's pinned, local direct-SV mappings.

    The function is deliberately read-only and offline.  It binds logical
    module names from ``zlang.toml`` to semantic contract identities only after
    semantic analysis has produced the exact typed contracts.
    """

    contracts: dict[str, ExternalModuleContract] = {}

    def visit(current: Module) -> None:
        if current.external_contract is not None:
            contract = current.external_contract
            previous = contracts.get(contract.logical_name)
            if previous is not None and previous.semantic_identity != contract.semantic_identity:
                raise ExternalMappingError(
                    f"external logical module '{contract.logical_name}' has multiple typed identities"
                )
            contracts[contract.logical_name] = contract
        for child in current.children:
            visit(child)

    visit(module)
    if profile is None:
        if not contracts:
            return ()
        raise ExternalMappingError(
            "external modules require a selected project profile with external-mappings"
        )
    raw_profile = manifest.profiles.get(profile)
    if not isinstance(raw_profile, Mapping):
        raise ExternalMappingError(f"unknown implementation profile '{profile}'")
    raw_names = raw_profile.get("external-mappings", ())
    if not isinstance(raw_names, tuple) or any(not isinstance(item, str) for item in raw_names):
        raise ExternalMappingError(
            f"profile '{profile}' external-mappings must be an array of strings"
        )
    if len(raw_names) != len(set(raw_names)):
        raise ExternalMappingError(
            f"profile '{profile}' external-mappings contains duplicates"
        )
    specs = {item.name: item for item in manifest.external_mappings}
    locked = {item.name: item for item in lock.external_mappings}
    result: list[ExternalPhysicalMapping] = []
    selected_logical: set[str] = set()
    root = manifest.project_root.resolve(strict=True)
    for name in raw_names:
        spec = specs.get(name)
        pinned = locked.get(name)
        if spec is None:
            raise ExternalMappingError(
                f"profile '{profile}' selects unknown external mapping '{name}'"
            )
        if pinned is None:
            raise ExternalMappingError(f"external mapping '{name}' is not pinned in zlang.lock")
        if (
            pinned.logical_module != spec.logical_module
            or pinned.backend != spec.backend
            or pinned.physical_module != spec.physical_module
            or pinned.ports != spec.ports
            or tuple(item.relative_path for item in pinned.sources) != spec.sources
        ):
            raise ExternalMappingError(
                f"external mapping '{name}' differs from its zlang.lock record"
            )
        contract = contracts.get(spec.logical_module)
        if contract is None:
            raise ExternalMappingError(
                f"external mapping '{name}' targets unused logical module "
                f"'{spec.logical_module}'"
            )
        if spec.logical_module in selected_logical:
            raise ExternalMappingError(
                f"multiple physical mappings select external module '{spec.logical_module}'"
            )
        selected_logical.add(spec.logical_module)
        payload_parts: list[bytes] = []
        for source in pinned.sources:
            lexical = manifest.project_root / source.relative_path
            if lexical.is_symlink():
                raise ExternalMappingError(
                    f"external source '{source.relative_path}' must not be a symlink"
                )
            try:
                path = lexical.resolve(strict=True)
                path.relative_to(root)
            except FileNotFoundError as error:
                raise ExternalMappingError(
                    f"external source '{source.relative_path}' is unavailable"
                ) from error
            except ValueError:
                raise ExternalMappingError(
                    f"external source '{source.relative_path}' escapes project root"
                ) from None
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != source.digest:
                raise ExternalMappingError(
                    f"external source '{source.relative_path}' is dirty: expected "
                    f"sha256:{source.digest}, found sha256:{digest}"
                )
            payload_parts.append(payload if payload.endswith(b"\n") else payload + b"\n")
        payload = b"".join(payload_parts)
        port_by_name = dict(spec.ports)
        port_map = tuple(
            (port.name, port_by_name[port.name])
            for port in contract.signature.ports
            if port.name in port_by_name
        )
        if set(port_by_name) != {port.name for port in contract.signature.ports}:
            expected = tuple(port.name for port in contract.signature.ports)
            raise ExternalMappingError(
                f"external mapping ports for '{contract.logical_name}' must be {expected}"
            )
        result.append(ExternalPhysicalMapping(
            "direct_systemverilog",
            contract.semantic_identity,
            spec.physical_module,
            port_map,
            payload,
            hashlib.sha256(payload).hexdigest(),
        ))
    missing = set(contracts) - selected_logical
    if missing:
        raise ExternalMappingError(
            f"profile '{profile}' has no physical mapping for external module "
            f"'{sorted(missing)[0]}'"
        )
    return tuple(sorted(result, key=lambda item: item.logical_extern_identity))


__all__ = [
    "ExternalMappingError", "ExternalPhysicalMapping", "load_profile_external_mappings",
]
