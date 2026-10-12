# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned verification predicate and ownership semantics."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import cdc as ir_cdc
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.ir import verification as ir_verification
from zlang.ir import interfaces as ir_interfaces
from zlang.ir.traversal import expression_children, walk_expression
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import callables as semantic_callables
from . import expression_domains
from . import expression_coercion
from . import expression_support
from .errors import SemanticError


def _public_observation_owner(
    expression: ir_expr.Expression,
    ports: Mapping[str, ir_module.Port],
    request_responses: Mapping[str, ir_module.RequestResponseInterface],
) -> tuple[str, str] | None:
    """Classify one public leaf as environment, implementation, or mixed."""

    if isinstance(expression, ir_expr.InputRef):
        port = ports.get(expression.name)
        owner = (
            "environment"
            if port is not None
            and port.protocol is ir_interfaces.InterfaceProtocol.WIRE
            and port.direction is ir_module.PortDirection.INPUT
            else "implementation"
        )
        return owner, expression.name
    if isinstance(expression, ir_expr.ReadyValidRef):
        port = ports[expression.interface]
        signal = expression.signal
        if signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            return "mixed", f"{expression.interface}.transfer"
        environment = (
            {ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID}
            if port.direction is ir_module.PortDirection.INPUT
            else {ir_interfaces.ReadyValidSignal.READY}
        )
        return (
            "environment" if signal in environment else "implementation",
            f"{expression.interface}.{signal.value}",
        )
    if isinstance(expression, ir_expr.PacketRef):
        port = ports[expression.interface]
        signal = expression.signal
        if signal is ir_interfaces.PacketSignal.TRANSFER:
            return "mixed", f"{expression.interface}.transfer"
        environment = (
            {ir_interfaces.PacketSignal.PAYLOAD, ir_interfaces.PacketSignal.VALID, ir_interfaces.PacketSignal.LAST}
            if port.direction is ir_module.PortDirection.INPUT
            else {ir_interfaces.PacketSignal.READY}
        )
        return (
            "environment" if signal in environment else "implementation",
            f"{expression.interface}.{signal.value}",
        )
    if isinstance(expression, ir_expr.CreditRef):
        port = ports[expression.interface]
        signal = (
            ir_interfaces.CreditSignal.SEND
            if expression.signal is ir_interfaces.CreditSignal.TRANSFER
            else expression.signal
        )
        environment = (
            {ir_interfaces.CreditSignal.PAYLOAD, ir_interfaces.CreditSignal.SEND}
            if port.direction is ir_module.PortDirection.INPUT
            else {ir_interfaces.CreditSignal.RETURN}
        )
        return (
            "environment" if signal in environment else "implementation",
            f"{expression.interface}.{expression.signal.value}",
        )
    if isinstance(expression, ir_expr.VirtualChannelCreditRef):
        port = ports[expression.interface]
        signal = (
            ir_interfaces.VirtualChannelCreditSignal.SEND
            if expression.signal is ir_interfaces.VirtualChannelCreditSignal.TRANSFER
            else expression.signal
        )
        environment = (
            {
                ir_interfaces.VirtualChannelCreditSignal.PAYLOAD,
                ir_interfaces.VirtualChannelCreditSignal.VC,
                ir_interfaces.VirtualChannelCreditSignal.SEND,
            }
            if port.direction is ir_module.PortDirection.INPUT
            else {
                ir_interfaces.VirtualChannelCreditSignal.RETURN,
                ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
            }
        )
        return (
            "environment" if signal in environment else "implementation",
            f"{expression.interface}.{expression.signal.value}",
        )
    if isinstance(expression, ir_expr.RequestResponseRef):
        interface = request_responses[expression.interface]
        if expression.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            return (
                "mixed",
                f"{expression.interface}.{expression.channel.value}.transfer",
            )
        requester_environment = {
            (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY),
            (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.PAYLOAD),
            (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID),
        }
        responder_environment = {
            (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.PAYLOAD),
            (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID),
            (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY),
        }
        environment = (
            requester_environment
            if interface.role is ir_interfaces.RequestResponseRole.REQUESTER
            else responder_environment
        )
        return (
            "environment"
            if (expression.channel, expression.signal) in environment
            else "implementation",
            f"{expression.interface}.{expression.channel.value}."
            f"{expression.signal.value}",
        )
    return None


class VerificationPredicateValidator:
    """Own the exact contract and same-cycle predicate subsets."""

    def validate_contract(
        self,
        expression: ir_expr.Expression,
        ports: Mapping[str, ir_module.Port],
        contract_name: str,
    ) -> None:
        """Keep the first SVA slice observable and exactly translatable."""

        scalar_types = (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)
        if isinstance(expression, ir_expr.Constant):
            return
        if isinstance(expression, ir_expr.InputRef):
            port = ports.get(expression.name)
            if port is None:
                raise SemanticError(
                    f"contract '{contract_name}' may currently reference ports, not "
                    f"internal signal '{expression.name}'"
                )
            if not isinstance(port.type, scalar_types):
                raise SemanticError(
                    f"contract '{contract_name}' cannot yet reference aggregate port "
                    f"'{port.name}'"
                )
            return
        if isinstance(
            expression,
            (
                ir_expr.ReadyValidRef,
                ir_expr.PacketRef,
                ir_expr.RequestResponseRef,
            ),
        ):
            if not isinstance(expression.type, scalar_types):
                raise SemanticError(
                    f"contract '{contract_name}' cannot yet reference an aggregate "
                    "protocol payload"
                )
            return
        if isinstance(expression, ir_expr.CreditRef):
            if expression.signal is ir_interfaces.CreditSignal.CREDITS:
                raise SemanticError(
                    f"contract '{contract_name}' cannot observe an internal credit "
                    "counter; use protocol events or ports"
                )
            if not isinstance(expression.type, scalar_types):
                raise SemanticError(
                    f"contract '{contract_name}' cannot yet reference an aggregate "
                    "credit payload"
                )
            return
        if isinstance(expression, ir_expr.VirtualChannelCreditRef):
            if expression.signal is ir_interfaces.VirtualChannelCreditSignal.CREDITS:
                raise SemanticError(
                    f"contract '{contract_name}' cannot observe internal per-VC credit "
                    "counters; use protocol events or ports"
                )
            if not isinstance(expression.type, scalar_types):
                raise SemanticError(
                    f"contract '{contract_name}' cannot yet reference an aggregate "
                    "virtual-channel payload"
                )
            return
        if isinstance(
            expression,
            (ir_expr.Extend, ir_expr.Truncate, ir_expr.FixedConvert),
        ):
            for child in expression_children(expression):
                self.validate_contract(child, ports, contract_name)
            return
        if isinstance(
            expression,
            (
                ir_expr.Slice,
                ir_expr.Concat,
                ir_expr.Bitcast,
                ir_expr.VectorConcat,
                ir_expr.Reshape,
                ir_expr.Pack,
                ir_expr.Unpack,
            ),
        ):
            raise SemanticError(
                f"contract '{contract_name}' does not yet support packing expressions "
                "in generated SVA"
            )
        if isinstance(
            expression,
            (ir_expr.Add, ir_expr.Binary, ir_expr.Mux, ir_expr.Switch),
        ):
            for child in expression_children(expression):
                self.validate_contract(child, ports, contract_name)
            return
        if isinstance(expression, ir_expr.RegisterRef):
            raise SemanticError(
                f"contract '{contract_name}' cannot yet bind internal register "
                f"'{expression.name}'; expose an observation port"
            )
        if isinstance(expression, (ir_expr.Delay, ir_expr.Pipeline)):
            raise SemanticError(
                f"contract '{contract_name}' does not support temporal expressions; "
                "only same-cycle invariants are implemented"
            )
        if isinstance(expression, ir_expr.Call):
            raise SemanticError(
                f"contract '{contract_name}' does not yet support function calls"
            )
        if isinstance(
            expression,
            (
                ir_expr.FieldAccess,
                ir_expr.TupleProject,
                ir_expr.VectorIndex,
                ir_expr.InstanceOutputRef,
            ),
        ):
            raise SemanticError(
                f"contract '{contract_name}' does not yet support aggregate access in "
                "generated SVA"
            )
        if isinstance(
            expression,
            (ir_expr.Generate, ir_expr.Map, ir_expr.Dot, ir_expr.Reduce),
        ):
            raise SemanticError(
                f"contract '{contract_name}' does not yet support functional "
                "datapath aggregates in generated SVA"
            )
        if isinstance(
            expression,
            (ir_expr.ParameterRef, ir_expr.FifoRef, ir_expr.MemoryRef, ir_expr.RomRef),
        ):
            raise SemanticError(
                f"contract '{contract_name}' references an internal symbol that cannot "
                "yet be bound in generated SVA"
            )
        raise SemanticError(
            f"contract '{contract_name}' uses unsupported expression {expression!r}"
        )

    def validate_expression(
        self,
        expression: ir_expr.Expression,
        ports: Mapping[str, ir_module.Port],
        clause_name: str,
        *,
        public_only: bool = False,
        allow_aggregate_base: bool = False,
    ) -> None:
        """Validate the bounded same-cycle verification predicate subset."""

        scalar_types = (
            ir_types.BitType,
            ir_types.UIntType,
            ir_types.SIntType,
            ir_types.BitsType,
            ir_types.FixedType,
            ir_types.UFixedType,
            ir_types.EnumType,
        )

        def unsupported(detail: str) -> None:
            raise SemanticError(
                f"verification clause '{clause_name}' {detail}",
                code="ZL-VERIFY-PREDICATE",
                primary=expression.origin,
            )

        if isinstance(expression, ir_expr.Constant):
            if not isinstance(expression.type, scalar_types):
                unsupported(f"cannot use aggregate constant {expression.type}")
            return
        if isinstance(expression, ir_expr.ParameterRef):
            if not isinstance(expression.type, scalar_types):
                unsupported(f"cannot use aggregate parameter {expression.type}")
            return
        if isinstance(expression, ir_expr.InputRef):
            port = ports.get(expression.name)
            if port is None:
                unsupported(f"references unknown signal '{expression.name}'")
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported(
                    f"must project aggregate port '{expression.name}' to a scalar"
                )
            return
        if isinstance(expression, ir_expr.RegisterRef):
            if public_only:
                unsupported(
                    f"ensure cannot depend on hidden register '{expression.name}'"
                )
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported(
                    f"must project aggregate register '{expression.name}' to a scalar"
                )
            return
        if isinstance(
            expression,
            (
                ir_expr.ReadyValidRef,
                ir_expr.PacketRef,
                ir_expr.CreditRef,
                ir_expr.VirtualChannelCreditRef,
                ir_expr.RequestResponseRef,
            ),
        ):
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported("must project an aggregate protocol payload to a scalar")
            return
        if isinstance(expression, ir_expr.FifoRef):
            if public_only:
                unsupported("ensure cannot depend on hidden FIFO state")
            if expression.signal not in {
                ir_storage.FifoSignal.PUSH,
                ir_storage.FifoSignal.POP,
                ir_storage.FifoSignal.FRONT,
                ir_storage.FifoSignal.FULL,
                ir_storage.FifoSignal.EMPTY,
                ir_storage.FifoSignal.READY,
                ir_storage.FifoSignal.VALID,
                ir_storage.FifoSignal.COUNT,
            }:
                unsupported(
                    f"cannot observe unpublished FIFO signal "
                    f"'{expression.fifo}.{expression.signal.value}'"
                )
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported("must project an aggregate FIFO value to a scalar")
            return
        if isinstance(expression, (ir_expr.MemoryRef, ir_expr.RomRef)):
            unsupported("cannot observe memory or ROM contents in this slice")
        if isinstance(expression, ir_expr.InstanceOutputRef):
            unsupported(
                "cannot observe a child instance output without a published "
                "recursive verification binding"
            )
        if isinstance(
            expression,
            (ir_expr.Bitcast, ir_expr.Concat, ir_expr.Pack, ir_expr.Unpack),
        ):
            for child in expression_children(expression):
                self.validate_expression(
                    child,
                    ports,
                    clause_name,
                    public_only=public_only,
                    allow_aggregate_base=True,
                )
            return
        if isinstance(expression, ir_expr.StructConstruct):
            if not allow_aggregate_base:
                unsupported("must project or bitcast a constructed struct value")
            for value in expression_children(expression):
                self.validate_expression(
                    value,
                    ports,
                    clause_name,
                    public_only=public_only,
                    allow_aggregate_base=True,
                )
            return
        if isinstance(expression, ir_expr.TupleConstruct):
            if not allow_aggregate_base:
                unsupported("must project or bitcast a constructed tuple value")
            for value in expression_children(expression):
                self.validate_expression(
                    value,
                    ports,
                    clause_name,
                    public_only=public_only,
                    allow_aggregate_base=True,
                )
            return
        if isinstance(expression, ir_expr.FixedConvert):
            source_fraction = getattr(expression.expression.type, "fraction", 0)
            target_fraction = getattr(expression.type, "fraction", 0)
            if (
                expression.rational_denominator is not None
                or expression.kind is ir_expr.FixedConversionKind.RESCALE
                and source_fraction != target_fraction
            ):
                raise SemanticError(
                    f"verification clause '{clause_name}' cannot use quantized "
                    "fixed-point conversion; compare an already quantized signal "
                    "or use the existing semantic-reference equivalence "
                    "reference-equivalence route",
                    code="ZL-VERIFY-PREDICATE",
                    primary=expression.origin,
                )
            for child in expression_children(expression):
                self.validate_expression(
                    child, ports, clause_name, public_only=public_only
                )
            return
        if isinstance(
            expression,
            (
                ir_expr.Extend,
                ir_expr.Truncate,
                ir_expr.EnumEncode,
                ir_expr.EnumValid,
                ir_expr.EnumDecode,
                ir_expr.Slice,
            ),
        ):
            for child in expression_children(expression):
                self.validate_expression(
                    child, ports, clause_name, public_only=public_only
                )
            return
        if isinstance(
            expression,
            (ir_expr.FieldAccess, ir_expr.TupleProject, ir_expr.VectorIndex),
        ):
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported("must continue projecting the aggregate value to a scalar")
            for child in expression_children(expression):
                self.validate_expression(
                    child,
                    ports,
                    clause_name,
                    public_only=public_only,
                    allow_aggregate_base=True,
                )
            return
        if isinstance(expression, ir_expr.RuntimeIndex):
            if not isinstance(expression.type, scalar_types) and not allow_aggregate_base:
                unsupported(
                    "must continue projecting the runtime-selected value to a scalar"
                )
            self.validate_expression(
                expression.expression,
                ports,
                clause_name,
                public_only=public_only,
                allow_aggregate_base=True,
            )
            self.validate_expression(
                expression.index,
                ports,
                clause_name,
                public_only=public_only,
            )
            return
        if isinstance(
            expression,
            (ir_expr.Add, ir_expr.Binary, ir_expr.Mux, ir_expr.Switch),
        ):
            for child in expression_children(expression):
                self.validate_expression(
                    child, ports, clause_name, public_only=public_only
                )
            return
        if isinstance(expression, (ir_expr.Delay, ir_expr.Pipeline)):
            unsupported(
                "does not support temporal delay/pipeline meaning; only same-cycle "
                "predicates are implemented"
            )
        if isinstance(expression, ir_expr.Call):
            unsupported("contains an unexpanded pure function call")
        if isinstance(
            expression,
            (
                ir_expr.Generate,
                ir_expr.Map,
                ir_expr.FunctionalRegion,
                ir_expr.Dot,
                ir_expr.Reduce,
                ir_expr.VectorUpdate,
                ir_expr.Reshape,
                ir_expr.VectorConcat,
                ir_expr.UnionConstruct,
                ir_expr.UnionTag,
                ir_expr.UnionField,
                ir_expr.FunctionalCaptureRef,
                ir_expr.FunctionalValue,
                ir_expr.FunctionalTableLookup,
                ir_expr.ImplementationChoice,
            ),
        ):
            unsupported(
                f"uses unsupported aggregate/functional expression "
                f"{type(expression).__name__}"
            )
        unsupported(f"uses unsupported expression {expression!r}")

class VerificationOwnershipValidator:
    """Own environment/implementation observation boundaries."""

    def observes_public_output(
        self,
        expression: ir_expr.Expression,
        ports: dict[str, ir_module.Port],
        request_responses: dict[str, ir_module.RequestResponseInterface],
    ) -> bool:
        """Return whether a public expression observes any DUT-owned leaf."""

        return any(
            owner[0] != "environment"
            for value in walk_expression(expression)
            if (
                owner := _public_observation_owner(
                    value, ports, request_responses
                )
            ) is not None
        )

    def validate_assumption_ownership(
        self,
        expression: ir_expr.Expression,
        ports: dict[str, ir_module.Port],
        request_responses: dict[str, ir_module.RequestResponseInterface],
        contract_name: str,
    ) -> None:
        """Reject assumptions that can constrain implementation-owned behavior."""

        def reject(observation: str, *, mixed: bool = False) -> None:
            detail = (
                "combines environment- and implementation-owned signals"
                if mixed
                else "is implementation-owned"
            )
            raise SemanticError(
                f"assumption contract '{contract_name}' cannot reference "
                f"'{observation}': the observation {detail}; assumptions may "
                "constrain environment-owned inputs only"
            )

        supported = (
            ir_expr.Constant,
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.EnumEncode,
            ir_expr.EnumValid,
            ir_expr.EnumDecode,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Pack,
            ir_expr.Unpack,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Add,
            ir_expr.Binary,
            ir_expr.Mux,
            ir_expr.Switch,
        )
        for value in walk_expression(expression):
            owner = _public_observation_owner(value, ports, request_responses)
            if owner is not None:
                if owner[0] != "environment":
                    reject(owner[1], mixed=owner[0] == "mixed")
            elif not isinstance(value, supported):
                raise SemanticError(
                    f"assumption contract '{contract_name}' uses an unsupported "
                    f"ownership expression {value!r}"
                )

class VerificationIdentityFinalizer:
    """Own stable scope, requirement, and goal identities."""

    @staticmethod
    def finalize(module: ir_module.Module) -> ir_module.Module:
        """Assign stable scope, requirement, and goal identities."""

        if not module.verification_scopes:
            return module
        namespace = ir_verification.verification_module_identity(
            replace(module, verification_scopes=())
        )

        def finalized_id(*parts: object) -> str:
            return hashlib.sha256(
                repr((namespace, *parts)).encode("utf-8")
            ).hexdigest()

        finalized_scopes: list[ir_verification.VerificationScope] = []
        for scope in module.verification_scopes:
            scope_id = finalized_id("scope", scope.name, scope.clock)
            finalized_scopes.append(
                replace(
                    scope,
                    semantic_id=scope_id,
                    requirements=tuple(
                        replace(
                            requirement,
                            semantic_id=finalized_id(
                                "scope",
                                scope.name,
                                scope.clock,
                                "requirement",
                                requirement.name,
                            ),
                        )
                        for requirement in scope.requirements
                    ),
                    goals=tuple(
                        replace(
                            goal,
                            semantic_id=finalized_id(
                                "scope",
                                scope.name,
                                scope.clock,
                                goal.kind.value,
                                goal.name,
                            ),
                            scope_id=scope_id,
                        )
                        for goal in scope.goals
                    ),
                )
            )
        return replace(module, verification_scopes=tuple(finalized_scopes))


@dataclass(frozen=True)
class VerificationAnalysisContext:
    """Narrow inputs needed to build compiler-owned verification IR."""

    module: ast.Module
    expression_context: object
    contract_symbols: Mapping[str, Any]
    ports: Mapping[str, ir_module.Port]
    request_responses: Mapping[str, ir_module.RequestResponseInterface]
    register_symbols: Mapping[str, Any]
    locals: Sequence[ir_module.LocalValue]
    clock_domains: tuple[ir_cdc.ClockDomain, ...]
    source_unit: str | None
    source_digest: str | None


@dataclass(frozen=True)
class VerificationAnalysisProduct:
    contracts: tuple[ir_verification.Contract, ...]
    scopes: tuple[ir_verification.VerificationScope, ...]


@dataclass
class _ScopeBuilder:
    semantic_id: str
    name: str
    clock: str
    reset: str
    origin: SourceOrigin | None
    requirements: list[ir_verification.VerificationRequirement] = field(
        default_factory=list
    )
    goals: list[ir_verification.VerificationGoal] = field(default_factory=list)
    names: set[str] = field(default_factory=set)

    def reserve(self, name: str) -> None:
        if name in self.names:
            raise SemanticError(
                f"duplicate verification clause '{name}' in scope '{self.name}'"
            )
        self.names.add(name)

    def freeze(self) -> ir_verification.VerificationScope:
        return ir_verification.VerificationScope(
            self.semantic_id,
            self.name,
            self.clock,
            self.reset,
            tuple(self.requirements),
            tuple(self.goals),
            self.origin,
        )


class _VerificationScopeRepository:
    """Own mutable scope construction and deterministic semantic identities."""

    def __init__(self) -> None:
        self._builders: dict[tuple[str, str], _ScopeBuilder] = {}
        self._order: list[tuple[str, str]] = []

    @staticmethod
    def identity(*parts: object) -> str:
        # SemanticFinalizer replaces every provisional identity after the
        # complete module exists.  Avoid hashing the same graph twice.
        return "provisional:" + "\0".join(str(part) for part in parts)

    def scope(
        self,
        name: str,
        domain: ir_cdc.ClockDomain,
        origin: SourceOrigin | None,
    ) -> _ScopeBuilder:
        key = (name, domain.clock)
        if key not in self._builders:
            self._builders[key] = _ScopeBuilder(
                self.identity("scope", name, domain.clock),
                name,
                domain.clock,
                domain.reset,
                origin,
            )
            self._order.append(key)
        return self._builders[key]

    def add(
        self,
        builder: _ScopeBuilder,
        kind: ir_verification.VerificationGoalKind | None,
        name: str,
        expression: ir_expr.Expression,
        origin: SourceOrigin | None,
    ) -> None:
        builder.reserve(name)
        identity_parts = (
            "scope", builder.name, builder.clock,
            "requirement" if kind is None else kind.value, name,
        )
        if kind is None:
            builder.requirements.append(ir_verification.VerificationRequirement(
                self.identity(*identity_parts), name, expression, origin
            ))
        else:
            builder.goals.append(ir_verification.VerificationGoal(
                self.identity(*identity_parts), builder.semantic_id,
                kind, name, expression, origin,
            ))

    def freeze(self) -> tuple[ir_verification.VerificationScope, ...]:
        return tuple(self._builders[key].freeze() for key in self._order)


class VerificationSemanticAnalyzer:
    """Build contracts and first-class verification scopes in source order."""

    def __init__(self) -> None:
        self._predicates = VerificationPredicateValidator()
        self._ownership = VerificationOwnershipValidator()

    @staticmethod
    def _origin(
        context: VerificationAnalysisContext,
        declaration: object,
        construct: str,
    ) -> SourceOrigin | None:
        span = getattr(declaration, "origin", None)
        if span is None:
            return None
        return SourceOrigin(
            span,
            construct,
            context.source_unit,
            context.source_digest,
        )

    @staticmethod
    def _domain(
        context: VerificationAnalysisContext,
        domains_by_clock: Mapping[str, ir_cdc.ClockDomain],
        name: str,
        requested_clock: str | None,
    ) -> ir_cdc.ClockDomain:
        if requested_clock is None:
            if len(context.clock_domains) != 1:
                raise SemanticError(
                    f"verification declaration '{name}' must name a clock: "
                    "clock inference requires exactly one clock/reset domain"
                )
            return context.clock_domains[0]
        selected = domains_by_clock.get(requested_clock)
        if selected is None:
            raise SemanticError(
                f"verification declaration '{name}' references unknown clock "
                f"'{requested_clock}'"
            )
        return selected

    def _typed_expression(
        self,
        context: VerificationAnalysisContext,
        expression_ast: ast.Expression,
        *,
        name: str,
        clock_name: str,
        ownership: str,
    ) -> ir_expr.Expression:
        expression = self._check_bit(
            context, expression_ast, name, "verification clause"
        )
        expression = expression_support._inline_semantic_locals(
            expression, tuple(context.locals)
        )
        expression = semantic_callables._expand_analysis_calls(
            expression,
            context.expression_context,
            purpose=f"verification clause '{name}'",
        )
        self._predicates.validate_expression(
            expression,
            context.ports,
            name,
            public_only=ownership == "ensure",
        )
        if ownership == "require":
            self._ownership.validate_assumption_ownership(
                expression,
                context.ports,
                context.request_responses,
                name,
            )
            try:
                if (
                    expression_coercion.is_constant_expression(expression)
                    and not bool(constant_runtime_value(expression))
                ):
                    raise SemanticError(
                        f"verification requirement '{name}' is compile-time false"
                    )
            except ConstantExpressionError:
                pass
        if ownership == "ensure" and not self._ownership.observes_public_output(
            expression,
            context.ports,
            context.request_responses,
        ):
            raise SemanticError(
                f"verification ensure '{name}' must observe at least one "
                "implementation-owned public output"
            )
        self._validate_domain(context, expression, name, clock_name, "clause")
        return expression

    @staticmethod
    def _check_bit(
        context: VerificationAnalysisContext,
        expression_ast: ast.Expression,
        name: str,
        construct: str,
    ) -> ir_expr.Expression:
        expression = context.expression_context.expressions.check(
            expression_ast, context.contract_symbols, ir_types.BitType(),
            context.expression_context,
        )
        if expression.type != ir_types.BitType():
            raise SemanticError(
                f"{construct} '{name}' expression must be bit, got "
                f"{expression.type}"
            )
        return expression

    @staticmethod
    def _validate_domain(
        context: VerificationAnalysisContext,
        expression: ir_expr.Expression,
        name: str,
        clock: str,
        label: str,
    ) -> None:
        domains = {
            item for item in expression_domains.expression_domains(
                expression, context.ports, context.register_symbols
            ) if item is not None
        }
        mismatched = sorted(domains - {clock})
        if mismatched:
            raise SemanticError(
                f"{label} '{name}' clocked by '{clock}' references signal in "
                f"domain '{mismatched[0]}'"
            )

    def _contracts(
        self,
        context: VerificationAnalysisContext,
        domains_by_clock: Mapping[str, ir_cdc.ClockDomain],
    ) -> tuple[ir_verification.Contract, ...]:
        contracts: list[ir_verification.Contract] = []
        names: set[str] = set()
        for declaration in context.module.contracts:
            if declaration.name in names:
                raise SemanticError(f"duplicate contract '{declaration.name}'")
            names.add(declaration.name)
            domain = domains_by_clock.get(declaration.clock)
            if domain is None:
                raise SemanticError(
                    f"contract '{declaration.name}' references unknown clock "
                    f"'{declaration.clock}'"
                )
            if declaration.reset != domain.reset:
                raise SemanticError(
                    f"contract '{declaration.name}' must use reset '{domain.reset}' "
                    f"for clock '{declaration.clock}'"
                )
            expression = self._check_bit(
                context, declaration.expression, declaration.name, "contract"
            )
            self._predicates.validate_contract(
                expression, context.ports, declaration.name
            )
            if declaration.kind.value == "assume":
                self._ownership.validate_assumption_ownership(
                    expression,
                    context.ports,
                    context.request_responses,
                    declaration.name,
                )
            self._validate_domain(
                context,
                expression,
                declaration.name,
                declaration.clock,
                "contract",
            )
            contracts.append(ir_verification.Contract(
                ir_verification.ContractKind(declaration.kind.value),
                declaration.name,
                declaration.clock,
                declaration.reset,
                expression,
            ))
        return tuple(contracts)

    def analyze(
        self, context: VerificationAnalysisContext
    ) -> VerificationAnalysisProduct:
        domains_by_clock = {domain.clock: domain for domain in context.clock_domains}
        contracts = self._contracts(context, domains_by_clock)
        scopes = _VerificationScopeRepository()

        for declaration, contract in zip(
            context.module.contracts, contracts, strict=True
        ):
            domain = domains_by_clock[contract.clock]
            builder = scopes.scope("$module", domain, None)
            if contract.kind is ir_verification.ContractKind.ASSUME:
                scopes.add(
                    builder,
                    None,
                    contract.name,
                    contract.expression,
                    self._origin(
                        context, declaration, f"assume {contract.name}"
                    ),
                )
            else:
                scopes.add(
                    builder,
                    ir_verification.VerificationGoalKind.ASSERT,
                    contract.name,
                    contract.expression,
                    self._origin(
                        context, declaration, f"guarantee {contract.name}"
                    ),
                )

        for declaration in context.module.verification_goals:
            domain = self._domain(
                context, domains_by_clock, declaration.name, declaration.clock
            )
            builder = scopes.scope("$module", domain, None)
            expression = self._typed_expression(
                context,
                declaration.expression,
                name=declaration.name,
                clock_name=domain.clock,
                ownership="assert",
            )
            kind = ir_verification.VerificationGoalKind(declaration.kind.value)
            scopes.add(
                builder,
                kind,
                declaration.name,
                expression,
                self._origin(
                    context, declaration, f"{kind.value} {declaration.name}"
                ),
            )

        explicit_scope_names: set[str] = set()
        for declaration in context.module.verification_scopes:
            if declaration.name in explicit_scope_names:
                raise SemanticError(
                    f"duplicate verification contract '{declaration.name}'"
                )
            explicit_scope_names.add(declaration.name)
            if not declaration.goals:
                raise SemanticError(
                    f"verification contract '{declaration.name}' must contain at "
                    "least one assert, ensure, or cover goal"
                )
            domain = self._domain(
                context, domains_by_clock, declaration.name, declaration.clock
            )
            builder = scopes.scope(
                declaration.name,
                domain,
                self._origin(
                    context, declaration, f"contract {declaration.name}"
                ),
            )
            for requirement in declaration.requirements:
                scopes.add(
                    builder,
                    None,
                    requirement.name,
                    self._typed_expression(
                        context,
                        requirement.expression,
                        name=requirement.name,
                        clock_name=domain.clock,
                        ownership="require",
                    ),
                    self._origin(
                        context, requirement, f"require {requirement.name}"
                    ),
                )
            for goal in declaration.goals:
                kind = ir_verification.VerificationGoalKind(goal.kind.value)
                scopes.add(
                    builder,
                    kind,
                    goal.name,
                    self._typed_expression(
                        context,
                        goal.expression,
                        name=goal.name,
                        clock_name=domain.clock,
                        ownership=(
                            "ensure"
                            if kind is ir_verification.VerificationGoalKind.ENSURE
                            else "assert"
                        ),
                    ),
                    self._origin(context, goal, f"{kind.value} {goal.name}"),
                )

        return VerificationAnalysisProduct(contracts, scopes.freeze())
