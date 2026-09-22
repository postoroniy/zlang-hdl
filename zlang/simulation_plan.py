"""Deterministic executable plan for native ZLang simulation.

The simulation plan is deliberately smaller than semantic IR.  It is built
from the exact post-planning module, retains a topologically ordered expression
DAG, and contains only information required by a simulator.  Native runtimes
must validate the complete plan before allocating executable memory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from functools import cached_property
import hashlib
import json
import platform
from pathlib import PurePosixPath
from typing import Any

from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.packing import PACKING_LAYOUT_SCHEMA
from zlang.ir import packing as ir_packing
from zlang.ir.state import (
    StateActionKind,
    StateResourceKind,
    conditional_activation_predicates,
    ordered_groups,
    selection_regions,
)
from zlang.ir.storage import FifoSignal, MemoryPortKind, MemorySignal, RomSignal
from zlang.ir.verification import VerificationGoalKind
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    TaggedUnionType,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import ExpressionOp, OptimizationStage, TargetKind
from zlang.opt.lowering import lower
from zlang.simulation_lowering import (
    PRIMITIVE_OPS,
    PrimitiveLoweringError,
    lower_to_primitive_plan,
)


SIMULATION_PLAN_SCHEMA = "zlang-simulation-plan-v9"
SIMULATION_RUNTIME_ABI = "zlang-native-simulation-abi-v9"
CRANELIFT_VERSION = "0.135.2"
MAX_PLAN_BYTES = 16_777_216
MAX_PLAN_NODES = 32_768
MAX_PLAN_WIDTH = 8192
MAX_PLAN_ARITHMETIC_WIDTH = 512
MAX_PLAN_MEMORY_WIDTH = 512
MAX_PLAN_LIMB_WORK = 32768
MAX_PLAN_MEMORY_BITS = 16_777_216
MAX_PLAN_EVENTS = 4_096


class SimulationPlanError(ValueError):
    """A module cannot be represented by the native simulation plan."""


class JitUnsupportedFeatureError(SimulationPlanError):
    """The current native runtime does not implement an exact module feature."""


@dataclass(frozen=True)
class SimulationPlan:
    """Strict canonical bytes plus their deterministic content identity."""

    payload: dict[str, Any]
    canonical_bytes: bytes
    identity: str

    @cached_property
    def execution_identity(self) -> str:
        """Identity of native-executable bits, not source/debug provenance.

        The full plan identity stays source-specific.  Native code is reusable
        only when its primitive program, layout, initialization, event IDs and
        target recipe match exactly.  Metadata consumed solely by the Python
        diagnostic projection does not participate in machine-code selection.
        """

        executable = {name: value for name, value in self.payload.items()
                      if name not in {"identity", "canonical_ir_identity"}}
        executable["nodes"] = [
            {name: value for name, value in node.items() if name != "origins"}
            for node in self.payload["nodes"]
        ]
        executable["events"] = [
            {"id": event["id"], "metadata": {
                name: value for name, value in event["metadata"].items()
                if name != "source_origin"
            }}
            for event in self.payload["events"]
        ]
        return hashlib.sha256(_canonical_json({
            "schema": "zlang-executable-plan-identity-v1",
            "program": executable,
        })).hexdigest()

    def to_bytes(self) -> bytes:
        return self.canonical_bytes

    def to_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "SimulationPlan":
        if len(payload) > MAX_PLAN_BYTES:
            raise SimulationPlanError(
                f"simulation plan exceeds {MAX_PLAN_BYTES} encoded bytes"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SimulationPlanError(
                "simulation plan is not canonical UTF-8 JSON"
            ) from error
        if not isinstance(value, dict):
            raise SimulationPlanError("simulation plan root must be an object")
        canonical = _canonical_json(value)
        if canonical != payload:
            raise SimulationPlanError("simulation plan bytes are not canonical")
        _validate_plan_payload(value)
        unsigned = dict(value)
        unsigned["identity"] = ""
        identity = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        if value["identity"] != identity:
            raise SimulationPlanError(
                "simulation plan identity does not match its bytes"
            )
        return cls(value, canonical, identity)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _identity_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    without_identity = dict(payload)
    without_identity["identity"] = ""
    digest = hashlib.sha256(_canonical_json(without_identity)).hexdigest()
    payload = dict(payload)
    payload["identity"] = digest
    return _canonical_json(payload), digest


def _type_payload(type_: HardwareType) -> dict[str, Any]:
    if isinstance(type_, BitType):
        return {"kind": "bit", "width": 1}
    if isinstance(type_, UIntType):
        return {"kind": "uint", "width": type_.width}
    if isinstance(type_, SIntType):
        return {"kind": "sint", "width": type_.width}
    if isinstance(type_, BitsType):
        return {"kind": "bits", "width": type_.width}
    if isinstance(type_, FixedType):
        return {
            "kind": "fixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, UFixedType):
        return {
            "kind": "ufixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, EnumType):
        return {
            "kind": "enum",
            "width": type_.width,
            "name": type_.name,
            "declaration_identity": type_.declaration_identity,
            "members": list(type_.members),
            "codes": list(type_.codes),
        }
    if isinstance(type_, VecType):
        return {
            "kind": "vec",
            "width": type_.width,
            "length": type_.length,
            "element": _type_payload(type_.element_type),
        }
    if isinstance(type_, TupleType):
        return {
            "kind": "tuple",
            "width": type_.width,
            "elements": [_type_payload(item) for item in type_.elements],
        }
    if isinstance(type_, StructType):
        return {
            "kind": "struct",
            "width": type_.width,
            "name": type_.name,
            "fields": [
                {"name": field.name, "type": _type_payload(field.type)}
                for field in type_.fields
            ],
        }
    if isinstance(type_, TaggedUnionType):
        return {
            "kind": "tagged_union",
            "width": type_.width,
            "name": type_.name,
            "declaration_identity": type_.declaration_identity,
            "tag_width": type_.tag_width,
            "payload_width": type_.payload_width,
            "variants": [
                {
                    "name": variant.name,
                    "fields": [
                        {"name": field.name, "type": _type_payload(field.type)}
                        for field in variant.fields
                    ],
                }
                for variant in type_.variants
            ],
        }
    raise JitUnsupportedFeatureError(
        f"native simulation does not support hardware type '{type_}'"
    )


def _pack_initial(type_: HardwareType, value: object) -> int:
    if isinstance(type_, EnumType):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not type_.is_valid_code(value)
        ):
            raise SimulationPlanError(
                f"initial value {value!r} is invalid for '{type_}'"
            )
        return value
    if isinstance(type_, TaggedUnionType):
        return ir_packing.pack_tagged_union_runtime(value)
    return ir_packing.pack_runtime(type_, value)


def _u64_limbs(value: int, width: int) -> list[int]:
    if value < 0 or value >= 1 << width:
        raise SimulationPlanError(f"packed value does not fit {width} bits")
    return [(value >> offset) & ((1 << 64) - 1) for offset in range(0, width, 64)]


def _json_attribute(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(
        value,
        (
            BitType,
            UIntType,
            SIntType,
            BitsType,
            FixedType,
            UFixedType,
            EnumType,
            VecType,
            TupleType,
            StructType,
            TaggedUnionType,
        ),
    ):
        return {"hardware_type": _type_payload(value)}
    if isinstance(value, tuple):
        return [_json_attribute(item) for item in value]
    if isinstance(value, list):
        return [_json_attribute(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_attribute(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise JitUnsupportedFeatureError(
        "native simulation cannot serialize expression metadata of type "
        f"'{type(value).__name__}'"
    )


def _origin_payload(origin: object) -> dict[str, object] | None:
    span = getattr(origin, "span", None)
    if span is None:
        return None
    payload = {
        "start_line": span.start_line,
        "start_column": span.start_column,
        "end_line": span.end_line,
        "end_column": span.end_column,
        "construct": getattr(origin, "construct", ""),
    }
    source_unit = getattr(origin, "source_unit", None)
    if isinstance(source_unit, str) and not PurePosixPath(source_unit).is_absolute():
        payload["source_unit"] = source_unit
    digest = getattr(origin, "digest", None)
    if isinstance(digest, str):
        payload["digest"] = digest
    return payload


_NATIVE_EXPRESSION_OPS = {
    ExpressionOp.INPUT,
    ExpressionOp.REGISTER_REF,
    ExpressionOp.CONSTANT,
    ExpressionOp.ENUM_ENCODE,
    ExpressionOp.ENUM_VALID,
    ExpressionOp.ENUM_DECODE,
    ExpressionOp.UNION_CONSTRUCT,
    ExpressionOp.UNION_TAG,
    ExpressionOp.UNION_FIELD,
    ExpressionOp.ADD,
    ExpressionOp.BINARY,
    ExpressionOp.EXTEND,
    ExpressionOp.TRUNCATE,
    ExpressionOp.FIXED_CONVERT,
    ExpressionOp.MUX,
    ExpressionOp.SWITCH,
    ExpressionOp.FIELD,
    ExpressionOp.STRUCT_CONSTRUCT,
    ExpressionOp.TUPLE_CONSTRUCT,
    ExpressionOp.TUPLE_PROJECT,
    ExpressionOp.VECTOR_INDEX,
    ExpressionOp.RUNTIME_INDEX,
    ExpressionOp.VECTOR_UPDATE,
    ExpressionOp.SLICE,
    ExpressionOp.CONCAT,
    ExpressionOp.VECTOR_CONCAT,
    ExpressionOp.RESHAPE,
    ExpressionOp.BITCAST,
    ExpressionOp.PACK,
    ExpressionOp.UNPACK,
    ExpressionOp.GENERATE,
    ExpressionOp.MAP,
    ExpressionOp.DOT,
    ExpressionOp.REDUCE,
}
_NATIVE_PLAN_OPS = {"fifo_ref", "memory_port_read", "rom_lookup"}


def _unsupported_module_features(module: object) -> tuple[str, ...]:
    features: list[str] = []
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        features.append("protocol ports")
    checks = (
        ("request/response interfaces", module.request_responses),
        ("connections", module.connections),
        ("CSR blocks", module.csr_blocks),
        ("packet arbiters", module.arbiters),
        ("instances", module.elaborated_instances or module.instances),
        ("hierarchical children", module.children),
        ("hierarchical connections", module.hierarchical_connections),
        ("aggregate protocol connections", module.aggregate_protocol_connections),
        ("request/response connections", module.request_response_connections),
        ("elastic pipelines", module.elastic_pipeline_regions),
    )
    features.extend(name for name, value in checks if value)
    if module.external_contract is not None:
        features.append("external module contract")
    return tuple(features)


def _erase_non_runtime_metadata(module: object) -> object:
    """Validate and erase compiler/formal metadata from executable planning.

    Equivalence rules are exact compile-time rewrite declarations. Historical
    ``Contract`` records are likewise not an independent execution surface:
    semantic analysis mirrors them into the verification overlay consumed by
    generic runtime probes.  Validate that mirror before discarding either
    record family so simulation cannot silently lose a source check.
    """

    clauses = [
        (scope, "requirement", clause)
        for scope in module.verification_scopes
        for clause in scope.requirements
    ]
    clauses.extend(
        (scope, "goal", clause)
        for scope in module.verification_scopes
        for clause in scope.goals
    )
    for contract in module.contracts:
        expected_kind = (
            "requirement" if contract.kind.value == "assume" else "goal"
        )
        matches = [
            (scope, clause)
            for scope, kind, clause in clauses
            if kind == expected_kind
            and scope.clock == contract.clock
            and scope.reset == contract.reset
            and clause.name == contract.name
            and clause.expression == contract.expression
            and (
                expected_kind == "requirement"
                or clause.kind is VerificationGoalKind.ASSERT
            )
        ]
        if len(matches) != 1:
            raise JitUnsupportedFeatureError(
                f"verification contract '{contract.name}' has no exact "
                "runtime verification-overlay mirror"
            )
    return replace(module, contracts=(), equivalences=())


def build_simulation_plan(module: object) -> SimulationPlan:
    """Build a strict executable plan from a post-planning semantic module.

    Static wire hierarchy is erased compiler-side before the primitive plan
    crosses the runtime boundary.  The native executor therefore consumes the
    same language-neutral schema for leaf and hierarchical designs.
    """

    from zlang.simulation_cdc import (
        CdcSimulationLoweringError,
        lower_cdc_module,
    )

    try:
        module = lower_cdc_module(module)
    except CdcSimulationLoweringError as error:
        raise JitUnsupportedFeatureError(str(error)) from error

    if module.request_responses and not (
        module.elaborated_instances or module.instances or module.children
    ):
        from zlang.simulation_protocols import (
            ProtocolSimulationLoweringError,
            lower_request_response_module,
        )

        try:
            module = lower_request_response_module(module)
        except ProtocolSimulationLoweringError as error:
            raise JitUnsupportedFeatureError(str(error)) from error

    if module.elaborated_instances or module.instances or module.children:
        from zlang.simulation_hierarchy import (
            HierarchicalSimulationError,
            compose_hierarchical_primitive_payload,
        )
        from zlang.simulation_protocols import (
            ProtocolSimulationLoweringError,
            lower_aggregate_protocol_hierarchy,
            lower_credit_hierarchy,
            lower_request_response_hierarchy,
            lower_ready_valid_hierarchy,
        )

        def has_request_response_hierarchy(current: object) -> bool:
            return bool(
                current.request_response_connections
                or any(
                    has_request_response_hierarchy(child)
                    for child in current.children
                )
            )

        def has_hierarchical_protocol(
            current: object,
            protocol: InterfaceProtocol,
        ) -> bool:
            return bool(
                any(
                    edge.source.protocol is protocol
                    or edge.destination.protocol is protocol
                    for edge in current.hierarchical_connections
                )
                or any(
                    has_hierarchical_protocol(child, protocol)
                    for child in current.children
                )
            )

        def has_aggregate_protocol_hierarchy(current: object) -> bool:
            return bool(
                current.aggregate_protocol_connections
                or current.aggregate_protocol_endpoints
                or any(
                    has_aggregate_protocol_hierarchy(child)
                    for child in current.children
                )
            )

        def has_protocol_surface(current: object) -> bool:
            return bool(
                any(
                    port.protocol is not InterfaceProtocol.WIRE
                    for port in current.ports
                )
                or current.hierarchical_connections
                or any(has_protocol_surface(child) for child in current.children)
            )

        try:
            if has_aggregate_protocol_hierarchy(module):
                module = lower_aggregate_protocol_hierarchy(
                    module, _physical_child=True
                )
            if has_request_response_hierarchy(module):
                module = lower_request_response_hierarchy(module)
            if has_hierarchical_protocol(module, InterfaceProtocol.CREDIT):
                module = lower_credit_hierarchy(module)
            if has_protocol_surface(module):
                module = lower_ready_valid_hierarchy(module)
            payload = compose_hierarchical_primitive_payload(
                module,
                _build_leaf_simulation_plan,
                max_nodes=MAX_PLAN_NODES,
            )
        except (HierarchicalSimulationError, ProtocolSimulationLoweringError) as error:
            raise JitUnsupportedFeatureError(str(error)) from error
        canonical_bytes, identity = _identity_bytes(payload)
        if len(canonical_bytes) > MAX_PLAN_BYTES:
            raise SimulationPlanError(
                f"simulation plan exceeds {MAX_PLAN_BYTES} encoded bytes"
            )
        payload["identity"] = identity
        _validate_plan_payload(payload)
        return SimulationPlan(payload, canonical_bytes, identity)
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        from zlang.simulation_protocols import (
            ProtocolSimulationLoweringError,
            lower_protocol_module,
        )

        try:
            module = lower_protocol_module(module)
        except ProtocolSimulationLoweringError as error:
            raise JitUnsupportedFeatureError(str(error)) from error
    return _build_leaf_simulation_plan(module)


def _build_leaf_simulation_plan(module: object) -> SimulationPlan:
    """Build one hierarchy-free executable primitive plan."""

    from zlang.simulation_csr import (
        CsrSimulationLoweringError,
        lower_csr_module,
    )
    from zlang.simulation_external import (
        ExternalModelSimulationLoweringError,
        lower_external_model,
    )

    try:
        module = lower_csr_module(module)
        module = lower_external_model(
            module,
            max_nodes=MAX_PLAN_NODES,
        )
    except (
        CsrSimulationLoweringError,
        ExternalModelSimulationLoweringError,
    ) as error:
        raise JitUnsupportedFeatureError(str(error)) from error
    # Canonical IR intentionally retains callable definitions for provenance
    # and backend emission.  Primitive simulation has no callable operation,
    # so remove definitions that are unreachable from this hierarchy shell
    # and perform the existing exact bounded inlining before serializing its
    # executable DAG.
    from zlang.ir.normalization import (
        SelectedValueNormalizationError,
        normalize_selected_values,
    )

    try:
        module = normalize_selected_values(
            module,
            total_inline_nodes=MAX_PLAN_NODES,
            inline_all_calls=True,
        )
    except SelectedValueNormalizationError as error:
        raise JitUnsupportedFeatureError(str(error)) from error
    module = _erase_non_runtime_metadata(module)

    unsupported = _unsupported_module_features(module)
    if unsupported:
        raise JitUnsupportedFeatureError(
            "native simulation does not yet support: " + ", ".join(unsupported)
        )
    canonical = lower(module, stage=OptimizationStage.SELECTED_ARCHITECTURE)
    if len(canonical.expressions) > MAX_PLAN_NODES:
        raise SimulationPlanError(
            f"simulation plan exceeds {MAX_PLAN_NODES} expression nodes"
        )

    for memory in canonical.memories:
        if memory.element_type.width > MAX_PLAN_MEMORY_WIDTH:
            raise JitUnsupportedFeatureError(
                f"memory '{memory.name}' has {memory.element_type.width}-bit cells; "
                f"simulation currently supports cells through {MAX_PLAN_MEMORY_WIDTH} bits"
            )
        scheduled = not memory.ports and memory.read_address is None
        port_domains = {port.domain for port in memory.ports}
        if (
            not 0 <= memory.read_latency <= 16
            or (
                memory.ports
                and not memory.async_memory
                and len(port_domains) != 1
            )
            or (scheduled and memory.read_latency != 1)
        ):
            raise JitUnsupportedFeatureError(
                "native simulation requires memory read_latency 0..16 and "
                "exact same-clock or 1W1R asynchronous ports"
            )
        if memory.depth * memory.element_type.width > MAX_PLAN_MEMORY_BITS:
            raise JitUnsupportedFeatureError(
                f"memory '{memory.name}' exceeds the native simulation bound of "
                f"{MAX_PLAN_MEMORY_BITS} storage bits"
            )

    for fifo in canonical.fifos:
        if fifo.depth * fifo.element_type.width > MAX_PLAN_MEMORY_BITS:
            raise JitUnsupportedFeatureError(
                f"FIFO '{fifo.name}' exceeds the native simulation bound of "
                f"{MAX_PLAN_MEMORY_BITS} storage bits"
            )

    nodes: list[dict[str, Any]] = []
    staged_expressions: list[dict[str, Any]] = []
    rom_result_names = {
        rom.name: "$zlang_jit_rom_"
        + hashlib.sha256(rom.semantic_id.encode("utf-8")).hexdigest()[:16]
        + "_read_data"
        for rom in canonical.roms
    }
    fifo_state = {
        fifo.name: {
            "prefix": "$zlang_jit_fifo_"
            + hashlib.sha256(
                f"{canonical.name}:{fifo.name}".encode("utf-8")
            ).hexdigest()[:16],
            "count_width": max(1, fifo.depth.bit_length()),
            "pointer_width": max(1, (fifo.depth - 1).bit_length()),
        }
        for fifo in canonical.fifos
    }
    memory_read_registers = {
        (memory.name, port_name): [
            "$zlang_jit_memory_"
            + hashlib.sha256(
                f"{memory.semantic_id}:{port_name or 'legacy'}".encode("utf-8")
            ).hexdigest()[:16]
            + f"_read_stage_{stage}"
            for stage in range(memory.read_latency)
        ]
        for memory in canonical.memories
        for port_name in (
            tuple(
                port.name
                for port in memory.ports
                if port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}
            )
            if memory.ports
            else (None,)
        )
    }
    semantic_registers = {register.name: register for register in module.registers}
    packed_register_initials: dict[str, int] = {}
    initial_node_values: dict[int, int] = {}
    for register in canonical.registers:
        try:
            initial_value = constant_runtime_value(
                semantic_registers[register.name].initial
            )
            packed_initial = _pack_initial(register.type, initial_value)
        except (ConstantExpressionError, ir_packing.PackingError) as error:
            raise JitUnsupportedFeatureError(
                f"native simulation requires a constant initial value for register "
                f"'{register.name}': {error}"
            ) from error
        packed_register_initials[register.name] = packed_initial
        previous = initial_node_values.setdefault(register.initial, packed_initial)
        if previous != packed_initial:
            raise SimulationPlanError(
                f"canonical initial node %{register.initial} has conflicting values"
            )
    for expected, node in enumerate(canonical.expressions):
        if node.id != expected:
            raise SimulationPlanError("canonical expression IDs are not contiguous")
        if (
            node.op is ExpressionOp.FUNCTIONAL_REGION
            and node.id in initial_node_values
        ):
            # The exact register initializer is stored separately in the plan.
            # Keep the canonical node ID stable without asking the executable
            # machine to interpret a compile-time functional region.
            nodes.append(
                {
                    "id": node.id,
                    "op": ExpressionOp.CONSTANT.value,
                    "type": _type_payload(node.type),
                    "operands": [],
                    "attributes": {
                        "limbs": _u64_limbs(
                            initial_node_values[node.id], node.type.width
                        )
                    },
                    "origins": [
                        encoded
                        for origin in node.origins
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            continue
        if node.op is ExpressionOp.ROM_REF:
            attributes = dict(node.attributes)
            rom_name = attributes.get("rom")
            signal = attributes.get("signal")
            if (
                not isinstance(rom_name, str)
                or rom_name not in rom_result_names
                or signal is not RomSignal.READ_DATA
                or node.operands
            ):
                raise SimulationPlanError(
                    f"ROM reference %{node.id} has invalid metadata"
                )
            nodes.append(
                {
                    "id": node.id,
                    "op": ExpressionOp.REGISTER_REF.value,
                    "type": _type_payload(node.type),
                    "operands": [],
                    "attributes": {"name": rom_result_names[rom_name]},
                    "origins": [
                        encoded
                        for origin in node.origins
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            continue
        if node.op is ExpressionOp.FIFO_REF:
            attributes = dict(node.attributes)
            fifo_name = attributes.get("fifo")
            signal = attributes.get("signal")
            if (
                not isinstance(fifo_name, str)
                or fifo_name not in fifo_state
                or not isinstance(signal, FifoSignal)
                or node.operands
            ):
                raise SimulationPlanError(
                    f"FIFO reference %{node.id} has invalid metadata"
                )
            nodes.append(
                {
                    "id": node.id,
                    "op": "fifo_ref",
                    "type": _type_payload(node.type),
                    "operands": [],
                    "attributes": {"fifo": fifo_name, "signal": signal.value},
                    "origins": [
                        encoded
                        for origin in node.origins
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            continue
        if node.op is ExpressionOp.MEMORY_REF:
            attributes = dict(node.attributes)
            memory_name = attributes.get("memory")
            signal = attributes.get("signal")
            port = attributes.get("port")
            key = (memory_name, port)
            if (
                not isinstance(memory_name, str)
                or key not in memory_read_registers
                or signal is not MemorySignal.READ_DATA
                or node.operands
            ):
                raise SimulationPlanError(
                    f"memory reference %{node.id} has invalid metadata"
                )
            read_registers = memory_read_registers[key]
            if read_registers:
                replacement_op = ExpressionOp.REGISTER_REF.value
                replacement_operands: list[int] = []
                replacement_attributes = {"name": read_registers[-1]}
            else:
                memory = next(item for item in canonical.memories if item.name == memory_name)
                if port is None:
                    assert memory.read_address is not None
                    address = memory.read_address
                else:
                    address = next(item.address for item in memory.ports if item.name == port)
                replacement_op = "memory_port_read"
                replacement_operands = [address]
                replacement_attributes = {"memory": memory_name, "port": port}
            nodes.append(
                {
                    "id": node.id,
                    "op": replacement_op,
                    "type": _type_payload(node.type),
                    "operands": replacement_operands,
                    "attributes": replacement_attributes,
                    "origins": [
                        encoded
                        for origin in node.origins
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            continue
        if node.op in {ExpressionOp.DELAY, ExpressionOp.PIPELINE}:
            attributes = dict(node.attributes)
            stage_count_name = "cycles" if node.op is ExpressionOp.DELAY else "stages"
            stage_count = attributes.get(stage_count_name)
            instance = attributes.get("instance")
            domain = attributes.get("domain") or canonical.clock
            if (
                isinstance(stage_count, bool)
                or not isinstance(stage_count, int)
                or stage_count < 1
                or isinstance(instance, bool)
                or not isinstance(instance, int)
                or len(node.operands) != 1
                or not isinstance(domain, str)
                or not domain
            ):
                raise SimulationPlanError(
                    f"sequential expression %{node.id} has invalid stage metadata"
                )
            stage_names = [
                f"$zlang_jit_stage_{instance}_{stage}" for stage in range(stage_count)
            ]
            staged_expressions.append(
                {
                    "source": node.operands[0],
                    "type": node.type,
                    "domain": domain,
                    "names": stage_names,
                    "final_node": node.id,
                    "origins": node.origins,
                }
            )
            nodes.append(
                {
                    "id": node.id,
                    "op": ExpressionOp.REGISTER_REF.value,
                    "type": _type_payload(node.type),
                    "operands": [],
                    "attributes": {"name": stage_names[-1]},
                    "origins": [
                        encoded
                        for origin in node.origins
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            continue
        if node.op not in _NATIVE_EXPRESSION_OPS:
            raise JitUnsupportedFeatureError(
                f"native simulation does not yet support expression '{node.op.value}'"
            )
        if node.type.width > MAX_PLAN_WIDTH:
            raise JitUnsupportedFeatureError(
                "simulation currently supports packed values through "
                f"{MAX_PLAN_WIDTH} bits; expression %{node.id} has width "
                f"{node.type.width}"
            )
        attributes = {name: _json_attribute(value) for name, value in node.attributes}
        if node.op is ExpressionOp.ENUM_DECODE:
            attributes.setdefault(
                "enum_type", {"hardware_type": _type_payload(node.type)}
            )
        if (
            node.op is ExpressionOp.FIXED_CONVERT
            and attributes.get("rational_denominator") is not None
        ):
            raise JitUnsupportedFeatureError(
                "native simulation requires rational fixed-point constants to "
                "be normalized before plan construction"
            )
        if node.op is ExpressionOp.CONSTANT:
            value = attributes.pop("value", None)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SimulationPlanError(
                    f"constant node %{node.id} has no exact integer value"
                )
            try:
                packed_value = _pack_initial(node.type, value)
            except ir_packing.PackingError as error:
                raise SimulationPlanError(
                    f"constant node %{node.id} cannot be packed: {error}"
                ) from error
            attributes["limbs"] = _u64_limbs(packed_value, node.type.width)
        nodes.append(
            {
                "id": node.id,
                "op": node.op.value,
                "type": _type_payload(node.type),
                "operands": list(node.operands),
                "attributes": attributes,
                "origins": [
                    encoded
                    for origin in node.origins
                    if (encoded := _origin_payload(origin)) is not None
                ],
            }
        )

    # Verification is an execution overlay with its own canonical DAG so it
    # cannot perturb hardware/candidate identity.  Link sampled output names
    # back to their already-lowered producers, then append only the remaining
    # nodes before primitive lowering.  The runtime never sees
    # ZLang goal kinds, requirements, scopes, or reset semantics; those are
    # converted below into generic check/cover probes over one-bit nodes.
    if len(nodes) + len(canonical.verification_expressions) > MAX_PLAN_NODES:
        raise SimulationPlanError(
            f"simulation plan exceeds {MAX_PLAN_NODES} expression nodes"
        )
    output_expression_by_name = {
        assignment.target_name: assignment.expression
        for assignment in canonical.assignments
        if assignment.target_kind is TargetKind.PORT
    }
    verification_node_ids: dict[int, int] = {}
    for expected, node in enumerate(canonical.verification_expressions):
        if node.id != expected:
            raise SimulationPlanError(
                "canonical verification expression IDs are not contiguous"
            )
        if node.op not in _NATIVE_EXPRESSION_OPS:
            raise JitUnsupportedFeatureError(
                "native verification does not yet support expression "
                f"'{node.op.value}'"
            )
        if node.type.width > MAX_PLAN_WIDTH:
            raise JitUnsupportedFeatureError(
                "simulation verification currently supports packed values through "
                f"{MAX_PLAN_WIDTH} bits; expression %{node.id} has width "
                f"{node.type.width}"
            )
        attributes = {name: _json_attribute(value) for name, value in node.attributes}
        if (
            node.op is ExpressionOp.INPUT
            and attributes.get("name") in output_expression_by_name
        ):
            verification_node_ids[node.id] = output_expression_by_name[
                attributes["name"]
            ]
            continue
        if node.op is ExpressionOp.ENUM_DECODE:
            attributes.setdefault(
                "enum_type", {"hardware_type": _type_payload(node.type)}
            )
        if (
            node.op is ExpressionOp.FIXED_CONVERT
            and attributes.get("rational_denominator") is not None
        ):
            raise JitUnsupportedFeatureError(
                "native verification requires rational fixed-point constants "
                "to be normalized before plan construction"
            )
        if node.op is ExpressionOp.CONSTANT:
            value = attributes.pop("value", None)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SimulationPlanError(
                    f"verification constant node %{node.id} has no exact integer value"
                )
            try:
                packed_value = _pack_initial(node.type, value)
            except ir_packing.PackingError as error:
                raise SimulationPlanError(
                    f"verification constant node %{node.id} cannot be packed: {error}"
                ) from error
            attributes["limbs"] = _u64_limbs(packed_value, node.type.width)
        identifier = len(nodes)
        verification_node_ids[node.id] = identifier
        nodes.append(
            {
                "id": identifier,
                "op": node.op.value,
                "type": _type_payload(node.type),
                "operands": [verification_node_ids[item] for item in node.operands],
                "attributes": attributes,
                "origins": [
                    encoded
                    for origin in node.origins
                    if (encoded := _origin_payload(origin)) is not None
                ],
            }
        )

    events: list[dict[str, Any]] = []
    instrumentation_scopes: list[dict[str, Any]] = []

    def instrumentation_event(
        *,
        category: str,
        scope: object,
        clause: object,
        goal_kind: str | None = None,
    ) -> int:
        identifier = len(events)
        if identifier >= MAX_PLAN_EVENTS:
            raise SimulationPlanError(
                f"simulation plan exceeds {MAX_PLAN_EVENTS} instrumentation events"
            )
        metadata: dict[str, Any] = {
            "category": category,
            "scope_id": scope.semantic_id,
            "scope_name": scope.name,
            "clause_id": clause.semantic_id,
            "clause_name": clause.name,
            "hierarchy_path": [canonical.name],
            "source_origin": _origin_payload(clause.source_origin),
        }
        if goal_kind is not None:
            metadata["goal_kind"] = goal_kind
        events.append({"id": identifier, "metadata": metadata})
        return identifier

    for scope in canonical.verification_scopes:
        requirements = [
            {
                "node": verification_node_ids[requirement.expression],
                "event": instrumentation_event(
                    category="requirement_violation",
                    scope=scope,
                    clause=requirement,
                ),
            }
            for requirement in scope.requirements
        ]
        goals = []
        for goal in scope.goals:
            if scope.semantic_id.startswith("$zlang_runtime_protocol:"):
                category = "runtime_violation"
            else:
                category = (
                    "cover_witness"
                    if goal.kind is VerificationGoalKind.COVER
                    else "assertion_failure"
                )
            goals.append(
                {
                    "node": verification_node_ids[goal.expression],
                    "event": instrumentation_event(
                        category=category,
                        scope=scope,
                        clause=goal,
                        goal_kind=(
                            None
                            if category == "runtime_violation"
                            else goal.kind.value
                        ),
                    ),
                    "kind": goal.kind.value,
                }
            )
        instrumentation_scopes.append(
            {
                "clock": scope.clock,
                "reset": scope.reset,
                "requirements": requirements,
                "goals": goals,
            }
        )

    ports = [
        {
            "name": port.name,
            "direction": port.direction.value,
            "type": _type_payload(port.type),
            "domain": port.domain,
        }
        for port in canonical.ports
    ]
    outputs = []
    for assignment in canonical.assignments:
        if assignment.target_kind is not TargetKind.PORT:
            raise JitUnsupportedFeatureError(
                "native simulation supports only direct public-port assignments"
            )
        outputs.append({"name": assignment.target_name, "node": assignment.expression})

    registers = []
    for register in canonical.registers:
        registers.append(
            {
                "name": register.name,
                "type": _type_payload(register.type),
                "initial": register.initial,
                "initial_limbs": _u64_limbs(
                    packed_register_initials[register.name], register.type.width
                ),
                "domain": register.domain or canonical.clock,
            }
        )

    domains = [
        {
            "clock": domain.clock,
            "reset": domain.reset,
            "edge": domain.edge.value,
            "reset_mode": domain.reset_mode.value,
            "reset_polarity": domain.reset_polarity.value,
            "reset_release_mode": domain.reset_release_mode.value,
            "reset_release_cycles": domain.reset_release_cycles,
        }
        for domain in canonical.clock_domains
    ]
    if canonical.clock and not domains:
        domains.append(
            {
                "clock": canonical.clock,
                "reset": canonical.reset,
                "edge": "rising",
                "reset_mode": "synchronous",
                "reset_polarity": "active_high",
                "reset_release_mode": "native",
                "reset_release_cycles": 0,
            }
        )
    direct_next = [
        {
            "target": item.target_name,
            "node": item.expression,
            "activation": item.activation,
            "domain": next(
                (
                    register.domain or canonical.clock
                    for register in canonical.registers
                    if register.name == item.target_name
                ),
                canonical.clock,
            ),
        }
        for item in canonical.next_assignments
        if item.target_kind is TargetKind.REGISTER
    ]

    # Delay and fixed-pipeline expressions are state, even though the semantic
    # IR intentionally keeps them embedded in the expression DAG.  Materialize
    # their stages in the plan so every native backend consumes the same
    # compiler-owned schedule: stage zero samples the source and every later
    # stage samples its predecessor from the common pre-edge snapshot.
    for staged in staged_expressions:
        type_ = staged["type"]
        assert isinstance(type_, HardwareType)
        names = staged["names"]
        assert isinstance(names, list)
        zero_node = len(nodes)
        nodes.append(
            {
                "id": zero_node,
                "op": ExpressionOp.CONSTANT.value,
                "type": _type_payload(type_),
                "operands": [],
                "attributes": {"limbs": _u64_limbs(0, type_.width)},
                "origins": [],
            }
        )
        predecessor_nodes: list[int] = []
        for stage, name in enumerate(names):
            if stage == len(names) - 1:
                reference_node = staged["final_node"]
                assert isinstance(reference_node, int)
            else:
                reference_node = len(nodes)
                nodes.append(
                    {
                        "id": reference_node,
                        "op": ExpressionOp.REGISTER_REF.value,
                        "type": _type_payload(type_),
                        "operands": [],
                        "attributes": {"name": name},
                        "origins": [],
                    }
                )
            predecessor_nodes.append(reference_node)
            registers.append(
                {
                    "name": name,
                    "type": _type_payload(type_),
                    "initial": zero_node,
                    "initial_limbs": _u64_limbs(0, type_.width),
                    "domain": staged["domain"],
                }
            )
            direct_next.append(
                {
                    "target": name,
                    "node": (
                        staged["source"] if stage == 0 else predecessor_nodes[stage - 1]
                    ),
                    "activation": None,
                    "domain": staged["domain"],
                }
            )

    for rom in canonical.roms:
        domain = rom.domain or canonical.clock
        if not isinstance(domain, str) or not domain:
            raise SimulationPlanError(f"ROM '{rom.name}' has no owning clock domain")
        zero_node = len(nodes)
        nodes.append(
            {
                "id": zero_node,
                "op": ExpressionOp.CONSTANT.value,
                "type": _type_payload(rom.element_type),
                "operands": [],
                "attributes": {"limbs": _u64_limbs(0, rom.element_type.width)},
                "origins": [],
            }
        )
        lookup_node = len(nodes)
        nodes.append(
            {
                "id": lookup_node,
                "op": "rom_lookup",
                "type": _type_payload(rom.element_type),
                "operands": [rom.read_address, *rom.contents],
                "attributes": {"depth": rom.depth},
                "origins": [
                    encoded
                    for origin in (rom.source_origin,)
                    if (encoded := _origin_payload(origin)) is not None
                ],
            }
        )
        result_name = rom_result_names[rom.name]
        registers.append(
            {
                "name": result_name,
                "type": _type_payload(rom.element_type),
                "initial": zero_node,
                "initial_limbs": _u64_limbs(0, rom.element_type.width),
                "domain": domain,
            }
        )
        direct_next.append(
            {
                "target": result_name,
                "node": lookup_node,
                "activation": None,
                "domain": domain,
            }
        )
    memories: list[dict[str, Any]] = []
    fifos: list[dict[str, Any]] = []
    for fifo in canonical.fifos:
        domain = fifo.domain or canonical.clock
        if not isinstance(domain, str) or not domain:
            raise SimulationPlanError(f"FIFO '{fifo.name}' has no owning clock domain")
        state = fifo_state[fifo.name]
        prefix = state["prefix"]
        count_width = int(state["count_width"])
        pointer_width = int(state["pointer_width"])
        count_name = f"{prefix}_count"
        read_pointer = f"{prefix}_read_pointer"
        write_pointer = f"{prefix}_write_pointer"
        memory_name = f"{prefix}_storage"
        for name, width in (
            (count_name, count_width),
            (read_pointer, pointer_width),
            (write_pointer, pointer_width),
        ):
            zero_node = len(nodes)
            type_ = UIntType(width)
            nodes.append(
                {
                    "id": zero_node,
                    "op": ExpressionOp.CONSTANT.value,
                    "type": _type_payload(type_),
                    "operands": [],
                    "attributes": {"limbs": _u64_limbs(0, width)},
                    "origins": [],
                }
            )
            registers.append(
                {
                    "name": name,
                    "type": _type_payload(type_),
                    "initial": zero_node,
                    "initial_limbs": _u64_limbs(0, width),
                    "domain": domain,
                }
            )
        memories.append(
            {
                "name": memory_name,
                "type": _type_payload(fifo.element_type),
                "depth": fifo.depth,
                "domain": domain,
                "collision": "read_first",
                "write_mask_width": None,
                "contents_reset": "preserve",
                "read_data_reset": "preserve",
                "initial_limbs": _u64_limbs(0, fifo.element_type.width),
                "ports": [],
                "write_priority": [],
                "managed_by": "fifo",
            }
        )
        fifos.append(
            {
                "name": fifo.name,
                "type": _type_payload(fifo.element_type),
                "depth": fifo.depth,
                "domain": domain,
                "scheduled": fifo.data is None,
                "data": fifo.data,
                "push": fifo.push,
                "pop": fifo.pop,
                "memory": memory_name,
                "count": count_name,
                "read_pointer": read_pointer,
                "write_pointer": write_pointer,
                "count_width": count_width,
                "pointer_width": pointer_width,
                "reset": next(
                    (item["reset"] for item in domains if item["clock"] == domain),
                    None,
                ),
            }
        )
    semantic_memories = {memory.name: memory for memory in module.memories}
    for memory in canonical.memories:
        domain = memory.domain or canonical.clock
        if memory.async_memory:
            domain = next(
                (
                    port.domain
                    for port in memory.ports
                    if port.kind is MemoryPortKind.WRITE
                ),
                None,
            )
        if not isinstance(domain, str) or not domain:
            raise SimulationPlanError(
                f"memory '{memory.name}' has no owning clock domain"
            )
        semantic_memory = semantic_memories[memory.name]
        try:
            initial_value = (
                constant_runtime_value(semantic_memory.initial_value)
                if semantic_memory.initial_value is not None
                else 0
            )
            packed_initial = _pack_initial(memory.element_type, initial_value)
        except (ConstantExpressionError, ir_packing.PackingError) as error:
            raise JitUnsupportedFeatureError(
                f"native simulation requires a constant initial value for memory "
                f"'{memory.name}': {error}"
            ) from error
        zero_node = len(nodes)
        nodes.append(
            {
                "id": zero_node,
                "op": ExpressionOp.CONSTANT.value,
                "type": _type_payload(memory.element_type),
                "operands": [],
                "attributes": {"limbs": _u64_limbs(0, memory.element_type.width)},
                "origins": [],
            }
        )
        scheduled = not memory.ports and memory.read_address is None
        if scheduled:
            read_registers = memory_read_registers[(memory.name, None)]
            registers.extend(
                {
                    "name": read_register,
                    "type": _type_payload(memory.element_type),
                    "initial": zero_node,
                    "initial_limbs": _u64_limbs(0, memory.element_type.width),
                    "domain": domain,
                }
                for read_register in read_registers
            )
            port_records = []
        elif memory.ports:
            port_records = []
            for port in memory.ports:
                read_registers = memory_read_registers.get(
                    (memory.name, port.name), []
                )
                registers.extend(
                    {
                        "name": read_register,
                        "type": _type_payload(memory.element_type),
                        "initial": zero_node,
                        "initial_limbs": _u64_limbs(0, memory.element_type.width),
                        "domain": port.domain,
                    }
                    for read_register in read_registers
                )
                port_records.append(
                    {
                        "name": port.name,
                        "kind": port.kind.value,
                        "domain": port.domain,
                        "address": port.address,
                        "write_address": port.address,
                        "read_enable": port.read_enable,
                        "write_enable": port.write_enable,
                        "write_data": port.write_data,
                        "write_mask": port.write_mask,
                        "read_registers": read_registers,
                    }
                )
        else:
            read_registers = memory_read_registers[(memory.name, None)]
            registers.extend(
                {
                    "name": read_register,
                    "type": _type_payload(memory.element_type),
                    "initial": zero_node,
                    "initial_limbs": _u64_limbs(0, memory.element_type.width),
                    "domain": domain,
                }
                for read_register in read_registers
            )
            assert memory.read_address is not None
            assert memory.write_enable is not None
            assert memory.write_address is not None
            assert memory.write_data is not None
            port_records = [
                {
                    "name": None,
                    "kind": MemoryPortKind.READ_WRITE.value,
                    "domain": domain,
                    "address": memory.read_address,
                    "write_address": memory.write_address,
                    "read_enable": None,
                    "write_enable": memory.write_enable,
                    "write_data": memory.write_data,
                    "write_mask": memory.write_mask,
                    "read_registers": read_registers,
                }
            ]
        memories.append(
            {
                "name": memory.name,
                "type": _type_payload(memory.element_type),
                "depth": memory.depth,
                "domain": domain,
                "async_memory": memory.async_memory,
                "reset": next(
                    (item["reset"] for item in domains if item["clock"] == domain),
                    None,
                ),
                "collision": memory.collision.value,
                "write_mask_width": memory.write_mask_width,
                "contents_reset": memory.contents_reset.value,
                "read_data_reset": memory.read_data_reset.value,
                "initial_limbs": _u64_limbs(packed_initial, memory.element_type.width),
                "ports": port_records,
                "write_priority": list(memory.write_priority),
                "managed_by": "scheduled_memory" if scheduled else "memory",
                "scheduled_read_registers": read_registers if scheduled else [],
            }
        )
    if len(nodes) > MAX_PLAN_NODES:
        raise SimulationPlanError(
            f"simulation plan exceeds {MAX_PLAN_NODES} expression nodes after "
            "sequential-state materialization"
        )

    transitions: list[dict[str, Any]] = []
    scheduler_fifos: list[str] = []
    scheduler_guards: list[str] = []
    scheduler_activations: list[int] = []
    transition = canonical.resolved_transition
    if transition is not None:
        resources = {item.semantic_id: item for item in transition.resources}
        ordered = ordered_groups(transition)
        scheduler_fifos = [
            item.name
            for item in transition.resources
            if item.kind is StateResourceKind.FIFO
        ]
        scheduler_guards = [item.rule_name for item in ordered]
        scheduler_activations = list(conditional_activation_predicates(transition))
        for group in ordered:
            actions = []
            for action in group.actions:
                resource = resources[action.resource_id]
                valid = (
                    action.kind is StateActionKind.REGISTER_WRITE
                    and resource.kind is StateResourceKind.REGISTER
                    and len(action.operands) == 1
                ) or (
                    action.kind is StateActionKind.FIFO_PUSH
                    and resource.kind is StateResourceKind.FIFO
                    and len(action.operands) == 1
                ) or (
                    action.kind is StateActionKind.FIFO_POP
                    and resource.kind is StateResourceKind.FIFO
                    and not action.operands
                ) or (
                    action.kind is StateActionKind.MEMORY_READ_REQUEST
                    and resource.kind is StateResourceKind.MEMORY
                    and len(action.operands) == 1
                ) or (
                    action.kind is StateActionKind.MEMORY_WRITE
                    and resource.kind is StateResourceKind.MEMORY
                    and len(action.operands) in {2, 3}
                )
                if not valid:
                    raise JitUnsupportedFeatureError(
                        "native simulation currently accepts register writes and "
                        "synchronous FIFO push/pop scheduled actions"
                    )
                actions.append(
                    {
                        "kind": action.kind.value,
                        "target": resource.name,
                        "node": action.operands[0] if action.operands else None,
                        "operands": list(action.operands),
                        "activation": action.activation,
                    }
                )
            transitions.append(
                {
                    "name": group.rule_name,
                    "domain": group.domain or transition.domain or canonical.clock,
                    "guard": group.guard,
                    "selection_regions": [
                        [
                            value.value if isinstance(value, Enum) else value
                            for value in region
                        ]
                        for region in selection_regions(
                            transition, group.rule_name
                        )
                    ],
                    "actions": actions,
                }
            )

    payload: dict[str, Any] = {
        "schema": SIMULATION_PLAN_SCHEMA,
        "runtime_abi": SIMULATION_RUNTIME_ABI,
        "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
        "canonical_ir_identity": canonical_ir_identity(canonical),
        "native_target": {
            "triple": f"{platform.machine().lower()}-unknown-{platform.system().lower()}-gnu",
            "cranelift": CRANELIFT_VERSION,
            "isa_flags": ["native"],
        },
        "module": canonical.name,
        "identity": "",
        "ports": ports,
        "nodes": nodes,
        "outputs": outputs,
        "registers": registers,
        "memories": memories,
        "events": events,
        "instrumentation_scopes": instrumentation_scopes,
        "fifos": fifos,
        "domains": domains,
        "direct_next": direct_next,
        "transitions": transitions,
        "scheduler_fifos": scheduler_fifos,
        "scheduler_guards": scheduler_guards,
        "scheduler_activations": scheduler_activations,
    }
    try:
        payload = lower_to_primitive_plan(payload, max_nodes=MAX_PLAN_NODES)
    except PrimitiveLoweringError as error:
        raise SimulationPlanError(str(error)) from error
    canonical_bytes, identity = _identity_bytes(payload)
    if len(canonical_bytes) > MAX_PLAN_BYTES:
        raise SimulationPlanError(
            f"simulation plan exceeds {MAX_PLAN_BYTES} encoded bytes"
        )
    payload["identity"] = identity
    _validate_plan_payload(payload)
    return SimulationPlan(payload, canonical_bytes, identity)


def _validate_type_payload(type_: object) -> None:
    if not isinstance(type_, dict):
        raise SimulationPlanError("hardware type record must be an object")
    kind = type_.get("kind")
    width = type_.get("width")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not 1 <= width <= MAX_PLAN_WIDTH
    ):
        raise SimulationPlanError("hardware type width is invalid")
    scalar_fields = {"kind", "width"}
    if kind == "bit":
        if set(type_) != scalar_fields or width != 1:
            raise SimulationPlanError("bit type record is invalid")
        return
    if kind in {"uint", "sint", "bits"}:
        if set(type_) != scalar_fields:
            raise SimulationPlanError(f"{kind} type record is invalid")
        return
    if kind in {"fixed", "ufixed"}:
        if set(type_) != {*scalar_fields, "fraction", "overflow"}:
            raise SimulationPlanError(f"{kind} type record is invalid")
        fraction = type_["fraction"]
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, int)
            or not 0 <= fraction < width
            or type_["overflow"] not in {"wrap", "saturate"}
        ):
            raise SimulationPlanError(f"{kind} type policy is invalid")
        return
    if kind == "enum":
        if set(type_) != {
            *scalar_fields,
            "name",
            "declaration_identity",
            "members",
            "codes",
        }:
            raise SimulationPlanError("enum type record is invalid")
        members = type_["members"]
        codes = type_["codes"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(type_["declaration_identity"], str)
            or not type_["declaration_identity"]
            or not isinstance(members, list)
            or not members
            or not all(isinstance(item, str) and item for item in members)
            or len(set(members)) != len(members)
            or not isinstance(codes, list)
            or len(codes) != len(members)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < 1 << width
                for item in codes
            )
            or len(set(codes)) != len(codes)
        ):
            raise SimulationPlanError("enum type declaration is invalid")
        return
    if kind == "vec":
        if set(type_) != {*scalar_fields, "length", "element"}:
            raise SimulationPlanError("vector type record is invalid")
        length = type_["length"]
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            raise SimulationPlanError("vector length is invalid")
        _validate_type_payload(type_["element"])
        if width != length * type_["element"]["width"]:
            raise SimulationPlanError("vector packed width is invalid")
        return
    if kind == "tuple":
        if set(type_) != {*scalar_fields, "elements"}:
            raise SimulationPlanError("tuple type record is invalid")
        elements = type_["elements"]
        if not isinstance(elements, list) or not elements:
            raise SimulationPlanError("tuple element table is invalid")
        for element in elements:
            _validate_type_payload(element)
        if width != sum(element["width"] for element in elements):
            raise SimulationPlanError("tuple packed width is invalid")
        return
    if kind == "struct":
        if set(type_) != {*scalar_fields, "name", "fields"}:
            raise SimulationPlanError("struct type record is invalid")
        fields = type_["fields"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(fields, list)
            or not fields
        ):
            raise SimulationPlanError("struct declaration is invalid")
        names: list[str] = []
        for field in fields:
            if (
                not isinstance(field, dict)
                or set(field) != {"name", "type"}
                or not isinstance(field["name"], str)
                or not field["name"]
            ):
                raise SimulationPlanError("struct field is invalid")
            names.append(field["name"])
            _validate_type_payload(field["type"])
        if len(names) != len(set(names)) or width != sum(
            field["type"]["width"] for field in fields
        ):
            raise SimulationPlanError("struct packed layout is invalid")
        return
    if kind == "tagged_union":
        if set(type_) != {
            *scalar_fields,
            "name",
            "declaration_identity",
            "tag_width",
            "payload_width",
            "variants",
        }:
            raise SimulationPlanError("tagged-union type record is invalid")
        variants = type_["variants"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(type_["declaration_identity"], str)
            or not type_["declaration_identity"]
            or not isinstance(variants, list)
            or not variants
        ):
            raise SimulationPlanError("tagged-union declaration is invalid")
        variant_names: list[str] = []
        payload_widths: list[int] = []
        for variant in variants:
            if (
                not isinstance(variant, dict)
                or set(variant) != {"name", "fields"}
                or not isinstance(variant["name"], str)
                or not variant["name"]
                or not isinstance(variant["fields"], list)
            ):
                raise SimulationPlanError("tagged-union variant is invalid")
            variant_names.append(variant["name"])
            field_names: list[str] = []
            payload_width = 0
            for field in variant["fields"]:
                if (
                    not isinstance(field, dict)
                    or set(field) != {"name", "type"}
                    or not isinstance(field["name"], str)
                    or not field["name"]
                ):
                    raise SimulationPlanError("tagged-union field is invalid")
                field_names.append(field["name"])
                _validate_type_payload(field["type"])
                payload_width += field["type"]["width"]
            if len(field_names) != len(set(field_names)):
                raise SimulationPlanError("tagged-union field names are not unique")
            payload_widths.append(payload_width)
        expected_tag = max(1, (len(variants) - 1).bit_length())
        expected_payload = max(payload_widths)
        if (
            len(variant_names) != len(set(variant_names))
            or type_["tag_width"] != expected_tag
            or type_["payload_width"] != expected_payload
            or width != expected_tag + expected_payload
        ):
            raise SimulationPlanError("tagged-union packed layout is invalid")
        return
    raise SimulationPlanError(f"unsupported hardware type kind '{kind}'")


def _validate_u64_limbs(value: object, width: int) -> bool:
    count = (width + 63) // 64
    if not isinstance(value, list) or len(value) != count:
        return False
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < 1 << 64
        for item in value
    ):
        return False
    used = width - 64 * (count - 1)
    return used == 64 or value[-1] < 1 << used


def _validate_plan_payload(payload: dict[str, Any]) -> None:
    required = {
        "schema",
        "runtime_abi",
        "packing_layout_schema",
        "canonical_ir_identity",
        "native_target",
        "module",
        "identity",
        "ports",
        "nodes",
        "outputs",
        "registers",
        "memories",
        "events",
        "domains",
        "edge_programs",
    }
    if set(payload) != required:
        raise SimulationPlanError("simulation plan fields do not match its schema")
    if payload["schema"] != SIMULATION_PLAN_SCHEMA:
        raise SimulationPlanError("unsupported simulation plan schema")
    if payload["runtime_abi"] != SIMULATION_RUNTIME_ABI:
        raise SimulationPlanError("unsupported native simulation ABI")
    if payload["packing_layout_schema"] != PACKING_LAYOUT_SCHEMA:
        raise SimulationPlanError("unsupported packed-layout schema")
    if (
        not isinstance(payload["canonical_ir_identity"], str)
        or not payload["canonical_ir_identity"]
    ):
        raise SimulationPlanError("canonical IR identity is missing")
    if not isinstance(payload["module"], str) or not payload["module"]:
        raise SimulationPlanError("simulation module name is missing")
    native_target = payload["native_target"]
    if native_target != {
        "triple": "x86_64-unknown-linux-gnu",
        "cranelift": CRANELIFT_VERSION,
        "isa_flags": ["native"],
    }:
        raise SimulationPlanError(
            "native JIT v1 requires the Linux x86-64 Cranelift recipe"
        )
    nodes = payload["nodes"]
    if not isinstance(nodes, list) or len(nodes) > MAX_PLAN_NODES:
        raise SimulationPlanError("simulation plan node table is invalid")
    limb_work = 0
    for expected, node in enumerate(nodes):
        if (
            not isinstance(node, dict)
            or set(node) != {"id", "op", "width", "operands", "attributes", "origins"}
            or node.get("id") != expected
        ):
            raise SimulationPlanError("simulation plan node IDs are not contiguous")
        if node.get("op") not in PRIMITIVE_OPS:
            raise SimulationPlanError(
                f"simulation plan node %{expected} has an unsupported operation"
            )
        width = node.get("width")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= MAX_PLAN_WIDTH
        ):
            raise SimulationPlanError(
                f"simulation plan node %{expected} has an invalid width"
            )
        limb_work += (width + 63) // 64
        if limb_work > MAX_PLAN_LIMB_WORK:
            raise SimulationPlanError(
                f"simulation plan exceeds {MAX_PLAN_LIMB_WORK} packed node limbs"
            )
        if not isinstance(node.get("attributes"), dict) or not isinstance(
            node.get("origins"), list
        ):
            raise SimulationPlanError(
                f"simulation plan node %{expected} metadata is invalid"
            )
        if node["op"] == "constant" and not _validate_u64_limbs(
            node["attributes"].get("limbs"), width
        ):
            raise SimulationPlanError(
                f"simulation plan constant node %{expected} has invalid limbs"
            )
        operands = node.get("operands")
        if not isinstance(operands, list) or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item < expected
            for item in operands
        ):
            raise SimulationPlanError(
                f"simulation plan node %{expected} has an invalid operand"
            )
        _validate_primitive_node(node, nodes)
    expected_fields = {
        "ports": {"name", "direction", "width", "api_type", "domain"},
        "registers": {
            "name",
            "width",
            "initial_limbs",
            "domain",
        },
    }
    names: set[str] = set()
    for table in ("ports", "registers"):
        if not isinstance(payload[table], list):
            raise SimulationPlanError(f"simulation plan {table} table is invalid")
        for item in payload[table]:
            if (
                not isinstance(item, dict)
                or set(item) != expected_fields[table]
                or not isinstance(item.get("name"), str)
                or not item["name"]
            ):
                raise SimulationPlanError(f"simulation plan {table} entry is invalid")
            if item["name"] in names:
                raise SimulationPlanError(
                    f"simulation plan has duplicate signal '{item['name']}'"
                )
            names.add(item["name"])
            width = item.get("width")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or not 1 <= width <= MAX_PLAN_WIDTH
            ):
                raise SimulationPlanError(f"simulation plan {table} width is invalid")
            if table == "ports":
                _validate_type_payload(item.get("api_type"))
                if item["api_type"]["width"] != width:
                    raise SimulationPlanError(
                        "simulation port API type width is inconsistent"
                    )
            if table == "ports" and item.get("direction") not in {"input", "output"}:
                raise SimulationPlanError("simulation port direction is invalid")
    known_nodes = range(len(nodes))
    output_names = {
        item["name"] for item in payload["ports"] if item["direction"] == "output"
    }
    if not isinstance(payload["outputs"], list):
        raise SimulationPlanError("simulation output table is invalid")
    for item in payload["outputs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "node"}
            or item.get("name") not in output_names
            or item.get("node") not in known_nodes
        ):
            raise SimulationPlanError("simulation output references an unknown node")
    register_names = {item["name"] for item in payload["registers"]}
    if not isinstance(payload["domains"], list):
        raise SimulationPlanError("simulation clock-domain table is invalid")
    domain_fields = {
        "clock",
        "reset",
        "edge",
        "reset_mode",
        "reset_polarity",
        "reset_release_mode",
        "reset_release_cycles",
    }
    for domain in payload["domains"]:
        if (
            not isinstance(domain, dict)
            or set(domain) != domain_fields
            or not isinstance(domain.get("clock"), str)
            or not domain["clock"]
            or domain.get("edge") not in {"rising", "falling"}
            or domain.get("reset_mode") not in {"synchronous", "asynchronous"}
            or domain.get("reset_polarity") not in {"active_high", "active_low"}
            or domain.get("reset_release_mode") not in {"native", "synchronized"}
            or isinstance(domain.get("reset_release_cycles"), bool)
            or not isinstance(domain.get("reset_release_cycles"), int)
            or domain["reset_release_cycles"] < 0
            or (
                domain.get("reset_release_mode") == "native"
                and domain["reset_release_cycles"] != 0
            )
        ):
            raise SimulationPlanError("simulation clock-domain entry is invalid")
    domain_names = {item.get("clock") for item in payload["domains"]}
    if len(domain_names) != len(payload["domains"]):
        raise SimulationPlanError("simulation clock-domain names are not unique")
    for node in nodes:
        if (
            node["op"] == "load_event"
            and node["attributes"]["name"] not in domain_names
        ):
            raise SimulationPlanError(
                f"primitive event load %{node['id']} names an unknown clock"
            )
    for register in payload["registers"]:
        if (
            not _validate_u64_limbs(register["initial_limbs"], register["width"])
            or register["domain"] not in domain_names
        ):
            raise SimulationPlanError(
                f"simulation register '{register['name']}' metadata is invalid"
            )
    if not isinstance(payload["memories"], list):
        raise SimulationPlanError("simulation memory table is invalid")
    memory_names: set[str] = set()
    memory_fields = {
        "name",
        "width",
        "depth",
        "domain",
        "initial_limbs",
    }
    for memory in payload["memories"]:
        if (
            not isinstance(memory, dict)
            or set(memory) != memory_fields
            or not isinstance(memory.get("name"), str)
            or not memory["name"]
            or memory["name"] in memory_names
            or isinstance(memory.get("depth"), bool)
            or not isinstance(memory.get("depth"), int)
            or memory["depth"] < 1
            or memory["domain"] not in domain_names
        ):
            raise SimulationPlanError("simulation memory entry is invalid")
        memory_names.add(memory["name"])
        if (
            isinstance(memory.get("width"), bool)
            or not isinstance(memory.get("width"), int)
            or not 1 <= memory["width"] <= MAX_PLAN_MEMORY_WIDTH
            or memory["depth"] * memory["width"] > MAX_PLAN_MEMORY_BITS
            or not _validate_u64_limbs(memory["initial_limbs"], memory["width"])
        ):
            raise SimulationPlanError(
                f"simulation memory '{memory['name']}' metadata is invalid"
            )
    events = payload["events"]
    if not isinstance(events, list) or len(events) > MAX_PLAN_EVENTS:
        raise SimulationPlanError("simulation instrumentation event table is invalid")
    for expected, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or set(event) != {"id", "metadata"}
            or event.get("id") != expected
            or not isinstance(event.get("metadata"), dict)
        ):
            raise SimulationPlanError(
                "simulation instrumentation event entry is invalid"
            )
        metadata = event["metadata"]
        category = metadata.get("category")
        expected_metadata = {
            "category",
            "scope_id",
            "scope_name",
            "clause_id",
            "clause_name",
            "hierarchy_path",
            "source_origin",
        }
        if category in {"assertion_failure", "cover_witness"}:
            expected_metadata.add("goal_kind")
        if (
            category
            not in {
                "requirement_violation",
                "assertion_failure",
                "cover_witness",
                "runtime_violation",
            }
            or set(metadata) != expected_metadata
            or any(
                not isinstance(metadata.get(name), str) or not metadata[name]
                for name in ("scope_id", "scope_name", "clause_id", "clause_name")
            )
            or not isinstance(metadata.get("hierarchy_path"), list)
            or not metadata["hierarchy_path"]
            or not all(
                isinstance(item, str) and item
                for item in metadata["hierarchy_path"]
            )
            or (
                "goal_kind" in metadata
                and metadata["goal_kind"] not in {"assert", "ensure", "cover"}
            )
            or (
                metadata.get("source_origin") is not None
                and not isinstance(metadata["source_origin"], dict)
            )
        ):
            raise SimulationPlanError(
                "simulation instrumentation event metadata is invalid"
            )
    _validate_edge_programs(
        payload["edge_programs"],
        nodes,
        domain_names,
        register_names,
        memory_names,
        set(range(len(events))),
    )


def _validate_primitive_node(node: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    op = node["op"]
    operands = node["operands"]
    attrs = node["attributes"]
    arity = {
        "constant": 0,
        "load_input": 0,
        "load_state": 0,
        "load_event": 0,
        "load_memory": 1,
        "not": 1,
        "add": 2,
        "sub": 2,
        "mul": 2,
        "and": 2,
        "or": 2,
        "xor": 2,
        "shl": 2,
        "lshr": 2,
        "ashr": 2,
        "eq": 2,
        "ult": 2,
        "ule": 2,
        "slt": 2,
        "sle": 2,
        "extract_bits": 2,
        "select": 3,
        "insert_bits": 3,
    }
    if op in arity and len(operands) != arity[op]:
        raise SimulationPlanError(f"primitive node %{node['id']} has invalid arity")
    if op in {"load_input", "load_state", "load_event", "load_memory"}:
        key = "memory" if op == "load_memory" else "name"
        if set(attrs) != {key} or not isinstance(attrs[key], str) or not attrs[key]:
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid storage metadata"
            )
    elif op == "constant":
        if set(attrs) != {"limbs"}:
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid constant metadata"
            )
    elif op == "concat_bits":
        widths = attrs.get("operand_widths")
        if (
            set(attrs) != {"operand_widths"}
            or not isinstance(widths, list)
            or len(widths) != len(operands)
            or sum(widths) != node["width"]
        ):
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid concat metadata"
            )
    elif attrs:
        raise SimulationPlanError(
            f"primitive node %{node['id']} has unexpected metadata"
        )
    if op in {"eq", "ult", "ule", "slt", "sle"} and node["width"] != 1:
        raise SimulationPlanError(
            f"primitive comparison node %{node['id']} must be one bit"
        )
    if op in {
        "add", "sub", "mul", "shl", "lshr", "ashr", "ult", "ule", "slt", "sle",
    } and nodes[operands[0]]["width"] > MAX_PLAN_ARITHMETIC_WIDTH:
        raise SimulationPlanError(
            f"primitive node %{node['id']} exceeds the "
            f"{MAX_PLAN_ARITHMETIC_WIDTH}-bit arithmetic bound"
        )
    if op == "select" and nodes[operands[0]]["width"] != 1:
        raise SimulationPlanError(
            f"primitive select node %{node['id']} condition is not one bit"
        )


def _validate_edge_programs(
    programs: object,
    nodes: list[dict[str, Any]],
    domains: set[object],
    registers: set[str],
    memories: set[str],
    events: set[int],
) -> None:
    if not isinstance(programs, list) or len(programs) != len(domains):
        raise SimulationPlanError("primitive edge-program table is invalid")
    seen: set[str] = set()
    for program in programs:
        if (
            not isinstance(program, dict)
            or set(program) != {"clock", "effects", "error", "probes"}
            or program.get("clock") not in domains
            or program["clock"] in seen
            or program.get("error") not in range(len(nodes))
            or not isinstance(program.get("effects"), list)
            or not isinstance(program.get("probes"), list)
        ):
            raise SimulationPlanError("primitive edge-program entry is invalid")
        seen.add(program["clock"])
        for probe in program["probes"]:
            if (
                not isinstance(probe, dict)
                or set(probe) != {"kind", "event", "condition", "once"}
                or probe.get("kind") not in {"check", "cover"}
                or probe.get("event") not in events
                or probe.get("condition") not in range(len(nodes))
                or nodes[probe["condition"]]["width"] != 1
                or not isinstance(probe.get("once"), bool)
                or (probe["kind"] == "check" and probe["once"])
            ):
                raise SimulationPlanError("primitive instrumentation probe is invalid")
        for effect in program["effects"]:
            if not isinstance(effect, dict) or effect.get("op") not in {
                "commit_state",
                "store_memory",
                "fill_memory",
            }:
                raise SimulationPlanError("primitive edge effect is invalid")
            if effect["op"] == "commit_state":
                if (
                    set(effect) != {"op", "target", "node"}
                    or effect.get("target") not in registers
                    or effect.get("node") not in range(len(nodes))
                ):
                    raise SimulationPlanError("primitive state commit is invalid")
            elif effect["op"] == "store_memory":
                if (
                    set(effect) != {"op", "memory", "address", "node", "enable"}
                    or effect.get("memory") not in memories
                    or any(
                        effect.get(key) not in range(len(nodes))
                        for key in ("address", "node", "enable")
                    )
                ):
                    raise SimulationPlanError("primitive memory store is invalid")
            elif (
                set(effect) != {"op", "memory", "node", "enable"}
                or effect.get("memory") not in memories
                or any(
                    effect.get(key) not in range(len(nodes))
                    for key in ("node", "enable")
                )
            ):
                raise SimulationPlanError("primitive memory fill is invalid")


__all__ = [
    "JitUnsupportedFeatureError",
    "CRANELIFT_VERSION",
    "MAX_PLAN_BYTES",
    "MAX_PLAN_NODES",
    "MAX_PLAN_WIDTH",
    "SIMULATION_PLAN_SCHEMA",
    "SIMULATION_RUNTIME_ABI",
    "SimulationPlan",
    "SimulationPlanError",
    "build_simulation_plan",
]
