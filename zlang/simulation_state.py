"""Simulation-only access to selected architectural state.

The catalog is backend independent and names state by typed hierarchy and
semantic identities.  It is deliberately separate from formal observations
and from :class:`BackendArtifact`: asking for test access must not change the
production implementation or its public ABI.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import json
from typing import Iterable, Mapping

from zlang.common import stable_digest, stable_pretty_json
from zlang.ir.hierarchy import build_hierarchy_index, specialization_fingerprint
from zlang.ir.module import Module
from zlang.ir.packing import (
    PACKING_LAYOUT_SCHEMA,
    is_bit_packable,
    pack_runtime,
    unpack_runtime,
)
from zlang.ir.runtime_values import runtime_value_fits
from zlang.ir.type_codec import canonical_type_data, canonical_type_from_data
from zlang.ir.types import HardwareType, VecType
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import OptimizationStage
from zlang.opt.module_lowering import lower
from zlang.source import SourceOrigin


SIMULATION_STATE_SCHEMA = "zlang-simulation-state-catalog-v2"
SIMULATION_STATE_BINDING_SCHEMA = "zlang-simulation-state-binding-v2"


class SimulationStateError(ValueError):
    """A state catalog, access request, or persistent session is invalid."""


class SimulationStateKind(str, Enum):
    REGISTER = "register_value"
    MEMORY = "memory_contents"
    MEMORY_READ_DATA = "memory_read_data"


@dataclass(frozen=True)
class SimulationStateBinding:
    """One exact writable simulation object in one physical instance."""

    binding_id: str
    instance_identity: str
    specialization_identity: str
    physical_instance_path: tuple[str, ...]
    local_semantic_id: str
    object_name: str
    object_kind: SimulationStateKind
    canonical_type: HardwareType
    element_type: HardwareType | None
    length: int | None
    packed_width: int
    element_width: int | None
    clock_domain: str | None
    reset_domain: str | None
    source_origin: SourceOrigin | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "instance_identity": self.instance_identity,
            "specialization_identity": self.specialization_identity,
            "physical_instance_path": list(self.physical_instance_path),
            "local_semantic_id": self.local_semantic_id,
            "object_name": self.object_name,
            "object_kind": self.object_kind.value,
            "canonical_type": canonical_type_data(self.canonical_type),
            "element_type": (
                None
                if self.element_type is None
                else canonical_type_data(self.element_type)
            ),
            "length": self.length,
            "packed_width": self.packed_width,
            "element_width": self.element_width,
            "clock_domain": self.clock_domain,
            "reset_domain": self.reset_domain,
            "source_origin": (
                None if self.source_origin is None else self.source_origin.to_data()
            ),
        }

    def identity_data(self) -> dict[str, object]:
        """Return semantic/shape data without diagnostic source provenance."""

        data = self.to_data()
        data.pop("source_origin")
        return data

    @classmethod
    def from_data(cls, value: object) -> "SimulationStateBinding":
        if not isinstance(value, dict):
            raise SimulationStateError("simulation state binding must be an object")
        expected = {
            "binding_id", "instance_identity", "specialization_identity",
            "physical_instance_path", "local_semantic_id", "object_name",
            "object_kind", "canonical_type", "element_type", "length",
            "packed_width", "element_width", "clock_domain", "reset_domain",
            "source_origin",
        }
        if set(value) != expected:
            raise SimulationStateError(
                "simulation state binding fields do not match the v1 schema"
            )
        strings = (
            "binding_id", "instance_identity", "specialization_identity",
            "local_semantic_id", "object_name", "object_kind",
        )
        if any(not isinstance(value[name], str) or not value[name] for name in strings):
            raise SimulationStateError(
                "simulation state binding identities and names must be non-empty"
            )
        raw_path = value["physical_instance_path"]
        if (
            not isinstance(raw_path, list)
            or not raw_path
            or any(not isinstance(item, str) or not item for item in raw_path)
        ):
            raise SimulationStateError(
                "simulation state physical path must contain non-empty names"
            )
        length = value["length"]
        element_width = value["element_width"]
        packed_width = value["packed_width"]
        for label, item, optional in (
            ("length", length, True),
            ("element width", element_width, True),
            ("packed width", packed_width, False),
        ):
            if item is None and optional:
                continue
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise SimulationStateError(
                    f"simulation state {label} must be a positive integer"
                )
        for label in ("clock_domain", "reset_domain"):
            item = value[label]
            if item is not None and (not isinstance(item, str) or not item):
                raise SimulationStateError(
                    f"simulation state {label} must be null or a non-empty string"
                )
        try:
            type_ = canonical_type_from_data(value["canonical_type"])
            raw_element = value["element_type"]
            element_type = (
                None if raw_element is None else canonical_type_from_data(raw_element)
            )
            kind = SimulationStateKind(value["object_kind"])
            origin = (
                None
                if value["source_origin"] is None
                else SourceOrigin.from_data(value["source_origin"])
            )
        except (TypeError, ValueError) as error:
            raise SimulationStateError(str(error)) from error
        result = cls(
            value["binding_id"],
            value["instance_identity"],
            value["specialization_identity"],
            tuple(raw_path),
            value["local_semantic_id"],
            value["object_name"],
            kind,
            type_,
            element_type,
            length,
            packed_width,
            element_width,
            value["clock_domain"],
            value["reset_domain"],
            origin,
        )
        result.validate()
        return result

    def validate_shape(self) -> None:
        if self.canonical_type.width != self.packed_width:
            raise SimulationStateError(
                f"simulation state '{self.binding_id}' packed width disagrees "
                "with its canonical type"
            )
        if self.length is None:
            if self.element_type is not None or self.element_width is not None:
                raise SimulationStateError(
                    f"scalar simulation state '{self.binding_id}' has element metadata"
                )
            return
        if self.element_type is None or self.element_width is None:
            raise SimulationStateError(
                f"indexed simulation state '{self.binding_id}' lacks element metadata"
            )
        if self.element_type.width != self.element_width:
            raise SimulationStateError(
                f"simulation state '{self.binding_id}' element width is inconsistent"
            )
        if self.packed_width != self.length * self.element_width:
            raise SimulationStateError(
                f"simulation state '{self.binding_id}' shape does not match its width"
            )
        if not isinstance(self.canonical_type, VecType):
            raise SimulationStateError(
                f"indexed simulation state '{self.binding_id}' is not a vector"
            )
        if (
            self.canonical_type.length != self.length
            or self.canonical_type.element_type != self.element_type
        ):
            raise SimulationStateError(
                f"simulation state '{self.binding_id}' vector type disagrees with shape"
            )

    def validate(self) -> None:
        """Validate programmatic records as strictly as decoded JSON records."""

        for label, value in (
            ("binding ID", self.binding_id),
            ("instance identity", self.instance_identity),
            ("specialization identity", self.specialization_identity),
            ("local semantic ID", self.local_semantic_id),
            ("object name", self.object_name),
            ("clock domain", self.clock_domain),
            ("reset domain", self.reset_domain),
        ):
            if not isinstance(value, str) or not value:
                raise SimulationStateError(
                    f"simulation state {label} must be a non-empty string"
                )
        if (
            not isinstance(self.physical_instance_path, tuple)
            or not self.physical_instance_path
            or any(not isinstance(item, str) or not item
                   for item in self.physical_instance_path)
        ):
            raise SimulationStateError(
                "simulation state physical path must contain non-empty names"
            )
        if not isinstance(self.object_kind, SimulationStateKind):
            raise SimulationStateError("simulation state object kind is invalid")
        if self.source_origin is not None and not isinstance(
            self.source_origin, SourceOrigin
        ):
            raise SimulationStateError("simulation state source origin is invalid")
        for label, value, optional in (
            ("packed width", self.packed_width, False),
            ("element width", self.element_width, True),
            ("length", self.length, True),
        ):
            if value is None and optional:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SimulationStateError(
                    f"simulation state {label} must be a positive integer"
                )
        if not is_bit_packable(self.canonical_type):
            raise SimulationStateError(
                f"simulation state '{self.object_name}' type is not bit-packable"
            )
        if self.object_kind is SimulationStateKind.MEMORY and self.length is None:
            raise SimulationStateError(
                "memory-contents simulation state must be indexed"
            )
        if (
            self.object_kind is SimulationStateKind.MEMORY_READ_DATA
            and self.length is not None
        ):
            raise SimulationStateError(
                "memory read-data simulation state must be one complete value"
            )
        self.validate_shape()


@dataclass(frozen=True)
class SimulationStateCatalog:
    """Deterministic simulation-state ABI for one selected typed hierarchy."""

    module: str
    selected_ir_identity: str
    root_instance_identity: str
    bindings: tuple[SimulationStateBinding, ...]
    catalog_identity: str
    schema: str = SIMULATION_STATE_SCHEMA

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
            "module": self.module,
            "selected_ir_identity": self.selected_ir_identity,
            "root_instance_identity": self.root_instance_identity,
            "bindings": [item.identity_data() for item in self.bindings],
        }

    def validate(self) -> None:
        if self.schema != SIMULATION_STATE_SCHEMA:
            raise SimulationStateError("unsupported simulation state catalog schema")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.module,
                self.selected_ir_identity,
                self.root_instance_identity,
                self.catalog_identity,
            )
        ):
            raise SimulationStateError("simulation state catalog identities must not be empty")
        if not isinstance(self.bindings, tuple):
            raise SimulationStateError("simulation state catalog bindings must be a tuple")
        ids = [item.binding_id for item in self.bindings]
        if len(ids) != len(set(ids)):
            raise SimulationStateError("duplicate simulation state binding identity")
        for binding in self.bindings:
            if not isinstance(binding, SimulationStateBinding):
                raise SimulationStateError(
                    "simulation state catalog contains an invalid binding"
                )
            binding.validate()
            if binding.physical_instance_path[0] != self.module:
                raise SimulationStateError(
                    "simulation state binding physical path is outside the root module"
                )
        expected = stable_digest(self._identity_payload())
        if self.catalog_identity != expected:
            raise SimulationStateError("simulation state catalog identity is stale")

    def to_data(self) -> dict[str, object]:
        self.validate()
        return {
            "schema": self.schema,
            "module": self.module,
            "selected_ir_identity": self.selected_ir_identity,
            "root_instance_identity": self.root_instance_identity,
            "bindings": [item.to_data() for item in self.bindings],
            "catalog_identity": self.catalog_identity,
        }

    def to_json(self) -> str:
        return stable_pretty_json(self.to_data())

    @classmethod
    def from_data(cls, value: object) -> "SimulationStateCatalog":
        if not isinstance(value, dict):
            raise SimulationStateError("simulation state catalog must be an object")
        expected = {
            "schema", "module", "selected_ir_identity", "root_instance_identity",
            "bindings", "catalog_identity",
        }
        if set(value) != expected:
            raise SimulationStateError(
                "simulation state catalog fields do not match the v1 schema"
            )
        raw_bindings = value["bindings"]
        if not isinstance(raw_bindings, list):
            raise SimulationStateError("simulation state bindings must be an array")
        strings = (
            "schema", "module", "selected_ir_identity", "root_instance_identity",
            "catalog_identity",
        )
        if any(not isinstance(value[name], str) or not value[name] for name in strings):
            raise SimulationStateError("simulation state catalog fields must be strings")
        result = cls(
            value["module"],
            value["selected_ir_identity"],
            value["root_instance_identity"],
            tuple(SimulationStateBinding.from_data(item) for item in raw_bindings),
            value["catalog_identity"],
            value["schema"],
        )
        result.validate()
        return result

    @classmethod
    def from_json(cls, text: str) -> "SimulationStateCatalog":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise SimulationStateError(
                f"invalid simulation state catalog JSON: {error}"
            ) from error
        return cls.from_data(value)

    def binding(self, binding_id: str) -> SimulationStateBinding:
        matches = tuple(item for item in self.bindings if item.binding_id == binding_id)
        if not matches:
            raise SimulationStateError(
                f"unknown simulation state binding '{binding_id}'"
            )
        if len(matches) != 1:
            raise SimulationStateError(
                f"ambiguous simulation state binding '{binding_id}'"
            )
        return matches[0]

    def resolve(
        self,
        *,
        physical_instance_path: tuple[str, ...],
        object_kind: SimulationStateKind,
        object_name: str,
    ) -> SimulationStateBinding:
        matches = tuple(
            item
            for item in self.bindings
            if item.physical_instance_path == physical_instance_path
            and item.object_kind is object_kind
            and item.object_name == object_name
        )
        if not matches:
            raise SimulationStateError(
                "simulation state object is absent: "
                f"{'.'.join(physical_instance_path)}.{object_name} "
                f"({object_kind.value})"
            )
        if len(matches) != 1:
            raise SimulationStateError("simulation state object resolution is ambiguous")
        return matches[0]


def build_simulation_state_catalog(
    module: Module,
    *,
    selected_ir_identity: str,
) -> SimulationStateCatalog:
    """Collect ordinary registers and writable memory cells recursively."""

    selected = selected_ir_identity
    if not selected:
        raise SimulationStateError(
            "simulation state catalog requires the selected IR identity"
        )
    actual_selected = canonical_ir_identity(
        lower(module, stage=OptimizationStage.SELECTED_ARCHITECTURE)
    )
    if selected != actual_selected:
        raise SimulationStateError(
            "simulation state catalog selected IR identity is stale"
        )
    hierarchy = build_hierarchy_index(module)
    if len(module.clock_domains) != 1:
        raise SimulationStateError(
            "simulation state access currently requires exactly one root "
            "clock/reset domain"
        )
    root_domain = module.clock_domains[0]
    for entry in hierarchy.entries:
        if entry.module.clock_domains and entry.module.clock_domains != (root_domain,):
            raise SimulationStateError(
                "simulation state access does not support multiple or crossed "
                "clock/reset domains"
            )
    root_specialization = specialization_fingerprint(module)
    root_instance = "simulation-instance:" + stable_digest({
        "schema": "zlang-simulation-root-instance-v1",
        "selected_ir_identity": selected,
        "specialization_identity": root_specialization,
    })
    instance_ids: dict[tuple[str, ...], str] = {hierarchy.root_path: root_instance}
    bindings: list[SimulationStateBinding] = []
    for entry in hierarchy.entries:
        if entry.physical_path != hierarchy.root_path:
            assert entry.elaborated is not None
            parent_instance = instance_ids[entry.physical_path[:-1]]
            instance_ids[entry.physical_path] = "simulation-instance:" + stable_digest({
                "schema": "zlang-simulation-child-instance-v1",
                "parent_instance_identity": parent_instance,
                "elaborated_instance_identity": entry.elaborated.instance_identity,
            })
        instance_identity = instance_ids[entry.physical_path]
        specialization_identity = (
            root_specialization
            if entry.elaborated is None
            else entry.elaborated.specialization_identity
        )
        state_domain = (
            entry.module.clock_domains[0]
            if entry.module.clock_domains
            else root_domain
        )
        for register in entry.module.registers:
            if not is_bit_packable(register.type):
                raise SimulationStateError(
                    f"register '{register.name}' has a type unsupported by "
                    "simulation state access"
                )
            element_type = (
                register.type.element_type
                if isinstance(register.type, VecType)
                else None
            )
            length = register.type.length if isinstance(register.type, VecType) else None
            matching_resources = ()
            if entry.module.resolved_transition is not None:
                matching_resources = tuple(
                    resource
                    for resource in entry.module.resolved_transition.resources
                    if resource.name == register.name
                    and resource.kind.value == "register"
                )
                if len(matching_resources) > 1:
                    raise SimulationStateError(
                        f"register '{register.name}' has duplicate state resources"
                    )
                if not matching_resources:
                    raise SimulationStateError(
                        f"register '{register.name}' is missing its resolved state resource"
                    )
                if matching_resources and (
                    matching_resources[0].type != register.type
                    or matching_resources[0].domain != register.domain
                ):
                    raise SimulationStateError(
                        f"register '{register.name}' state-resource metadata disagrees"
                    )
            local_id = (
                matching_resources[0].semantic_id
                if matching_resources
                else f"register:{register.name}"
            )
            binding_id = "simulation-state:" + stable_digest({
                "schema": SIMULATION_STATE_BINDING_SCHEMA,
                "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
                "selected_ir_identity": selected,
                "instance_identity": instance_identity,
                "local_semantic_id": local_id,
                "canonical_type": canonical_type_data(register.type),
            })
            origin = matching_resources[0].source_origin if matching_resources else None
            bindings.append(SimulationStateBinding(
                binding_id,
                instance_identity,
                specialization_identity,
                entry.physical_path,
                local_id,
                register.name,
                SimulationStateKind.REGISTER,
                register.type,
                element_type,
                length,
                register.type.width,
                None if element_type is None else element_type.width,
                register.domain or state_domain.clock,
                state_domain.reset,
                origin,
            ))
        for memory in entry.module.memories:
            if not is_bit_packable(memory.element_type):
                raise SimulationStateError(
                    f"memory '{memory.name}' has an element type unsupported by "
                    "simulation state access"
                )
            type_ = VecType(memory.depth, memory.element_type)
            local_id = f"memory:{memory.semantic_id}:cells"
            binding_id = "simulation-state:" + stable_digest({
                "schema": SIMULATION_STATE_BINDING_SCHEMA,
                "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
                "selected_ir_identity": selected,
                "instance_identity": instance_identity,
                "local_semantic_id": local_id,
                "canonical_type": canonical_type_data(type_),
            })
            bindings.append(SimulationStateBinding(
                binding_id,
                instance_identity,
                specialization_identity,
                entry.physical_path,
                local_id,
                memory.name,
                SimulationStateKind.MEMORY,
                type_,
                memory.element_type,
                memory.depth,
                type_.width,
                memory.element_type.width,
                state_domain.clock,
                state_domain.reset,
                memory.source_origin,
            ))
            if memory.read_latency >= 1:
                read_local_id = f"memory:{memory.semantic_id}:read_data"
                read_binding_id = "simulation-state:" + stable_digest({
                    "schema": SIMULATION_STATE_BINDING_SCHEMA,
                    "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
                    "selected_ir_identity": selected,
                    "instance_identity": instance_identity,
                    "local_semantic_id": read_local_id,
                    "canonical_type": canonical_type_data(memory.element_type),
                })
                bindings.append(SimulationStateBinding(
                    read_binding_id,
                    instance_identity,
                    specialization_identity,
                    entry.physical_path,
                    read_local_id,
                    memory.name,
                    SimulationStateKind.MEMORY_READ_DATA,
                    memory.element_type,
                    None,
                    None,
                    memory.element_type.width,
                    None,
                    state_domain.clock,
                    state_domain.reset,
                    memory.source_origin,
                ))
    provisional = SimulationStateCatalog(
        module.name,
        selected,
        root_instance,
        tuple(bindings),
        "pending",
    )
    result = SimulationStateCatalog(
        provisional.module,
        provisional.selected_ir_identity,
        provisional.root_instance_identity,
        provisional.bindings,
        stable_digest(provisional._identity_payload()),
    )
    result.validate()
    return result


class SimulationStateSession:
    """Persistent native simulation with typed out-of-band state access."""

    def __init__(
        self,
        compilation: object,
        *,
        catalog: SimulationStateCatalog | None = None,
    ) -> None:
        from zlang.sim import Program, _native_runtime
        from zlang.simulation_expression_plan import _memory_read_register_name
        from zlang.simulation_plan_build import build_simulation_plan

        module = getattr(compilation, "ir", None)
        selected = getattr(compilation, "selected_ir_identity", None)
        if not isinstance(module, Module) or not isinstance(selected, str) or not selected:
            raise SimulationStateError(
                "simulation session requires a complete CompilationResult"
            )
        self.module = module
        expected = build_simulation_state_catalog(
            module, selected_ir_identity=selected
        )
        if catalog is not None:
            catalog.validate()
        self.catalog = catalog or expected
        if (
            self.catalog.catalog_identity != expected.catalog_identity
            or self.catalog.module != expected.module
            or self.catalog.selected_ir_identity != expected.selected_ir_identity
            or self.catalog.root_instance_identity != expected.root_instance_identity
        ):
            raise SimulationStateError(
                "simulation state catalog does not match the selected typed hierarchy"
            )
        try:
            plan = build_simulation_plan(module)
            self._simulator = Program(
                plan, module, _native_runtime().compile_plan_bytes(plan.to_bytes())
            ).create()
        except (ValueError, RuntimeError, ImportError) as error:
            raise SimulationStateError(
                f"typed hierarchy is not supported by the native simulator: {error}"
            ) from error
        registers = {item["name"]: item for item in plan.payload["registers"]}
        memories = {item["name"]: item for item in plan.payload["memories"]}
        semantic_memories = {
            (entry.physical_path, memory.name): memory
            for entry in build_hierarchy_index(module).entries
            for memory in entry.module.memories
        }
        self._state_targets: dict[str, tuple[str, str]] = {}
        for binding in self.catalog.bindings:
            prefix = ".".join(binding.physical_instance_path[1:])
            if binding.object_kind is SimulationStateKind.MEMORY_READ_DATA:
                memory = semantic_memories.get(
                    (binding.physical_instance_path, binding.object_name)
                )
                if memory is None or memory.read_latency < 1 or memory.ports:
                    raise SimulationStateError(
                        "native memory read-data state has no exact compiler binding"
                    )
                name = _memory_read_register_name(
                    memory.semantic_id, None, memory.read_latency - 1
                )
            else:
                name = binding.object_name
            if prefix:
                name = f"{prefix}.{name}"
            if binding.object_kind is SimulationStateKind.MEMORY:
                record = memories.get(name)
                if (record is None or record["width"] != binding.element_width
                        or record["depth"] != binding.length):
                    raise SimulationStateError(
                        "native memory state disagrees with the selected catalog"
                    )
                kind = "memory"
            else:
                record = registers.get(name)
                if record is None or record["width"] != binding.packed_width:
                    raise SimulationStateError(
                        "native register state disagrees with the selected catalog"
                    )
                kind = "register"
            self._state_targets[binding.binding_id] = (kind, name)
        self._external_reset_active = False
        self.cycle = 0

    @classmethod
    def from_compilation(cls, compilation: object) -> "SimulationStateSession":
        return cls(compilation)

    def _resolve(self, binding_id: str) -> tuple[SimulationStateBinding, str, str]:
        binding = self.catalog.binding(binding_id)
        kind, name = self._state_targets[binding_id]
        return binding, kind, name

    @staticmethod
    def _check_index(binding: SimulationStateBinding, index: int | None) -> None:
        if index is None:
            return
        if isinstance(index, bool) or not isinstance(index, int):
            raise SimulationStateError("simulation state index must be an integer")
        if binding.length is None:
            raise SimulationStateError(
                f"simulation state '{binding.binding_id}' is not indexable"
            )
        if not 0 <= index < binding.length:
            raise SimulationStateError(
                f"simulation state index {index} is outside 0..{binding.length - 1}"
            )

    def read(self, binding_id: str, *, index: int | None = None) -> object:
        from zlang.simulation_primitives import int_from_limbs

        binding, kind, name = self._resolve(binding_id)
        self._check_index(binding, index)
        if kind == "memory" and index is None:
            assert binding.length is not None and binding.element_type is not None
            value = [
                unpack_runtime(
                    binding.element_type,
                    int_from_limbs(self._simulator._native.read_state(kind, name, offset)),
                )
                for offset in range(binding.length)
            ]
        else:
            type_ = binding.element_type if kind == "memory" else binding.canonical_type
            assert type_ is not None
            value = unpack_runtime(
                type_,
                int_from_limbs(self._simulator._native.read_state(kind, name, index if kind == "memory" else None)),
            )
            if index is not None and kind == "register":
                value = value[index]
        return deepcopy(value)

    def write(
        self,
        binding_id: str,
        value: object,
        *,
        index: int | None = None,
    ) -> None:
        binding, _, _ = self._resolve(binding_id)
        self._check_index(binding, index)
        expected_type = binding.canonical_type if index is None else binding.element_type
        assert expected_type is not None
        if not runtime_value_fits(value, expected_type):
            raise SimulationStateError(
                f"simulation state value does not fit exact type '{expected_type}'"
            )
        self._simulator._native.write_state_batch(
            self._prepare_edits(binding, deepcopy(value), index=index)
        )

    def _prepare_edits(
        self,
        binding: SimulationStateBinding,
        value: object,
        *,
        index: int | None,
    ) -> list[tuple[str, str, int | None, list[int]]]:
        from zlang.simulation_primitives import int_to_limbs

        kind, name = self._state_targets[binding.binding_id]
        if kind == "memory":
            assert binding.element_type is not None
            if index is not None:
                return [(kind, name, index, int_to_limbs(
                    pack_runtime(binding.element_type, value), binding.element_type.width
                ))]
            return [
                (kind, name, offset, int_to_limbs(
                    pack_runtime(binding.element_type, element), binding.element_type.width
                ))
                for offset, element in enumerate(value)
            ]
        if index is not None:
            current = self.read(binding.binding_id)
            current[index] = value
            value = current
        return [(kind, name, None, int_to_limbs(
            pack_runtime(binding.canonical_type, value), binding.packed_width
        ))]

    def preload(self, values: Mapping[str, object]) -> None:
        prepared: list[tuple[str, str, int | None, list[int]]] = []
        for binding_id, value in values.items():
            binding, _, _ = self._resolve(binding_id)
            if not runtime_value_fits(value, binding.canonical_type):
                raise SimulationStateError(
                    "simulation state value does not fit exact type "
                    f"'{binding.canonical_type}'"
                )
            prepared.extend(self._prepare_edits(binding, deepcopy(value), index=None))
        self._simulator._native.write_state_batch(prepared)

    def snapshot(self, binding_ids: Iterable[str] | None = None) -> dict[str, object]:
        selected = (
            tuple(binding_ids)
            if binding_ids is not None
            else tuple(item.binding_id for item in self.catalog.bindings)
        )
        return {binding_id: self.read(binding_id) for binding_id in selected}

    def step(
        self,
        inputs: Mapping[str, object],
        *,
        reset: bool = False,
    ) -> dict[str, object]:
        try:
            expected = {port.name for port in self.module.inputs}
            if set(inputs) != expected:
                raise SimulationStateError(
                    "simulation state step requires exactly the module inputs"
                )
            domain = self.module.clock_domains[0]
            for name, value in inputs.items():
                self._simulator.set(name, value)
            if domain.reset and bool(reset) != self._external_reset_active:
                self._simulator.reset(domain.reset, asserted=bool(reset))
            result = self._simulator.eval()
            self._simulator.edge(domain.clock)
        except Exception as error:
            raise SimulationStateError(str(error)) from error
        self._external_reset_active = bool(reset)
        self.cycle += 1
        return result

    def run(
        self,
        input_cycles: Iterable[Mapping[str, object]],
        *,
        reset: Iterable[bool] | None = None,
    ) -> list[dict[str, object]]:
        cycles = tuple(input_cycles)
        resets = tuple(reset) if reset is not None else (False,) * len(cycles)
        if len(resets) != len(cycles):
            raise SimulationStateError(
                "reset sequence length must match input cycles"
            )
        return [
            self.step(inputs, reset=reset_active)
            for inputs, reset_active in zip(cycles, resets, strict=True)
        ]


__all__ = [
    "SIMULATION_STATE_SCHEMA",
    "SimulationStateBinding",
    "SimulationStateCatalog",
    "SimulationStateError",
    "SimulationStateKind",
    "SimulationStateSession",
    "build_simulation_state_catalog",
]
