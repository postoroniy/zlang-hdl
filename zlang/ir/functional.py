"""Backend-independent lowering for typed functional datapath nodes."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Iterable

from zlang.ir import expressions as expr
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    CompileTimeExpr,
    FunctionalRegionKind,
    FunctionalTable,
    evaluate_compile_time,
)
from zlang.ir.numeric import (
    NumericTypeError,
    NumericTypeErrorReason,
    addition_rule,
    bitwise_rule,
    multiplication_rule,
)
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.traversal import expression_children as typed_expression_children
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
    VecType,
)


class FunctionalLoweringError(ValueError):
    """A typed functional node cannot be lowered to scalar expression IR."""


_MAX_MATERIALIZED_FUNCTIONAL_ELEMENTS = 65_536


def materialize_functional_region(
    region: expr.FunctionalRegion,
    *,
    max_elements: int = _MAX_MATERIALIZED_FUNCTIONAL_ELEMENTS,
) -> tuple[expr.Expression, ...]:
    """Instantiate one typed functional template for final backend lowering.

    This is intentionally not part of :func:`collection_elements`: semantic,
    optimization, M32, and e-graph consumers continue to observe one compact
    region.  Concrete backends may call this bounded helper at their final
    expression-emission boundary.
    """

    if isinstance(max_elements, bool) or not isinstance(max_elements, int):
        raise FunctionalLoweringError(
            "functional materialization limit must be an integer"
        )
    if max_elements < 1:
        raise FunctionalLoweringError(
            "functional materialization limit must be positive"
        )
    length = region.binder.stop - region.binder.start
    if length > max_elements:
        raise FunctionalLoweringError(
            f"functional region contains {length} elements, exceeding backend "
            f"materialization limit {max_elements}"
        )
    captures = {reference.identity: value for reference, value in region.captures}
    tables = {table.name: table for table in region.tables}
    return tuple(
        _instantiate_functional_value(
            region.template,
            binder_values={region.binder.identity: index},
            captures=captures,
            tables=tables,
        )
        for index in range(region.binder.start, region.binder.stop)
    )


def _instantiate_functional_value(
    value: object,
    *,
    binder_values: dict[str, int],
    captures: dict[str, expr.Expression],
    tables: dict[str, FunctionalTable],
) -> object:
    """Substitute one region-owned binder/table/capture environment."""

    if isinstance(value, expr.FunctionalCaptureRef):
        capture = captures.get(value.identity)
        if capture is None or capture.type != value.type:
            raise FunctionalLoweringError(
                f"functional capture '{value.display_name}' is not bound exactly"
            )
        return capture
    if isinstance(value, expr.FunctionalTableLookup):
        table = tables.get(value.table_name)
        if table is None or table.type != value.type:
            raise FunctionalLoweringError(
                f"functional table '{value.table_name}' is not bound exactly"
            )
        try:
            index = evaluate_compile_time(value.index, binder_values)
        except ValueError as error:
            raise FunctionalLoweringError(str(error)) from error
        offset = index - table.start
        if not 0 <= offset < len(table.values):
            raise FunctionalLoweringError(
                f"functional table index {index} is outside "
                f"{table.start}..{table.stop}"
            )
        selected = table.values[offset]
        return _instantiate_functional_value(
            selected,
            binder_values=binder_values,
            captures=captures,
            tables=tables,
        )
    if isinstance(value, expr.VectorIndex):
        expression = _instantiate_functional_value(
            value.expression,
            binder_values=binder_values,
            captures=captures,
            tables=tables,
        )
        assert isinstance(expression, expr.Expression)
        try:
            index = (
                value.index
                if isinstance(value.index, int)
                else evaluate_compile_time(value.index, binder_values)
            )
        except ValueError as error:
            raise FunctionalLoweringError(str(error)) from error
        return replace(value, expression=expression, index=index)
    if isinstance(value, expr.FunctionalRegion):
        raise FunctionalLoweringError(
            "nested functional regions require an explicit outer lowering boundary"
        )
    if isinstance(value, tuple):
        return tuple(
            _instantiate_functional_value(
                item,
                binder_values=binder_values,
                captures=captures,
                tables=tables,
            )
            for item in value
        )
    if is_dataclass(value) and not isinstance(value, type):
        updates: dict[str, object] = {}
        for item in fields(value):
            if item.name in {"type", "origin"} or not item.init:
                continue
            updates[item.name] = _instantiate_functional_value(
                getattr(value, item.name),
                binder_values=binder_values,
                captures=captures,
                tables=tables,
            )
        if not updates:
            return value
        try:
            return replace(value, **updates)
        except (TypeError, ValueError) as error:
            raise FunctionalLoweringError(
                f"cannot instantiate functional {type(value).__name__}: {error}"
            ) from error
    return value


def vector_leaf_shape(type_: HardwareType) -> tuple[int, HardwareType]:
    """Return outer-to-inner leaf count/type for a nested vector type.

    This operation describes collection shape only.  It deliberately does not
    consult bit packing, widths, or representation order.
    """

    if not isinstance(type_, VecType):
        raise FunctionalLoweringError(f"reshape requires a vector type, got {type_}")
    count = type_.length
    leaf = type_.element_type
    while isinstance(leaf, VecType):
        count *= leaf.length
        leaf = leaf.element_type
    return count, leaf


def collection_elements(collection: expr.Expression) -> tuple[expr.Expression, ...]:
    """Return the statically known elements of one typed vector expression."""

    if isinstance(collection, (expr.Generate, expr.Map)):
        return collection.elements
    if isinstance(collection, expr.Dot):
        return collection.products
    if isinstance(collection, expr.FunctionalRegion):
        raise FunctionalLoweringError(
            "compact functional regions require a lazy consumer or exact reduction plan"
        )
    if not isinstance(collection.type, VecType):
        raise FunctionalLoweringError(
            f"reduction requires a vector collection, got {collection.type}"
        )
    return tuple(
        expr.VectorIndex(collection, index, collection.type.element_type)
        for index in range(collection.type.length)
    )


def compact_functional_elements(
    kind: FunctionalRegionKind,
    binder: CompileTimeBinderRef,
    elements: tuple[expr.Expression, ...],
    result_type: HardwareType,
    definitions: Iterable[object] = (),
) -> tuple[expr.FunctionalRegion, tuple[str, ...]] | None:
    """Anti-unify one eager homogeneous tuple into a bounded typed template.

    The accepted subset is intentionally structural: static vector indices may
    follow the binder, varying constants become immutable tables, zero-argument
    constant callables are inlined, and ordinary expression/call structure must
    otherwise agree exactly.  Any unsafe mismatch returns ``None``.
    """

    if not isinstance(result_type, VecType):
        return None
    if not isinstance(kind, FunctionalRegionKind):
        try:
            kind = FunctionalRegionKind(kind)
        except (TypeError, ValueError):
            return None
    if result_type.length != binder.stop - binder.start:
        return None
    if len(elements) != result_type.length or not elements:
        return None
    if any(element.type != result_type.element_type for element in elements):
        return None
    ordered_definitions = tuple(definitions)
    by_identity = {
        str(getattr(definition, "callee_identity", "")): definition
        for definition in ordered_definitions
        if getattr(definition, "callee_identity", "")
    }
    by_name: dict[str, list[object]] = {}
    for definition in ordered_definitions:
        by_name.setdefault(str(getattr(definition, "name", "")), []).append(definition)
    tables: list[FunctionalTable] = []
    captures: list[tuple[expr.FunctionalCaptureRef, expr.Expression]] = []
    inlined: list[str] = []

    def resolve(call: expr.Call) -> object | None:
        if call.callee_identity is not None:
            definition = by_identity.get(call.callee_identity)
            if definition is None or getattr(definition, "name", None) != call.function:
                return None
            return definition
        candidates = by_name.get(call.function, ())
        return candidates[0] if len(candidates) == 1 else None

    def constant_callable(
        value: expr.Expression,
        stack: tuple[str, ...] = (),
    ) -> bool:
        if isinstance(value, expr.Constant):
            return True
        if isinstance(
            value,
            (
                expr.InputRef,
                expr.ParameterRef,
                expr.RegisterRef,
                expr.ReadyValidRef,
                expr.CreditRef,
                expr.PacketRef,
                expr.VirtualChannelCreditRef,
                expr.RequestResponseRef,
                expr.FifoRef,
                expr.MemoryRef,
                expr.RomRef,
                expr.InstanceOutputRef,
                expr.FunctionalCaptureRef,
                expr.FunctionalTableLookup,
                expr.Delay,
                expr.Pipeline,
                expr.ImplementationChoice,
            ),
        ):
            return False
        if isinstance(value, expr.Call):
            definition = resolve(value)
            if definition is None or value.arguments or getattr(definition, "parameters"):
                return False
            identity = str(getattr(definition, "callee_identity"))
            if identity in stack:
                return False
            return constant_callable(
                getattr(definition, "body"), (*stack, identity)
            )
        if isinstance(value, expr.FunctionalRegion):
            return False
        for child in _expression_children(value):
            if not constant_callable(child, stack):
                return False
        return True

    def capture(value: expr.Expression) -> expr.FunctionalCaptureRef:
        for reference, existing in captures:
            if existing == value:
                return reference
        ordinal = len(captures)
        reference = expr.FunctionalCaptureRef(
            f"{binder.identity}:capture:{ordinal}",
            f"{binder.display_name}_capture_{ordinal}",
            value.type,
        )
        captures.append((reference, value))
        return reference

    def table(values: tuple[expr.Constant, ...]) -> expr.FunctionalTableLookup:
        ordinal = len(tables)
        name = f"{binder.identity}:table:{ordinal}"
        tables.append(FunctionalTable(name, values, values[0].type, binder.start))
        value_range = (
            expr.ValueRange(
                min(value.value for value in values),
                max(value.value for value in values),
                "constant_table",
            )
            if isinstance(values[0].type, (UIntType, BitsType))
            else None
        )
        return expr.FunctionalTableLookup(
            name,
            CompileTimeExpr.ref(binder),
            values[0].type,
            value_range,
        )

    def unify(
        values: tuple[expr.Expression, ...],
        stack: tuple[str, ...] = (),
    ) -> expr.Expression | None:
        first = values[0]
        if any(type(value) is not type(first) or value.type != first.type for value in values):
            return None
        if any(not _pure_functional_expression(value) for value in values):
            return None
        if isinstance(first, expr.Constant):
            constants = tuple(value for value in values if isinstance(value, expr.Constant))
            if len(constants) != len(values):
                return None
            if all(value == first for value in constants[1:]):
                return first
            return table(constants)
        if isinstance(first, expr.VectorIndex):
            indices = tuple(value.index for value in values if isinstance(value, expr.VectorIndex))
            expected = tuple(range(binder.start, binder.stop))
            if indices == expected:
                collections = tuple(
                    value.expression for value in values if isinstance(value, expr.VectorIndex)
                )
                collection = unify(collections, stack)
                if collection is None:
                    return None
                return expr.VectorIndex(
                    collection,
                    CompileTimeExpr.ref(binder),
                    first.type,
                )
            if all(value == first for value in values[1:]):
                return capture(first)
            return None
        if isinstance(first, expr.Call):
            calls = tuple(value for value in values if isinstance(value, expr.Call))
            if len(calls) != len(values):
                return None
            if all(not call.arguments for call in calls):
                definitions_for_calls = tuple(resolve(call) for call in calls)
                if any(definition is None for definition in definitions_for_calls):
                    return None
                concrete = tuple(definition for definition in definitions_for_calls if definition is not None)
                if any(getattr(definition, "parameters") for definition in concrete):
                    return None
                identities = tuple(str(getattr(definition, "callee_identity")) for definition in concrete)
                if any(identity in stack for identity in identities):
                    return None
                bodies = tuple(getattr(definition, "body") for definition in concrete)
                if any(not constant_callable(body, (*stack, identity)) for body, identity in zip(bodies, identities, strict=True)):
                    return None
                # Retain occurrence multiplicity for calls physically present
                # in the original elements (for example OUT=0 repeats one
                # twiddle 64 times). Calls reached only by opening a shared
                # definition body are retained once in that definition, not
                # once per virtual expansion; semantic reachability releases
                # those edges when the owning definition itself is removed.
                if not stack:
                    inlined.extend(identities)
                return unify(bodies, (*stack, *identities))
            signature = (first.function, first.callee_identity, len(first.arguments))
            if any(
                (call.function, call.callee_identity, len(call.arguments)) != signature
                for call in calls[1:]
            ):
                return None
            arguments: list[expr.Expression] = []
            for index in range(len(first.arguments)):
                argument = unify(tuple(call.arguments[index] for call in calls), stack)
                if argument is None:
                    return None
                arguments.append(argument)
            return replace(first, arguments=tuple(arguments))
        if isinstance(first, expr.StructConstruct):
            structs = tuple(value for value in values if isinstance(value, expr.StructConstruct))
            field_names = tuple(name for name, _ in first.fields)
            if any(
                value.struct_name != first.struct_name
                or tuple(name for name, _ in value.fields) != field_names
                for value in structs[1:]
            ):
                return None
            result_fields: list[tuple[str, expr.Expression]] = []
            for index, name in enumerate(field_names):
                field = unify(
                    tuple(value.fields[index][1] for value in structs), stack
                )
                if field is None:
                    return None
                result_fields.append((name, field))
            return replace(first, fields=tuple(result_fields))
        if all(value == first for value in values[1:]):
            return capture(first)
        if not is_dataclass(first):
            return None
        updates: dict[str, object] = {}
        for item in fields(first):
            if item.name in {"type", "origin"} or not item.init:
                continue
            corresponding = tuple(getattr(value, item.name) for value in values)
            if all(isinstance(value, expr.Expression) for value in corresponding):
                child = unify(corresponding, stack)  # type: ignore[arg-type]
                if child is None:
                    return None
                updates[item.name] = child
                continue
            if all(
                isinstance(value, tuple)
                and len(value) == len(corresponding[0])
                and all(isinstance(child, expr.Expression) for child in value)
                for value in corresponding
            ):
                children: list[expr.Expression] = []
                for index in range(len(corresponding[0])):
                    child = unify(
                        tuple(value[index] for value in corresponding), stack
                    )
                    if child is None:
                        return None
                    children.append(child)
                updates[item.name] = tuple(children)
                continue
            if any(value != corresponding[0] for value in corresponding[1:]):
                return None
        try:
            return replace(first, **updates)
        except (TypeError, ValueError):
            return None

    template = unify(elements)
    if template is None:
        return None
    try:
        region = expr.FunctionalRegion(
            kind,
            binder,
            template,
            tuple(tables),
            tuple(captures),
            result_type,
        )
    except ValueError:
        return None
    return region, tuple(inlined)


def _expression_children(value: expr.Expression) -> tuple[expr.Expression, ...]:
    return typed_expression_children(value)


def _pure_functional_expression(value: expr.Expression) -> bool:
    # A ready/valid payload is an ordinary current-cycle combinational value.
    # Capturing it does not give a compact functional region ownership of the
    # handshake: valid, ready, and transfer remain protocol observations and
    # therefore stay outside this pure value-only representation.
    if isinstance(value, expr.ReadyValidRef):
        return value.signal is ReadyValidSignal.PAYLOAD
    if isinstance(
        value,
        (
            expr.RegisterRef,
            expr.CreditRef,
            expr.PacketRef,
            expr.VirtualChannelCreditRef,
            expr.RequestResponseRef,
            expr.FifoRef,
            expr.MemoryRef,
            expr.RomRef,
            expr.Delay,
            expr.Pipeline,
            expr.ImplementationChoice,
        ),
    ):
        return False
    return all(_pure_functional_expression(child) for child in _expression_children(value))


def lower_reduction(reduction: expr.Reduce) -> expr.Expression:
    """Materialize the specified source-order balanced scalar reduction tree."""

    if reduction.plan is not None:
        return materialize_exact_reduction(reduction)
    # A compact functional collection remains opaque to semantic optimization,
    # M32, and the e-graph, but an ordinary built-in scalar reduction still
    # needs the same executable tree it had before compaction.  Reconstruct the
    # bounded leaves only at this final lowering boundary; do not teach
    # ``collection_elements`` to expose the compact region globally.
    elements = (
        materialize_functional_region(reduction.collection)
        if isinstance(reduction.collection, expr.FunctionalRegion)
        else collection_elements(reduction.collection)
    )
    if not elements:
        raise FunctionalLoweringError("cannot lower an empty reduction")
    lowered = (
        reduction.expanded
        if reduction.expanded is not None
        else _balanced(reduction.operator, elements)
    )
    if lowered.type != reduction.type:
        raise FunctionalLoweringError(
            f"reduction lowered to {lowered.type}, expected {reduction.type}"
        )
    return lowered


def materialize_exact_reduction(reduction: expr.Reduce) -> expr.Expression:
    """Reconstruct one exact typed reduction plan without reassociation.

    Every plan level is replayed in its frozen order.  Nominal operations
    become identity-bearing :class:`Call` nodes; built-in additions become
    ordinary exact-width :class:`Add` nodes.  This helper is for concrete
    execution/backend lowering, not optimization discovery.
    """

    plan = reduction.plan
    if plan is None:
        raise FunctionalLoweringError(
            "exact reduction materialization requires a reduction plan"
        )
    elements = (
        materialize_functional_region(reduction.collection)
        if isinstance(reduction.collection, expr.FunctionalRegion)
        else collection_elements(reduction.collection)
    )
    if len(elements) != plan.length:
        raise FunctionalLoweringError(
            f"exact reduction expected {plan.length} leaves, got {len(elements)}"
        )
    if any(element.type != plan.leaf_type for element in elements):
        raise FunctionalLoweringError(
            "exact reduction leaf type does not match its frozen plan"
        )

    current = elements
    for level in plan.levels:
        if tuple(value.type for value in current) != level.input_types:
            raise FunctionalLoweringError(
                "exact reduction level input types do not match its frozen plan"
            )
        by_left = {operation.left_index: operation for operation in level.operations}
        next_values: list[expr.Expression] = []
        index = 0
        while index < len(current):
            operation = by_left.get(index)
            if operation is None:
                next_values.append(current[index])
                index += 1
                continue
            left = current[index]
            right = current[index + 1]
            if (
                left.type != operation.left_type
                or right.type != operation.right_type
            ):
                raise FunctionalLoweringError(
                    "exact reduction operation operand types do not match"
                )
            if operation.function is None:
                combined: expr.Expression = expr.Add(
                    left,
                    right,
                    operation.result_type,
                    origin=reduction.origin,
                )
            else:
                assert operation.callee_identity is not None
                combined = expr.Call(
                    operation.function,
                    (left, right),
                    operation.result_type,
                    operation.callee_identity,
                    origin=reduction.origin,
                )
            next_values.append(combined)
            index += 2
        current = tuple(next_values)
    if len(current) != 1 or current[0].type != plan.root_type:
        raise FunctionalLoweringError(
            "exact reduction plan did not produce its declared root type"
        )
    if current[0].type != reduction.type:
        raise FunctionalLoweringError(
            f"reduction lowered to {current[0].type}, expected {reduction.type}"
        )
    return current[0]


def reduction_result_type(
    operator: expr.ReductionOperator,
    element_type: HardwareType,
    length: int,
) -> HardwareType:
    """Derive a reduction type using the same balanced tree as backend lowering."""

    if length < 1:
        raise FunctionalLoweringError("reduction length must be positive")
    leaves = tuple(_type_leaf(element_type) for _ in range(length))
    return _balanced(operator, leaves).type


def _type_leaf(type_: HardwareType) -> expr.Constant:
    if not isinstance(type_, (BitType, UIntType, SIntType, BitsType, FixedType, UFixedType)):
        raise FunctionalLoweringError(
            f"reduction is not defined for element type {type_}"
        )
    return expr.Constant(0, type_)


def _balanced(
    operator: expr.ReductionOperator,
    elements: tuple[expr.Expression, ...],
) -> expr.Expression:
    if len(elements) == 1:
        return elements[0]
    middle = len(elements) // 2
    left = _balanced(operator, elements[:middle])
    right = _balanced(operator, elements[middle:])
    return _combine(operator, left, right)


def _combine(
    operator: expr.ReductionOperator,
    left: expr.Expression,
    right: expr.Expression,
) -> expr.Expression:
    if operator is expr.ReductionOperator.ADD:
        try:
            result_type = addition_rule(left.type, right.type).result_type
        except NumericTypeError as error:
            if error.reason is NumericTypeErrorReason.FRACTION_MISMATCH:
                message = (
                    "built-in exact fixed-point addition requires identical "
                    "fractional widths"
                )
            else:
                message = (
                    "built-in exact addition requires one integer or fixed-point "
                    f"signedness family, got {left.type} and {right.type}"
                )
            raise FunctionalLoweringError(message) from error
        return expr.Add(left, right, result_type)

    binary_operator = {
        expr.ReductionOperator.MULTIPLY: expr.BinaryOperator.MULTIPLY,
        expr.ReductionOperator.BIT_AND: expr.BinaryOperator.BIT_AND,
        expr.ReductionOperator.BIT_OR: expr.BinaryOperator.BIT_OR,
        expr.ReductionOperator.BIT_XOR: expr.BinaryOperator.BIT_XOR,
    }[operator]
    if operator is expr.ReductionOperator.MULTIPLY:
        try:
            result_type = multiplication_rule(left.type, right.type).result_type
        except NumericTypeError as error:
            raise FunctionalLoweringError(
                "multiplication reduction requires one integer signedness family, "
                f"got {left.type} and {right.type}"
            ) from error
        return expr.Binary(binary_operator, left, right, result_type, result_type)

    try:
        result_type = bitwise_rule(left.type, right.type).result_type
    except NumericTypeError as error:
        if error.reason is NumericTypeErrorReason.FAMILY_MISMATCH:
            message = (
                "bitwise reduction requires matching type families, "
                f"got {left.type} and {right.type}"
            )
        else:
            message = f"bitwise reduction is not defined for {left.type}"
        raise FunctionalLoweringError(message) from error
    return expr.Binary(binary_operator, left, right, result_type, result_type)
