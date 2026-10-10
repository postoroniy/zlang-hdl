# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by ExpressionRules."""

from __future__ import annotations

from fractions import Fraction
import re

from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError
from zlang.source import SourceSpan

from .rules_support import _cost_metric


class ExpressionRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    @v_args(meta=True)
    def removed_pipeline_auto_expr(
        self, _meta: object, _items: list[object]
    ) -> object:
        raise ParseError(
            "scalar pipeline(auto) was removed; use implement { expression "
            "intent { ... } } for compiler-selected implementation, or "
            "pipeline(N) for exact latency"
        )

    @v_args(meta=True)
    def removed_architecture_expr(
        self, _meta: object, _items: list[object]
    ) -> object:
        raise ParseError(
            "scalar architecture(auto) was removed; use implement { expression "
            "intent { ... } } for compiler-selected implementation"
        )

    @v_args(meta=True)
    def removed_explore_expr(
        self, _meta: object, _items: list[object]
    ) -> object:
        raise ParseError(
            "scalar explore was removed; use implement { expression intent "
            "{ ... } } for compiler-selected implementation"
        )

    @v_args(meta=True)
    def name_expr(self, meta: object, items: list[object]) -> ast_nodes.NameExpr:
        return ast_nodes.NameExpr(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_builtin(self, meta: object, items: list[object]) -> ast_nodes.TypeValueExpr:
        return ast_nodes.TypeValueExpr(ast_nodes.TypeName(str(items[0])), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_generic(self, meta: object, items: list[object]) -> ast_nodes.TypeValueExpr:
        return ast_nodes.TypeValueExpr(ast_nodes.TypeName(str(items[0])), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_vec(self, meta: object, items: list[object]) -> ast_nodes.TypeValueExpr:
        text = str(items[0])
        match = re.fullmatch(r"vec<([0-9]+),(.+)>", text)
        if match is None:
            raise ParseError(f"invalid vector type value '{text}'")
        return ast_nodes.TypeValueExpr(
            ast_nodes.VectorTypeName(int(match.group(1)), ast_nodes.TypeName(match.group(2))),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def type_value_ref(self, meta: object, items: list[object]) -> ast_nodes.TypeValueExpr:
        syntax = items[0] if isinstance(items[0], (ast_nodes.TypeName, ast_nodes.VectorTypeName, ast_nodes.TupleTypeName)) else ast_nodes.TypeName(str(items[0]))
        return ast_nodes.TypeValueExpr(syntax, origin=self._span(meta))

    @v_args(meta=True)
    def number_expr(self, meta: object, items: list[object]) -> ast_nodes.NumberExpr:
        return ast_nodes.NumberExpr(self._parse_number(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def rational_expr(self, meta: object, items: list[object]) -> ast_nodes.RationalExpr:
        value = Fraction(str(items[0]).replace("_", ""))
        return ast_nodes.RationalExpr(value.numerator, value.denominator, origin=self._span(meta))

    def argument_list(self, items: list[object]) -> tuple[object, ...]:
        return tuple(items)

    def named_call_specialization_argument(self, items: list[object]) -> ast_nodes.SpecializationArgument:
        return ast_nodes.SpecializationArgument(str(items[0]), items[1])

    def positional_call_specialization_argument(self, items: list[object]) -> ast_nodes.SpecializationArgument:
        return ast_nodes.SpecializationArgument(None, items[0])

    def call_specialization_arguments(self, items: list[object]) -> tuple[ast_nodes.SpecializationArgument, ...]:
        return tuple(items)

    def callable_ref_specializations(self, items: list[object]) -> tuple[ast_nodes.SpecializationArgument, ...]:
        return tuple(items)

    def call_parameter_number(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def callable_specialization_ref(self, items: list[object]) -> ast_nodes.CallableRef:
        specializations = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)
            ),
            (),
        )
        return ast_nodes.CallableRef(str(items[0]), specializations)

    @v_args(meta=True)
    def function_call(self, meta: object, items: list[object]) -> ast_nodes.CallExpr:
        specializations = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)
            ),
            (),
        )
        arguments = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and not all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)
            ),
            (),
        )
        return ast_nodes.CallExpr(
            str(items[0]),
            arguments,
            specializations,
            origin=self._span(meta),
            callee_origin=self._token_span(items[0]),
        )

    @v_args(meta=True)
    def qualified_function_call(
        self, meta: object, items: list[object]
    ) -> ast_nodes.CallExpr:
        return self.function_call(meta, items)

    @v_args(meta=True)
    def unary_negate_expr(self, meta: object, items: list[object]) -> ast_nodes.UnaryExpr:
        return ast_nodes.UnaryExpr(ast_nodes.BinaryOperator.SUBTRACT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unary_not_expr(self, meta: object, items: list[object]) -> ast_nodes.UnaryExpr:
        return ast_nodes.UnaryExpr(ast_nodes.BinaryOperator.LOGIC_NOT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unary_bit_not_expr(self, meta: object, items: list[object]) -> ast_nodes.UnaryExpr:
        return ast_nodes.UnaryExpr(ast_nodes.BinaryOperator.BIT_NOT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def field_expr(self, meta: object, items: list[object]) -> ast_nodes.FieldExpr:
        member = str(items[1])
        member_origin = self._token_span(items[1]) or self._name_span_from_end_meta(
            meta, member
        )
        expression = items[0]
        return ast_nodes.FieldExpr(
            expression,
            member,
            origin=self._span(meta),
            member_origin=member_origin,
        )

    @v_args(meta=True)
    def index_expr(self, meta: object, items: list[object]) -> ast_nodes.IndexExpr:
        index = items[1]
        if isinstance(index, ast_nodes.NumberExpr):
            index = index.value
        return ast_nodes.IndexExpr(items[0], index, origin=self._span(meta))

    @v_args(meta=True)
    def slice_expr(self, meta: object, items: list[object]) -> ast_nodes.SliceExpr:
        bounds = [self._slice_bound(item) for item in items[1:]]
        return ast_nodes.SliceExpr(items[0], bounds[0], bounds[1], origin=self._span(meta))

    @v_args(meta=True)
    def dynamic_slice_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.DynamicSliceExpr:
        return ast_nodes.DynamicSliceExpr(
            items[0],
            items[1],
            self._slice_bound(items[3]),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def vector_range_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.VectorRangeExpr:
        bounds = [self._slice_bound(item) for item in items[1:]]
        return ast_nodes.VectorRangeExpr(
            items[0], bounds[0], bounds[1], origin=self._span(meta)
        )

    @classmethod
    def _slice_bound(cls, expression: object) -> int | str:
        """Retain a parsed bound as a compile-time integer expression."""

        if isinstance(expression, ast_nodes.NumberExpr):
            return expression.value
        if isinstance(expression, ast_nodes.NameExpr):
            return expression.name
        if isinstance(expression, ast_nodes.UnaryExpr):
            return f"{expression.operator.value}{cls._slice_bound(expression.expression)}"
        if isinstance(expression, ast_nodes.BinaryExpr):
            return (
                f"{cls._slice_bound(expression.left)}"
                f"{expression.operator.value}"
                f"{cls._slice_bound(expression.right)}"
            )
        if isinstance(expression, ast_nodes.CallExpr) and not expression.specializations:
            arguments = ",".join(
                str(cls._slice_bound(argument)) for argument in expression.arguments
            )
            return f"{expression.function}({arguments})"
        # Preserve parsing as the syntax boundary; semantic analysis owns the
        # compile-time-only diagnostic for other expression shapes.
        return str(expression)

    @v_args(meta=True)
    def concat_expr(self, meta: object, items: list[object]) -> ast_nodes.ConcatExpr:
        arguments = items[0] if items and items[0] is not None else ()
        return ast_nodes.ConcatExpr(tuple(arguments), origin=self._span(meta))

    @v_args(meta=True)
    def bitcast_expr(self, meta: object, items: list[object]) -> ast_nodes.BitcastExpr:
        return ast_nodes.BitcastExpr(items[0], items[1], origin=self._span(meta))

    @v_args(meta=True)
    def reshape_expr(self, meta: object, items: list[object]) -> ast_nodes.ReshapeExpr:
        if len(items) == 1:
            return ast_nodes.ReshapeExpr(None, items[0], origin=self._span(meta))
        return ast_nodes.ReshapeExpr(items[0], items[1], origin=self._span(meta))

    @v_args(meta=True)
    def pack_expr(self, meta: object, items: list[object]) -> ast_nodes.PackExpr:
        return ast_nodes.PackExpr(items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unpack_expr(self, meta: object, items: list[object]) -> ast_nodes.UnpackExpr:
        return ast_nodes.UnpackExpr(items[0], items[1], origin=self._span(meta))

    def range_number(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def range_name(self, items: list[object]) -> str:
        return str(items[0])

    def range_intrinsic(self, items: list[object]) -> str:
        return f"{items[0]}({items[1]})"

    def range_parenthesized(self, items: list[object]) -> str:
        return f"({items[0]})"

    def range_expression(self, items: list[object]) -> str:
        return str(items[0])

    def range_binder(self, items: list[object]) -> tuple[str, int | str, int | str]:
        return (
            str(items[0]),
            items[1],
            items[2],
        )

    @v_args(meta=True)
    def generate_expr(self, meta: object, items: list[object]) -> ast_nodes.GenerateExpr:
        index, start, stop = items[0]
        return ast_nodes.GenerateExpr(
            index, start, stop, items[1], origin=self._span(meta)
        )

    @v_args(meta=True)
    def map_expr(self, meta: object, items: list[object]) -> ast_nodes.MapExpr:
        index, start, stop = items[0]
        return ast_nodes.MapExpr(index, start, stop, items[1], origin=self._span(meta))

    @v_args(meta=True)
    def indexed_sum_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.IndexedSumExpr:
        index, start, stop = items[0]
        return ast_nodes.IndexedSumExpr(
            index, start, stop, items[1], origin=self._span(meta)
        )

    @v_args(meta=True)
    def collection_sum_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.CollectionSumExpr:
        return ast_nodes.CollectionSumExpr(items[0], origin=self._span(meta))

    @v_args(meta=True)
    def reduce_expr(self, meta: object, items: list[object]) -> ast_nodes.ReduceExpr:
        return ast_nodes.ReduceExpr(
            ast_nodes.ReductionOperator(str(items[0])),
            items[1],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def dot_expr(self, meta: object, items: list[object]) -> ast_nodes.DotExpr:
        rounding = (
            ast_nodes.FixedRoundingMode(str(items[2]))
            if len(items) == 3 and items[2] is not None
            else None
        )
        return ast_nodes.DotExpr(items[0], items[1], rounding, origin=self._span(meta))

    @v_args(meta=True)
    def contextual_quantize_expr(self, meta: object, items: list[object]) -> ast_nodes.QuantizeExpr:
        return ast_nodes.QuantizeExpr(
            items[0], ast_nodes.FixedRoundingMode(str(items[1])), origin=self._span(meta)
        )

    @v_args(meta=True)
    def contextual_full_quantize_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.QuantizeExpr:
        """Keep policy-explicit contextual quantize on the existing AST node."""

        return ast_nodes.QuantizeExpr(
            items[0],
            ast_nodes.FixedRoundingMode(str(items[1])),
            overflow=ast_nodes.FixedOverflowMode(str(items[2])),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def full_quantize_expr(self, meta: object, items: list[object]) -> ast_nodes.QuantizeExpr:
        return ast_nodes.QuantizeExpr(
            items[1],
            ast_nodes.FixedRoundingMode(str(items[2])),
            target_type=items[0],
            overflow=ast_nodes.FixedOverflowMode(str(items[3])),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def sum_expr(self, meta: object, items: list[object]) -> object:
        expression = items[0]
        origin = self._span(meta)
        for index in range(1, len(items), 2):
            operator = str(items[index])
            operand = items[index + 1]
            expression = (
                ast_nodes.AddExpr(expression, operand, origin=origin)
                if operator == "+"
                else ast_nodes.BinaryExpr(
                    ast_nodes.BinaryOperator.SUBTRACT,
                    expression,
                    operand,
                    origin=origin,
                )
            )
        return expression

    @v_args(meta=True)
    def product_expr(self, meta: object, items: list[object]) -> object:
        expression = items[0]
        origin = self._span(meta)
        for index in range(1, len(items), 2):
            expression = ast_nodes.BinaryExpr(
                ast_nodes.BinaryOperator.MULTIPLY
                if str(items[index]) == "*" else ast_nodes.BinaryOperator.DIVIDE,
                expression,
                items[index + 1],
                origin=origin,
            )
        return expression

    @v_args(meta=True)
    def bit_and_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, ast_nodes.BinaryOperator.BIT_AND, self._span(meta))

    @v_args(meta=True)
    def bit_xor_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, ast_nodes.BinaryOperator.BIT_XOR, self._span(meta))

    @v_args(meta=True)
    def bit_or_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, ast_nodes.BinaryOperator.BIT_OR, self._span(meta))

    @v_args(meta=True)
    def logical_and_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, ast_nodes.BinaryOperator.LOGIC_AND, self._span(meta))

    @v_args(meta=True)
    def logical_or_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, ast_nodes.BinaryOperator.LOGIC_OR, self._span(meta))

    @v_args(meta=True)
    def shift_expr(self, meta: object, items: list[object]) -> object:
        expression = items[0]
        origin = self._span(meta)
        for index in range(1, len(items), 2):
            operator = ast_nodes.BinaryOperator(str(items[index]))
            expression = ast_nodes.BinaryExpr(
                operator,
                expression,
                items[index + 1],
                origin=origin,
            )
        return expression

    @v_args(meta=True)
    def comparison_expr(self, meta: object, items: list[object]) -> object:
        if len(items) == 1:
            return items[0]
        return ast_nodes.BinaryExpr(
            ast_nodes.BinaryOperator(str(items[1])),
            items[0],
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def conditional_expr(self, meta: object, items: list[object]) -> ast_nodes.MuxExpr:
        return ast_nodes.MuxExpr(items[0], items[1], items[2], origin=self._span(meta))

    @v_args(meta=True)
    def explicit_resize_expr(self, meta: object, items: list[object]) -> ast_nodes.ResizeExpr:
        return ast_nodes.ResizeExpr(
            ast_nodes.ResizeKind(str(items[0])),
            int(str(items[1])) if str(items[1]).isdigit() else str(items[1]),
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def contextual_resize_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ResizeExpr:
        return ast_nodes.ResizeExpr(
            ast_nodes.ResizeKind(str(items[0])),
            None,
            items[1],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def delay_expr(self, meta: object, items: list[object]) -> ast_nodes.DelayExpr:
        return ast_nodes.DelayExpr(
            int(str(items[0])), items[1], origin=self._span(meta)
        )

    def pipeline_constraint(self, items: list[object]) -> ast_nodes.PipelineConstraint:
        return ast_nodes.PipelineConstraint(
            ast_nodes.PipelineMetric(str(items[0])),
            ast_nodes.PipelineRelation(str(items[1])),
            self._parse_number(items[2]),
        )

    @v_args(meta=True)
    def pipeline_expr(self, meta: object, items: list[object]) -> ast_nodes.PipelineExpr:
        depth = str(items[0])
        domain = next(
            (str(item) for item in items[1:-1] if isinstance(item, str)),
            None,
        )
        return ast_nodes.PipelineExpr(
            int(depth),
            items[-1],
            tuple(
                item for item in items[1:-1]
                if isinstance(item, ast_nodes.PipelineConstraint)
            ),
            origin=self._span(meta),
            domain=domain,
        )

    @v_args(meta=True)
    def protocol_transform_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ProtocolTransformExpr:
        return ast_nodes.ProtocolTransformExpr(
            items[-1],
            tuple(
                item for item in items[:-1]
                if isinstance(item, ast_nodes.PipelineConstraint)
            ),
            origin=self._span(meta),
        )

    def implementation_arm(self, items: list[object]) -> ast_nodes.ImplementationArm:
        return ast_nodes.ImplementationArm(ast_nodes.ImplementationKind(str(items[0])), items[1])

    def explicit_implementation_selection(
        self, items: list[object]
    ) -> tuple[ast_nodes.ImplementationKind, ast_nodes.CostPolicy | None]:
        return (ast_nodes.ImplementationKind(str(items[0])), None)

    def cost_constraint(self, items: list[object]) -> ast_nodes.CostConstraint:
        return ast_nodes.CostConstraint(
            _cost_metric(str(items[0])),
            self._parse_number(items[1]),
        )

    def feedback_policy(self, items: list[object]) -> ast_nodes.SynthesisFeedback:
        return ast_nodes.SynthesisFeedback(str(items[0]))

    def cost_implementation_selection(
        self, items: list[object]
    ) -> tuple[ast_nodes.ImplementationKind | None, ast_nodes.CostPolicy]:
        return (
            None,
            ast_nodes.CostPolicy(
                _cost_metric(str(items[0])),
                tuple(
                    item for item in items[1:] if isinstance(item, ast_nodes.CostConstraint)
                ),
                next(
                    (
                        item
                        for item in items[1:]
                        if isinstance(item, ast_nodes.SynthesisFeedback)
                    ),
                    None,
                ),
            ),
        )

    @v_args(meta=True)
    def implementation_choice_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ImplementationChoiceExpr:
        selected, cost_policy = items[0]
        return ast_nodes.ImplementationChoiceExpr(
            selected,
            tuple(items[1:]),
            cost_policy,
            origin=self._span(meta),
        )

    def implement_constraint(self, items: list[object]) -> ast_nodes.ExplorationConstraint:
        return ast_nodes.ExplorationConstraint(
            _cost_metric(str(items[0])),
            ast_nodes.ExplorationRelation(str(items[1])),
            self._parse_number(items[2]),
        )

    def implement_minimize(self, items: list[object]) -> ast_nodes.ExplorationObjective:
        return ast_nodes.ExplorationObjective("minimize", _cost_metric(str(items[0])))

    def implement_maximize(self, items: list[object]) -> ast_nodes.ExplorationObjective:
        return ast_nodes.ExplorationObjective("maximize", _cost_metric(str(items[0])))

    def implement_intent(
        self, items: list[object]
    ) -> tuple[tuple[ast_nodes.ExplorationConstraint, ...], ast_nodes.ExplorationObjective | None]:
        constraints: list[ast_nodes.ExplorationConstraint] = []
        objective = None
        for item in items:
            if isinstance(item, ast_nodes.ExplorationConstraint):
                constraints.append(item)
            elif isinstance(item, ast_nodes.ExplorationObjective):
                if objective is not None:
                    raise ParseError("implement intent accepts exactly one objective")
                objective = item
        if not constraints and objective is None:
            raise ParseError("implement intent must contain a constraint or objective")
        metrics = [item.metric for item in constraints]
        if len(metrics) != len(set(metrics)):
            duplicate = next(item for item in metrics if metrics.count(item) > 1)
            raise ParseError(
                f"implement intent repeats '{duplicate.value}' constraint"
            )
        return (tuple(constraints), objective)

    @v_args(meta=True)
    def implement_expr(self, meta: object, items: list[object]) -> ast_nodes.ImplementExpr:
        constraints, objective = items[1]
        return ast_nodes.ImplementExpr(
            items[0],
            constraints,
            objective,
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def mux_expr(self, meta: object, items: list[object]) -> ast_nodes.MuxExpr:
        return ast_nodes.MuxExpr(
            items[0], items[1], items[2], origin=self._span(meta)
        )

    def numeric_switch_key(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    @v_args(meta=True)
    def enum_switch_key(self, meta: object, items: list[object]) -> ast_nodes.EnumMemberRef:
        owner, member = str(items[0]).split(".", 1)
        return ast_nodes.EnumMemberRef(owner, member, self._token_span(items[0]) or self._span(meta))

    @v_args(meta=True)
    def qualified_nominal_ref(
        self, meta: object, items: list[object]
    ) -> tuple[str, str, SourceSpan | None]:
        owner, member = str(items[0]).split(".", 1)
        return owner, member, self._token_span(items[0]) or self._span(meta)

    @v_args(meta=True)
    def qualified_nominal_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.FieldExpr:
        owner, member, token_span = items[0]
        origin = token_span or self._span(meta)
        member_origin = None
        if origin is not None and origin.start_line == origin.end_line:
            member_origin = SourceSpan(
                origin.end_line,
                origin.end_column - len(member),
                origin.end_line,
                origin.end_column,
            )
        return ast_nodes.FieldExpr(
            ast_nodes.NameExpr(owner, origin=origin),
            member,
            origin=origin,
            member_origin=member_origin,
        )

    def switch_arm(self, items: list[object]) -> ast_nodes.SwitchArm:
        return ast_nodes.SwitchArm(items[0], items[1])

    def else_arm(self, items: list[object]) -> object:
        return items[0]

    @v_args(meta=True)
    def switch_expr(self, meta: object, items: list[object]) -> ast_nodes.SwitchExpr:
        arms = tuple(item for item in items[1:] if isinstance(item, ast_nodes.SwitchArm))
        default = next(
            (item for item in items[1:] if not isinstance(item, ast_nodes.SwitchArm)), None
        )
        return ast_nodes.SwitchExpr(
            items[0],
            arms,
            default,
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def qualified_switch_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.SwitchExpr:
        owner, member = str(items[0]).split(".", 1)
        origin = self._span(meta)
        selector = ast_nodes.FieldExpr(ast_nodes.NameExpr(owner, origin=origin), member, origin=origin)
        arms = tuple(item for item in items[1:] if isinstance(item, ast_nodes.SwitchArm))
        default = next(
            (
                item for item in items[1:]
                if item is not None and not isinstance(item, ast_nodes.SwitchArm)
            ),
            None,
        )
        return ast_nodes.SwitchExpr(selector, arms, default, origin=origin)

    @v_args(meta=True)
    def tagged_union_construct_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TaggedUnionConstructExpr:
        head = items[0]
        owner, variant = (
            head
            if isinstance(head, tuple)
            else str(head).split(".", 1)
        )
        return ast_nodes.TaggedUnionConstructExpr(
            owner, variant, tuple(items[1:]), origin=self._span(meta)
        )

    def tagged_union_binders(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def tagged_union_match_arm(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TaggedUnionMatchArm:
        owner, variant = str(items[0]).split(".", 1)
        binders = next((item for item in items[1:-1] if isinstance(item, tuple)), ())
        return ast_nodes.TaggedUnionMatchArm(
            owner, variant, binders, items[-1], self._span(meta)
        )

    @v_args(meta=True)
    def tagged_union_match_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TaggedUnionMatchExpr:
        return ast_nodes.TaggedUnionMatchExpr(
            items[0], tuple(items[1:]), origin=self._span(meta)
        )

    @v_args(meta=True)
    def qualified_tagged_union_match_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TaggedUnionMatchExpr:
        owner, member = str(items[0]).split(".", 1)
        origin = self._span(meta)
        selector = ast_nodes.FieldExpr(ast_nodes.NameExpr(owner, origin=origin), member, origin=origin)
        return ast_nodes.TaggedUnionMatchExpr(
            selector, tuple(items[1:]), origin=origin
        )

    @staticmethod
    def _fold(
        items: list[object],
        operator: ast_nodes.BinaryOperator,
        origin: SourceSpan,
    ) -> object:
        expression = items[0]
        for operand in items[1:]:
            expression = ast_nodes.BinaryExpr(
                operator, expression, operand, origin=origin
            )
        return expression

    @staticmethod
    def _span(meta: object) -> SourceSpan:
        return SourceSpan(
            int(getattr(meta, "line")),
            int(getattr(meta, "column")),
            int(getattr(meta, "end_line")),
            int(getattr(meta, "end_column")),
        )

    @staticmethod
    def _token_span(token: object) -> SourceSpan | None:
        """Return the exact lexer-token span when Lark retained positions."""

        if not all(
            hasattr(token, attribute)
            for attribute in ("line", "column", "end_line", "end_column")
        ):
            return None
        return SourceSpan(
            int(getattr(token, "line")),
            int(getattr(token, "column")),
            int(getattr(token, "end_line")),
            int(getattr(token, "end_column")),
        )

    @classmethod
    def _name_span_from_meta(cls, meta: object, name: str) -> SourceSpan | None:
        """Use the parser production start plus token length for simple names."""

        if not all(
            hasattr(meta, attribute) for attribute in ("line", "column")
        ) or "." in name:
            return None
        start_line = int(getattr(meta, "line"))
        start_column = int(getattr(meta, "column"))
        return SourceSpan(
            start_line,
            start_column,
            start_line,
            start_column + len(name),
        )

    @classmethod
    def _name_span_from_end_meta(cls, meta: object, name: str) -> SourceSpan | None:
        """Recover a trailing simple-name span from propagated production end."""

        if not name or "." in name or not all(
            hasattr(meta, attribute) for attribute in ("end_line", "end_column")
        ):
            return None
        end_line = int(getattr(meta, "end_line"))
        end_column = int(getattr(meta, "end_column"))
        if end_column <= len(name):
            return None
        return SourceSpan(
            end_line,
            end_column - len(name),
            end_line,
            end_column,
        )

    @staticmethod
    def _parse_number(value: object) -> int:
        text = str(value).replace("_", "")
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.lower().startswith("0b"):
            return int(text, 2)
        return int(text, 10)


__all__ = ["ExpressionRules"]
