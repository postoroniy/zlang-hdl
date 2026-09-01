"""Name resolution and type checking for the Milestone 0 language."""

from __future__ import annotations

import re
import ast as pyast
import hashlib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from fractions import Fraction

from zlang.ast import nodes as ast
from . import compile_time_real as ct_real
from zlang.ir import expressions as ir_expr
from zlang.ir import csr as ir_csr
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.ir import state as ir_state
from zlang.ir import cdc as ir_cdc
from zlang.ir import arbitration as ir_arbitration
from zlang.ir import verification as ir_verification
from zlang.ir import pipelines as ir_pipelines
from zlang.ir import elastic as ir_elastic
from zlang.ir import architectures as ir_architectures
from zlang.ir import packing as ir_packing
from zlang.ir import timing as ir_timing
from zlang.ir import external as ir_external
from zlang.ir.numeric import (
    NumericTypeError,
    NumericTypeErrorReason,
    addition_rule,
    bitwise_rule,
    comparison_rule,
    multiplication_rule,
    subtraction_rule,
)
from zlang.ir.runtime_values import (
    minimum_signed_width,
    minimum_unsigned_width,
    normalize_scalar,
    scalar_fits,
)
from zlang.ir.hierarchy import (
    HierarchyError,
    HierarchyTraversalCache,
    build_hierarchy_index,
    candidate_specialization_identity,
    validate_hierarchical_connections,
)
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.callables import (
    CallableExpansionError,
    CallableReachabilityError,
    expand_callable_calls,
    reachable_callable_definitions,
    stable_callee_identity,
)
from zlang.ir.functional import (
    collection_elements,
    compact_functional_elements,
    FunctionalLoweringError,
    reduction_result_type,
    vector_leaf_shape,
)
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    ExactReductionCombine,
    FunctionalRegionKind,
    build_exact_reduction_plan,
)
from zlang.common import stable_digest
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    walk_expression,
)
from zlang.timing import timing_info
from zlang.ir.interfaces import (
    ConnectionAdapter,
    CreditSignal,
    InterfaceProtocol,
    InterfaceSignal,
    PacketSignal,
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseOrdering,
    RequestResponseRole,
    VirtualChannelCreditSignal,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    FixedOverflowPolicy,
    HardwareType,
    SIntType,
    StructField,
    StructType,
    TaggedUnionField,
    TaggedUnionType,
    TaggedUnionVariant,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.pipelines import PipelineExplorationError, explore_pipeline
from zlang.architectures import (
    ArchitectureExplorationError,
    explore_architecture,
)
from zlang.source import SourceOrigin
from .errors import SemanticError
from .public_timing import analyze_public_module_timing
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.module_resolver import (
    ModuleResolutionContext,
    ModuleResolutionError,
    ModuleResolver,
    StdlibModuleResolver,
)
from zlang.generics import GenericArgument, SpecializationIdentity
from zlang.exploration import (
    ExplorationContext,
    ExplorationRequest,
    TransformFamily,
    constraints_from_syntax,
    explore,
)


_UNSIGNED_PATTERN = re.compile(r"u([1-9][0-9]*)\Z")
_SIGNED_PATTERN = re.compile(r"s([1-9][0-9]*)\Z")
_GENERIC_PATTERN = re.compile(r"(uint|sint|bits)<(.+)>\Z")
_FUNCTIONAL_RANGE_LIMIT = 4096
_TOTAL_GENERATED_LIMIT = 65536
_FUNCTIONAL_REGION_THRESHOLD = 32
_COMPILE_TIME_CALL_LIMIT = 64
_COMPILE_TIME_OPERATION_LIMIT = 1_000_000
_COMPILE_TIME_EVALUATOR_SCHEMA = "zlang-ct-v1"
_REAL_INTRINSICS = {"pi", "sin", "cos", "log2", "log"}


def _canonical_rom_runtime_values(
    type_: HardwareType,
    value: object,
) -> object:
    """Canonical, type-directed payload used for initialized-ROM hashing."""

    if isinstance(type_, StructType):
        if not isinstance(value, dict):
            raise SemanticError(f"constant ROM value does not match {type_}")
        return (
            "struct",
            str(type_),
            tuple(
                (
                    field.name,
                    _canonical_rom_runtime_values(field.type, value[field.name]),
                )
                for field in type_.fields
            ),
        )
    if isinstance(type_, TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise SemanticError(f"constant ROM value does not match {type_}")
        return (
            "tuple",
            str(type_),
            tuple(
                _canonical_rom_runtime_values(element_type, element)
                for element_type, element in zip(
                    type_.elements, value, strict=True
                )
            ),
        )
    if isinstance(type_, VecType):
        if not isinstance(value, (tuple, list)) or len(value) != type_.length:
            raise SemanticError(f"constant ROM value does not match {type_}")
        return (
            "vec",
            str(type_),
            tuple(
                _canonical_rom_runtime_values(type_.element_type, element)
                for element in value
            ),
        )
    return ("scalar", str(type_), value)


def _rom_constant_expression(
    type_: HardwareType,
    value: object,
    origin: SourceOrigin | None,
) -> ir_expr.Expression:
    """Rebuild one bounded ROM word from its evaluated immutable value."""

    if isinstance(type_, StructType):
        if not isinstance(value, dict):
            raise SemanticError(f"constant ROM value does not match {type_}")
        return ir_expr.StructConstruct(
            type_.name,
            tuple(
                (
                    field.name,
                    _rom_constant_expression(field.type, value[field.name], origin),
                )
                for field in type_.fields
            ),
            type_,
            origin=origin,
        )
    if isinstance(type_, TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise SemanticError(f"constant ROM value does not match {type_}")
        return ir_expr.TupleConstruct(
            tuple(
                _rom_constant_expression(element_type, element, origin)
                for element_type, element in zip(
                    type_.elements, value, strict=True
                )
            ),
            type_,
            origin=origin,
        )
    if isinstance(type_, VecType):
        if not isinstance(value, (tuple, list)) or len(value) != type_.length:
            raise SemanticError(f"constant ROM value does not match {type_}")
        return ir_expr.Generate(
            "rom_init",
            0,
            type_.length,
            tuple(
                _rom_constant_expression(type_.element_type, element, origin)
                for element in value
            ),
            type_,
            origin=origin,
        )
    return ir_expr.Constant(value, type_, origin=origin)


def _builtin_type(name: str) -> HardwareType | None:
    if name == "bit":
        return BitType()
    if name == "char":
        return UIntType(8)
    unsigned_match = _UNSIGNED_PATTERN.fullmatch(name)
    if unsigned_match is not None:
        return UIntType(int(unsigned_match.group(1)))
    signed_match = _SIGNED_PATTERN.fullmatch(name)
    if signed_match is not None:
        return SIntType(int(signed_match.group(1)))
    generic_match = _GENERIC_PATTERN.fullmatch(name)
    if generic_match is None:
        return None
    family, width_text = generic_match.groups()
    if not width_text.isdigit():
        return None
    width = int(width_text)
    if family == "uint":
        return UIntType(width)
    if family == "sint":
        return SIntType(width)
    return BitsType(width)


def _contains_enum_type(type_: HardwareType) -> bool:
    if isinstance(type_, EnumType):
        return True
    if isinstance(type_, StructType):
        return any(_contains_enum_type(field.type) for field in type_.fields)
    if isinstance(type_, TupleType):
        return any(_contains_enum_type(element) for element in type_.elements)
    if isinstance(type_, VecType):
        return _contains_enum_type(type_.element_type)
    return False


def _contains_tagged_union_type(type_: HardwareType) -> bool:
    if isinstance(type_, TaggedUnionType):
        return True
    if isinstance(type_, StructType):
        return any(_contains_tagged_union_type(field.type) for field in type_.fields)
    if isinstance(type_, TupleType):
        return any(_contains_tagged_union_type(element) for element in type_.elements)
    if isinstance(type_, VecType):
        return _contains_tagged_union_type(type_.element_type)
    return False


def _tuple_type_parts(text: str) -> tuple[str, ...] | None:
    """Split one structural tuple spelling at top-level commas only."""

    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return None
    body = text[1:-1]
    angle_depth = 0
    paren_depth = 0
    begin = 0
    parts: list[str] = []
    for index, character in enumerate(body):
        if character == "<":
            angle_depth += 1
        elif character == ">":
            angle_depth -= 1
        elif character == "(":
            paren_depth += 1
        elif character == ")":
            paren_depth -= 1
        elif character == "," and angle_depth == 0 and paren_depth == 0:
            parts.append(body[begin:index].strip())
            begin = index + 1
        if angle_depth < 0 or paren_depth < 0:
            return None
    if angle_depth != 0 or paren_depth != 0:
        return None
    parts.append(body[begin:].strip())
    if not 2 <= len(parts) <= 8 or any(not part for part in parts):
        return None
    return tuple(parts)


def _render_type_syntax(syntax: ast.TypeSyntax) -> str:
    """Render recursive source type syntax for diagnostics only."""

    if isinstance(syntax, ast.TypeName):
        return syntax.text
    if isinstance(syntax, ast.VectorTypeName):
        return f"vec<{syntax.length},{_render_type_syntax(syntax.element_type)}>"
    if isinstance(syntax, ast.TupleTypeName):
        return f"({','.join(_render_type_syntax(item) for item in syntax.elements)})"
    raise TypeError(f"unsupported source type syntax {type(syntax).__name__}")


class _TypeResolver:
    def __init__(
        self,
        aliases: tuple[ast.TypeAlias, ...],
        structs: tuple[ast.StructDecl, ...],
        enums: tuple[ast.EnumDecl, ...] = (),
        parameters: tuple[ast.ModuleParameter, ...] = (),
        parameter_values: dict[str, int | str] | None = None,
        type_bindings: dict[str, HardwareType] | None = None,
        identity_namespace: str | None = None,
        tagged_unions: tuple[ast.TaggedUnionDecl, ...] = (),
    ) -> None:
        self._aliases: dict[str, ast.TypeSyntax] = {}
        self._structs: dict[str, ast.StructDecl] = {}
        self._enum_declarations: dict[str, ast.EnumDecl] = {}
        self._enums: dict[str, EnumType] = {}
        self._tagged_union_declarations: dict[str, ast.TaggedUnionDecl] = {}
        self._resolved: dict[str, HardwareType] = {}
        self._active: list[str] = []
        self._parameters = {p.name: p for p in parameters}
        self._parameter_values = dict(parameter_values or {})
        self._type_bindings = dict(type_bindings or {})
        self._identity_namespace = identity_namespace

        for declaration in aliases:
            self._check_available(declaration.name, "type alias")
            self._aliases[declaration.name] = declaration.target
        for declaration in structs:
            self._check_available(declaration.name, "struct")
            self._structs[declaration.name] = declaration
        for declaration in enums:
            self._check_available(declaration.name, "enum")
            if not declaration.members:
                raise SemanticError(
                    f"enum '{declaration.name}' must contain at least one member"
                )
            duplicate = next(
                (
                    member for member in declaration.members
                    if declaration.members.count(member) > 1
                ),
                None,
            )
            if duplicate is not None:
                raise SemanticError(
                    f"duplicate member '{duplicate}' in enum '{declaration.name}'"
                )
            owner = declaration.source_identity or identity_namespace
            if owner is None:
                raise SemanticError(
                    f"enum '{declaration.name}' has no logical declaration identity"
                )
            explicit_width: int | None = None
            explicit_codes: tuple[int, ...] | None = None
            encodings = declaration.encodings or tuple(
                None for _ in declaration.members
            )
            if len(encodings) != len(declaration.members):
                raise SemanticError(
                    f"enum '{declaration.name}' has malformed member encodings"
                )
            if declaration.backing_type is None:
                if any(code is not None for code in encodings):
                    raise SemanticError(
                        f"enum '{declaration.name}' member codes require an explicit bits<W> backing type"
                    )
            else:
                backing = self.resolve(declaration.backing_type)
                if not isinstance(backing, BitsType):
                    raise SemanticError(
                        f"enum '{declaration.name}' backing type must be bits<W>, got {backing}"
                    )
                missing_code = next(
                    (
                        member for member, code in zip(
                            declaration.members, encodings, strict=True
                        )
                        if code is None
                    ),
                    None,
                )
                if missing_code is not None:
                    raise SemanticError(
                        f"explicit enum '{declaration.name}' member '{missing_code}' requires a code"
                    )
                explicit_width = backing.width
                explicit_codes = tuple(
                    int(code) for code in encodings if code is not None
                )
                duplicate_code = next(
                    (
                        code for code in explicit_codes
                        if explicit_codes.count(code) > 1
                    ),
                    None,
                )
                if duplicate_code is not None:
                    raise SemanticError(
                        f"duplicate code {duplicate_code} in enum '{declaration.name}'"
                    )
                oversized = next(
                    (code for code in explicit_codes if code >= (1 << backing.width)),
                    None,
                )
                if oversized is not None:
                    raise SemanticError(
                        f"enum '{declaration.name}' code {oversized} does not fit {backing}"
                    )
            enum_type = EnumType(
                declaration.name,
                declaration.members,
                f"{owner}::enum::{declaration.name}",
                explicit_width,
                explicit_codes,
            )
            self._enum_declarations[declaration.name] = declaration
            self._enums[declaration.name] = enum_type
        for declaration in tagged_unions:
            self._check_available(declaration.name, "tagged union")
            self._tagged_union_declarations[declaration.name] = declaration

    def _check_available(self, name: str, description: str) -> None:
        if _builtin_type(name) is not None or name == "string":
            raise SemanticError(f"{description} '{name}' uses a reserved type name")
        if name in self._aliases:
            if description == "type alias":
                raise SemanticError(f"duplicate type alias '{name}'")
            raise SemanticError(f"type name '{name}' is already an alias")
        if name in self._structs:
            if description == "struct":
                raise SemanticError(f"duplicate struct '{name}'")
            raise SemanticError(f"type name '{name}' is already a struct")
        if name in self._enums:
            if description == "enum":
                raise SemanticError(f"duplicate enum '{name}'")
            raise SemanticError(f"type name '{name}' is already an enum")
        if name in self._tagged_union_declarations:
            if description == "tagged union":
                raise SemanticError(f"duplicate tagged union '{name}'")
            raise SemanticError(f"type name '{name}' is already a tagged union")

    def resolve(self, syntax: ast.TypeSyntax) -> HardwareType:
        if isinstance(syntax, ast.VectorTypeName):
            length = syntax.length
            if isinstance(length, str):
                length = self._eval_width(length)
            if length < 1:
                raise SemanticError("vector width must be positive")
            return VecType(length, self.resolve(syntax.element_type))
        if isinstance(syntax, ast.TupleTypeName):
            return TupleType(tuple(self.resolve(item) for item in syntax.elements))
        if syntax.text in self._type_bindings:
            return self._type_bindings[syntax.text]
        builtin = self._resolve_builtin(syntax.text)
        if builtin is not None:
            return builtin
        return self._resolve_named(syntax.text)

    def _eval_constant_integer(
        self,
        text: str,
        *,
        description: str,
        allow_zero: bool = False,
        allow_negative: bool = False,
        positive_description: str | None = None,
        local_values: dict[str, int] | None = None,
        allow_resolver_parameters: bool = True,
    ) -> int:
        try:
            tree = pyast.parse(text, mode="eval")
        except SyntaxError as error:
            raise SemanticError(
                f"invalid constant {description} expression '{text}'"
            ) from error

        resolving: set[str] = set()

        def visit(node: pyast.AST) -> int:
            if isinstance(node, pyast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, pyast.Name):
                if local_values is not None and node.id in local_values:
                    return local_values[node.id]
                if allow_resolver_parameters and node.id in self._parameter_values:
                    value = self._parameter_values[node.id]
                    if isinstance(value, int):
                        return value
                    if isinstance(value, str):
                        if node.id in resolving:
                            raise SemanticError(
                                f"cyclic compile-time parameter '{node.id}' in {description}"
                            )
                        resolving.add(node.id)
                        try:
                            return visit(pyast.parse(value, mode="eval").body)
                        finally:
                            resolving.remove(node.id)
                raise SemanticError(
                    f"unresolved compile-time parameter '{node.id}' in {description}"
                )
            if isinstance(node, pyast.UnaryOp) and isinstance(node.op, (pyast.UAdd, pyast.USub)):
                value = visit(node.operand)
                return value if isinstance(node.op, pyast.UAdd) else -value
            if isinstance(node, pyast.BinOp) and isinstance(
                node.op,
                (pyast.Add, pyast.Sub, pyast.Mult, pyast.FloorDiv, pyast.Div,
                 pyast.LShift, pyast.RShift, pyast.Mod, pyast.BitAnd,
                 pyast.BitOr, pyast.BitXor),
            ):
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, pyast.Add): return left + right
                if isinstance(node.op, pyast.Sub): return left - right
                if isinstance(node.op, pyast.Mult): return left * right
                if isinstance(node.op, pyast.LShift):
                    if right < 0:
                        raise SemanticError(
                            f"constant {description} shift must be non-negative"
                        )
                    return left << right
                if isinstance(node.op, pyast.RShift):
                    if right < 0:
                        raise SemanticError(
                            f"constant {description} shift must be non-negative"
                        )
                    return left >> right
                if isinstance(node.op, pyast.Mod):
                    if right == 0:
                        raise SemanticError(f"constant {description} division by zero")
                    return left % right
                if isinstance(node.op, pyast.BitAnd): return left & right
                if isinstance(node.op, pyast.BitOr): return left | right
                if isinstance(node.op, pyast.BitXor): return left ^ right
                if right == 0:
                    raise SemanticError(
                        f"constant {description} division by zero"
                    )
                if left % right:
                    raise SemanticError(
                        f"constant {description} division must be exact"
                    )
                return left // right
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Name):
                if len(node.args) != 1:
                    raise SemanticError(
                        f"compile-time intrinsic '{node.func.id}' expects one argument"
                    )
                value = visit(node.args[0])
                if node.func.id == "floor_log2":
                    if value <= 0:
                        raise SemanticError("floor_log2 requires a positive integer")
                    return value.bit_length() - 1
                if node.func.id == "ceil_log2":
                    if value <= 0:
                        raise SemanticError("ceil_log2 requires a positive integer")
                    return max(0, (value - 1).bit_length())
                if node.func.id == "index_width":
                    if value <= 0:
                        raise SemanticError("index_width requires a positive integer")
                    return max(1, (value - 1).bit_length())
                if node.func.id == "is_power_of_two":
                    return int(value > 0 and (value & (value - 1)) == 0)
                raise SemanticError(
                    f"constant {description} intrinsic '{node.func.id}' is not an integer intrinsic"
                )
            raise SemanticError(
                f"{description} '{text}' is not a constant module expression"
            )
        value = visit(tree.body)
        if (value < 0 and not allow_negative) or (value == 0 and not allow_zero):
            raise SemanticError(
                f"{positive_description or description} must be positive"
            )
        return value

    def _eval_width(self, text: str, *, allow_zero: bool = False) -> int:
        return self._eval_constant_integer(
            text,
            description="width",
            allow_zero=allow_zero,
            positive_description="type width",
        )


    def _eval_storage_depth(self, value: int | str, *, kind: str, name: str) -> int:
        description = f"{kind} '{name}' depth"
        if isinstance(value, int):
            if value <= 0:
                raise SemanticError(f"{description} must be positive")
            return value
        return self._eval_constant_integer(value, description=description)

    def _resolve_builtin(self, name: str) -> HardwareType | None:
        if name == "bit":
            return BitType()
        if name == "char":
            return UIntType(8)
        string = re.fullmatch(r"string<(.+)>", name)
        if string:
            length = self._eval_width(string.group(1))
            return VecType(length, UIntType(8))
        vector = re.fullmatch(r"vec<([^,]+),(.+)>", name)
        if vector:
            length = self._eval_width(vector.group(1))
            return VecType(length, self.resolve(ast.TypeName(vector.group(2).strip())))
        match = re.fullmatch(r"(uint|sint|bits)<(.+)>", name)
        if match:
            width = self._eval_width(match.group(2))
            return {"uint": UIntType, "sint": SIntType, "bits": BitsType}[match.group(1)](width)
        concise_fixed = re.fullmatch(r"(SF_Sat|UF_Sat|SF|UF)([1-9][0-9]*)\.([0-9]+)", name)
        if concise_fixed:
            family, integer_text, fraction_text = concise_fixed.groups()
            integer, fraction = int(integer_text), int(fraction_text)
            fixed_class = FixedType if family.startswith("SF") else UFixedType
            overflow = (FixedOverflowPolicy.SATURATE
                        if "_Sat" in family else FixedOverflowPolicy.WRAP)
            return fixed_class(integer + fraction, fraction, overflow)
        fixed = re.fullmatch(r"(fixed|ufixed|fixed_sat|ufixed_sat)<([^,]+),([^,]+)>", name)
        if fixed:
            width = self._eval_width(fixed.group(2))
            # Fraction zero is valid, unlike ordinary hardware widths.
            fraction_text = fixed.group(3)
            try:
                fraction = (int(fraction_text) if fraction_text.isdigit()
                            else self._eval_width(fraction_text, allow_zero=True))
            except SemanticError as exc:
                raise SemanticError(f"invalid fixed-point fractional width '{fraction_text}'") from exc
            if fraction < 0 or fraction >= width:
                raise SemanticError("fixed-point fractional width must satisfy 0 <= F < W")
            family = fixed.group(1)
            fixed_class = FixedType if family in {"fixed", "fixed_sat"} else UFixedType
            overflow = (FixedOverflowPolicy.SATURATE
                        if family.endswith("_sat") else FixedOverflowPolicy.WRAP)
            return fixed_class(width, fraction, overflow)
        return _builtin_type(name)

    @staticmethod
    def _generic_parts(name: str) -> tuple[str, tuple[str, ...]] | None:
        start = name.find("<")
        if start < 0 or not name.endswith(">"): return None
        angle_depth, paren_depth, parts, begin = 0, 0, [], start + 1
        for index in range(start + 1, len(name) - 1):
            char = name[index]
            if char == "<": angle_depth += 1
            elif char == ">": angle_depth -= 1
            elif char == "(": paren_depth += 1
            elif char == ")": paren_depth -= 1
            elif char == "," and angle_depth == 0 and paren_depth == 0:
                parts.append(name[begin:index].strip()); begin = index + 1
        parts.append(name[begin:-1].strip())
        return name[:start], tuple(parts)

    def _resolve_named(self, name: str) -> HardwareType:
        if name in self._resolved:
            return self._resolved[name]
        if name in self._active:
            start = self._active.index(name)
            cycle = " -> ".join((*self._active[start:], name))
            raise SemanticError(f"cyclic type alias: {cycle}")
        self._active.append(name)
        try:
            tuple_parts = _tuple_type_parts(name)
            if tuple_parts is not None:
                resolved_tuple = TupleType(
                    tuple(self.resolve(ast.TypeName(part)) for part in tuple_parts)
                )
                self._resolved[name] = resolved_tuple
                return resolved_tuple
            generic = self._generic_parts(name)
            if generic is not None and generic[0] in self._structs:
                base, arguments = generic
                declaration = self._structs[base]
                if len(arguments) != len(declaration.parameters):
                    raise SemanticError(f"struct '{base}' expects {len(declaration.parameters)} parameters")
                values = dict(self._parameter_values)
                bindings: dict[str, HardwareType] = {}
                canonical_arguments: list[str] = []
                for parameter, argument in zip(declaration.parameters, arguments):
                    if parameter.kind == "type":
                        resolved_argument = self.resolve(ast.TypeName(argument))
                        bindings[parameter.name] = resolved_argument
                        canonical_arguments.append(str(resolved_argument))
                    else:
                        resolved_value = self._eval_width(argument)
                        values[parameter.name] = resolved_value
                        canonical_arguments.append(str(resolved_value))
                nested = _TypeResolver(
                    tuple(ast.TypeAlias(k, v) for k, v in self._aliases.items()),
                    tuple(self._structs.values()),
                    tuple(self._enum_declarations.values()),
                    tuple(declaration.parameters),
                    values,
                    bindings,
                    self._identity_namespace,
                    tagged_unions=tuple(
                        self._tagged_union_declarations.values()
                    ),
                )
                fields = tuple(StructField(field.name, nested.resolve(field.type_name)) for field in declaration.fields)
                result = StructType(
                    f"{base}<{','.join(canonical_arguments)}>", fields
                )
            elif name in self._aliases:
                result = self.resolve(self._aliases[name])
            elif name in self._enums:
                result = self._enums[name]
            elif name in self._structs:
                declaration = self._structs[name]
                if not declaration.fields:
                    raise SemanticError(f"struct '{name}' must contain at least one field")
                field_names: set[str] = set()
                fields: list[StructField] = []
                for field in declaration.fields:
                    if field.name in field_names:
                        raise SemanticError(
                            f"duplicate field '{field.name}' in struct '{name}'"
                        )
                    field_names.add(field.name)
                    fields.append(StructField(field.name, self.resolve(field.type_name)))
                result = StructType(name, tuple(fields))
            elif name in self._tagged_union_declarations:
                declaration = self._tagged_union_declarations[name]
                if not declaration.variants:
                    raise SemanticError(
                        f"tagged union '{name}' must contain at least one variant"
                    )
                variant_names = tuple(item.name for item in declaration.variants)
                duplicate_variant = next(
                    (item for item in variant_names if variant_names.count(item) > 1),
                    None,
                )
                if duplicate_variant is not None:
                    raise SemanticError(
                        f"duplicate variant '{duplicate_variant}' in tagged union '{name}'"
                    )
                owner = declaration.source_identity or self._identity_namespace
                if owner is None:
                    raise SemanticError(
                        f"tagged union '{name}' has no logical declaration identity"
                    )
                variants: list[TaggedUnionVariant] = []
                flat = (BitType, BitsType, UIntType, SIntType, FixedType, UFixedType)
                for variant in declaration.variants:
                    field_names = tuple(item.name for item in variant.fields)
                    duplicate_field = next(
                        (item for item in field_names if field_names.count(item) > 1),
                        None,
                    )
                    if duplicate_field is not None:
                        raise SemanticError(
                            f"duplicate field '{duplicate_field}' in tagged-union variant "
                            f"'{name}.{variant.name}'"
                        )
                    fields_: list[TaggedUnionField] = []
                    for field_ in variant.fields:
                        resolved = self.resolve(field_.type_name)
                        if not isinstance(resolved, flat):
                            raise SemanticError(
                                f"tagged-union field '{name}.{variant.name}.{field_.name}' "
                                f"must have a flat scalar type, got {resolved}"
                            )
                        fields_.append(TaggedUnionField(field_.name, resolved))
                    variants.append(TaggedUnionVariant(variant.name, tuple(fields_)))
                result = TaggedUnionType(
                    name, tuple(variants), f"{owner}::union::{name}"
                )
            else:
                raise SemanticError(f"unknown type '{name}'")
            self._resolved[name] = result
            return result
        finally:
            self._active.pop()

    def resolve_all(self) -> tuple[StructType, ...]:
        for name in self._aliases:
            self._resolve_named(name)
        return tuple(
            self._resolve_named(name)
            for name, declaration in self._structs.items()
            if not declaration.parameters
        )

    def resolve_enums(self) -> tuple[EnumType, ...]:
        """Return nominal enums in source declaration order."""
        return tuple(self._enums.values())

    def resolve_tagged_unions(self) -> tuple[TaggedUnionType, ...]:
        return tuple(
            self._resolve_named(name)
            for name in self._tagged_union_declarations
        )

    def tagged_union_type(self, name: str) -> TaggedUnionType | None:
        if name not in self._tagged_union_declarations:
            return None
        resolved = self._resolve_named(name)
        assert isinstance(resolved, TaggedUnionType)
        return resolved

    def enum_type(self, name: str) -> EnumType | None:
        return self._enums.get(name)


def _resolved_module_value_parameters(
    module: ast.Module, resolver: _TypeResolver
) -> tuple[dict[str, int], frozenset[str]]:
    """Resolve the module's one canonical compile-time value environment."""

    # Defaults are declaration-ordered.  The resolver is initially populated
    # with the source defaults so type syntax can be collected before this
    # pass; do not let that bootstrap environment make a later parameter
    # visible to an earlier default.  Concrete values are installed again as
    # each declaration is resolved.
    value_parameter_names = {
        parameter.name
        for parameter in module.parameters
        if parameter.kind == "value"
    }
    for name in value_parameter_names:
        resolver._parameter_values.pop(name, None)

    values: dict[str, int] = {}
    unresolved: set[str] = set()
    for parameter in module.parameters:
        if parameter.kind != "value":
            continue
        if parameter.default is None:
            unresolved.add(parameter.name)
            continue
        if isinstance(parameter.default, int):
            value = parameter.default
        else:
            value = resolver._eval_constant_integer(
                str(parameter.default),
                description=f"value parameter '{parameter.name}'",
                allow_zero=True,
                allow_negative=True,
            )
        values[parameter.name] = value
        resolver._parameter_values[parameter.name] = value
    return values, frozenset(unresolved)


def _resolved_module_parameter_records(
    module: ast.Module,
    specialization_type_bindings: dict[str, HardwareType] | None,
    specialization_constant_bindings: dict[str, ir_expr.Expression] | None,
    specialization_callable_bindings: dict[str, _StaticCallableBinding] | None,
) -> tuple[tuple[str, str, int | str | None], ...]:
    """Freeze the exact compile-time specialization arguments once.

    Candidate-site ownership and the final typed ``Module.parameters`` must use
    the same payload.  Keeping that payload in one helper prevents M39 from
    falling back to a source-module name when two child specializations coexist.
    """

    return tuple(
        (
            parameter.name,
            parameter.kind,
            (
                str(specialization_type_bindings[parameter.name])
                if parameter.kind == "type"
                and specialization_type_bindings is not None
                and parameter.name in specialization_type_bindings
                else parameter.default
                if parameter.kind in {"type", "value"}
                else (
                    "constant:"
                    + str(specialization_constant_bindings[parameter.name].type)
                    + ":"
                    + hashlib.sha256(
                        repr((
                            str(specialization_constant_bindings[parameter.name].type),
                            constant_runtime_value(
                                specialization_constant_bindings[parameter.name]
                            ),
                        )).encode("utf-8")
                    ).hexdigest()
                    if parameter.kind == "constant"
                    and specialization_constant_bindings is not None
                    and parameter.name in specialization_constant_bindings
                    else (
                        "callable:"
                        + specialization_callable_bindings[
                            parameter.name
                        ].canonical_identity
                        if parameter.kind == "callable"
                        and specialization_callable_bindings is not None
                        and parameter.name in specialization_callable_bindings
                        else (
                            str(parameter.type_name)
                            if parameter.kind == "constant"
                            else "fn("
                            + ",".join(map(str, parameter.callable_parameters))
                            + ")->"
                            + str(parameter.callable_return_type)
                        )
                    )
                )
            ),
        )
        for parameter in module.parameters
    )


def _signature_error(message: str, *, code: str = "ZL-INTERFACE-CONFORMANCE") -> None:
    raise SemanticError(
        message,
        code=code,
        fixes=("make the module public signature exactly match the named interface",),
    )


def _interface_clock_domains(
    declaration: ast.ModuleInterfaceDecl,
) -> tuple[ir_cdc.ClockDomain, ...]:
    clocks = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    resets = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain)
        for name, domain in (
            declaration.reset_domains
            or tuple((name, None) for name in declaration.resets)
        )
    )
    if len(set(declaration.clocks)) != len(declaration.clocks):
        _signature_error(
            f"module interface '{declaration.name}' has duplicate clock declarations"
        )
    bindings = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    if len({name for name, _ in bindings}) != len(bindings):
        _signature_error(
            f"module interface '{declaration.name}' has duplicate reset declarations"
        )
    if bool(declaration.clocks) != bool(bindings):
        _signature_error(
            f"module interface '{declaration.name}' must declare clock and reset together"
        )
    if not declaration.clocks:
        return ()
    if len(declaration.clocks) == 1:
        if len(bindings) != 1:
            _signature_error(
                f"module interface '{declaration.name}' requires exactly one reset"
            )
        reset, reset_domain = bindings[0]
        clock = declaration.clocks[0]
        if reset_domain is not None and reset_domain != clock:
            _signature_error(
                f"module interface reset '{reset}' references unknown domain "
                f"'{reset_domain}'"
            )
        if reset == clock:
            _signature_error("module interface clock and reset names must differ")
        clock_decl = next(item for item in clocks if item.name == clock)
        reset_decl = next(item for item in resets if item.name == reset)
        return (_clock_domain_from_source(clock_decl, reset_decl),)
    by_clock: dict[str, str] = {}
    for reset, domain in bindings:
        if domain is None or domain not in declaration.clocks:
            _signature_error(
                f"module interface reset '{reset}' requires a declared clock domain"
            )
        if domain in by_clock:
            _signature_error(
                f"module interface clock domain '{domain}' has multiple resets"
            )
        by_clock[domain] = reset
    missing = set(declaration.clocks) - by_clock.keys()
    if missing:
        _signature_error(
            f"module interface clock domain '{sorted(missing)[0]}' has no reset"
        )
    domains = tuple(
        _clock_domain_from_source(
            next(item for item in clocks if item.name == clock),
            next(
                item for item in resets
                if item.name == by_clock[clock]
            ),
        )
        for clock in declaration.clocks
    )
    _validate_async_reset_domain_scope(domains)
    return domains


def _validate_async_reset_domain_scope(
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
) -> None:
    """Keep the bounded async-reset contract single-domain everywhere."""

    if len(clock_domains) > 1 and any(
        domain.reset_mode is ir_cdc.ResetMode.ASYNCHRONOUS
        for domain in clock_domains
    ):
        raise SemanticError(
            "multi-domain asynchronous reset is not supported; use synchronous "
            "resets or isolate the asynchronous-reset domain in a child module"
        )


def _clock_domain_from_source(
    clock: ast.ClockPhysicalDecl,
    reset: ast.ResetPhysicalDecl,
    *,
    source_unit: str | None = None,
    source_digest: str | None = None,
) -> ir_cdc.ClockDomain:
    origin_span = reset.origin or clock.origin
    origin = (
        None
        if origin_span is None
        else SourceOrigin(
            origin_span,
            f"clock/reset domain {clock.name}",
            source_unit,
            source_digest,
        )
    )
    try:
        return ir_cdc.ClockDomain(
            clock.name,
            reset.name,
            ir_cdc.ClockEdge(clock.edge),
            ir_cdc.ResetMode(reset.mode),
            ir_cdc.ResetPolarity(reset.polarity),
            ir_cdc.PowerUpPolicy(reset.power_up),
            origin,
            ir_cdc.ResetReleaseMode(reset.release_mode),
            reset.release_cycles,
        )
    except ValueError as error:
        raise SemanticError(f"invalid reset contract for '{reset.name}': {error}") from error


def _inherit_applied_interface_surface(
    module: ast.Module,
    declaration: ast.ModuleInterfaceDecl,
) -> ast.Module:
    """Apply one complete named signature to an otherwise surface-free module.

    Interface conformance historically required a full declaration repeated in
    the module body.  Retain that spelling unchanged, but let a module with no
    public declarations inherit the *whole* applied signature.  Deliberately do
    not fill a partial declaration: the existing exact-conformance check then
    reports its missing/extra members instead of silently combining two ABIs.

    This is syntax normalization only.  The normal named-signature analysis
    still constructs and verifies the backend-independent ``ModuleSignature``.
    """

    if module.conforms_to is None:
        return module
    has_public_surface = bool(
        module.ports
        or module.clocks
        or module.resets
        or module.reset_domains
        or module.request_responses
        or module.aggregate_interfaces
        or module.timing is not None
    )
    if has_public_surface:
        return module

    parameters = declaration.parameters
    by_name = {item.name: item for item in parameters}
    applied: dict[str, int | str | ast.TypeSyntax] = {}
    positional = 0
    for argument in module.conforms_to.arguments:
        name = argument.name
        if name is None:
            if positional >= len(parameters):
                # Preserve the established structured diagnostic in the later
                # exact application check.
                return module
            name = parameters[positional].name
            positional += 1
        if name not in by_name or name in applied:
            return module
        applied[name] = argument.value
    for parameter in parameters:
        if parameter.name not in applied and parameter.default is not None:
            applied[parameter.name] = parameter.default

    def render(value: int | str | ast.TypeSyntax) -> str:
        if isinstance(value, ast.TypeName):
            return value.text
        if isinstance(value, ast.VectorTypeName):
            return f"vec<{value.length},{render(value.element_type)}>"
        if isinstance(value, ast.TupleTypeName):
            return f"({','.join(render(item) for item in value.elements)})"
        return str(value)

    def substitute_text(text: str) -> str:
        result = text
        for name in sorted(applied, key=lambda item: (-len(item), item)):
            result = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                render(applied[name]),
                result,
            )
        return result

    def substitute_type(value: ast.TypeSyntax) -> ast.TypeSyntax:
        if isinstance(value, ast.VectorTypeName):
            length: int | str = value.length
            if isinstance(length, str):
                replaced = substitute_text(length)
                length = int(replaced) if replaced.isdigit() else replaced
            return ast.VectorTypeName(length, substitute_type(value.element_type))
        if isinstance(value, ast.TupleTypeName):
            return ast.TupleTypeName(tuple(substitute_type(item) for item in value.elements))
        return ast.TypeName(substitute_text(value.text))

    def substitute_port_type(
        value: ast.PortTypeSyntax,
    ) -> ast.PortTypeSyntax:
        if not isinstance(value, ast.InterfaceTypeName):
            return substitute_type(value)
        return replace(value, payload_type=substitute_type(value.payload_type))

    inherited_ports = tuple(
        replace(port, type_name=substitute_port_type(port.type_name))
        for port in declaration.ports
    )
    inherited_aggregates = tuple(
        replace(
            endpoint,
            arguments=tuple(
                replace(
                    argument,
                    value=(
                        substitute_type(argument.value)
                        if isinstance(
                            argument.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
                        )
                        else (
                            int(substitute_text(str(argument.value)))
                            if substitute_text(str(argument.value)).isdigit()
                            else substitute_text(str(argument.value))
                        )
                    ),
                )
                for argument in endpoint.arguments
            ),
        )
        for endpoint in declaration.aggregate_interfaces
    )
    reset_domains = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    clock_physical = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    reset_physical = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in reset_domains
    )
    inherited_items: tuple[object, ...] = (
        *(
            ('clock', item.name, item)
            for item in clock_physical
        ),
        *(
            ('reset', item.name, item.clock, item)
            for item in reset_physical
        ),
        *inherited_ports,
        *inherited_aggregates,
        *((declaration.timing,) if declaration.timing is not None else ()),
    )
    return replace(
        module,
        ports=inherited_ports,
        clocks=declaration.clocks,
        resets=declaration.resets,
        reset_domains=reset_domains,
        clock_physical=clock_physical,
        reset_physical=reset_physical,
        request_responses=declaration.request_responses,
        aggregate_interfaces=inherited_aggregates,
        timing=declaration.timing,
        ordered_items=tuple((*inherited_items, *module.ordered_items)),
    )


def _applied_interface_parameters(
    source_module: ast.Module,
    declaration: ast.ModuleInterfaceDecl,
    reference: ast.ModuleInterfaceRef,
    resolver: _TypeResolver,
    specialization_type_bindings: dict[str, HardwareType] | None,
) -> tuple[
    tuple[ir_module.ModuleSignatureParameter, ...],
    dict[str, int],
    dict[str, HardwareType],
]:
    module_parameters = (
        source_module.declared_parameters or source_module.parameters
    )

    def normalized_defaults(
        parameters: tuple[ast.ModuleParameter, ...],
    ) -> dict[str, int | str | None]:
        """Normalize declaration defaults independently of an application.

        A specialized child carries concrete values in ``parameters`` while
        ``declared_parameters`` retains its public declaration.  Interface ABI
        identity must use the latter and must not distinguish equivalent
        spellings such as ``4`` and ``2 + 2``.
        """

        parameter_values = {
            item.name: item.default
            for item in parameters
            if item.kind == "value" and item.default is not None
        }
        default_resolver = _TypeResolver(
            (), (), (), parameters, parameter_values
        )
        result: dict[str, int | str | None] = {}
        for parameter in parameters:
            default = parameter.default
            if parameter.kind == "type" or default is None:
                result[parameter.name] = default
                continue
            result[parameter.name] = (
                default
                if isinstance(default, int)
                else default_resolver._eval_constant_integer(
                    str(default),
                    description=(
                        f"module parameter '{parameter.name}' declared default"
                    ),
                    allow_zero=True,
                    allow_negative=True,
                )
            )
        return result

    interface_defaults = normalized_defaults(declaration.parameters)
    module_defaults = normalized_defaults(module_parameters)
    expected_parameter_shape = tuple(
        (item.name, item.kind, interface_defaults[item.name])
        for item in declaration.parameters
    )
    actual_parameter_shape = tuple(
        (item.name, item.kind, module_defaults[item.name])
        for item in module_parameters
    )
    if actual_parameter_shape != expected_parameter_shape:
        _signature_error(
            f"module '{source_module.name}' parameter contract "
            f"{actual_parameter_shape} does not exactly match interface "
            f"'{declaration.name}' {expected_parameter_shape}",
            code="ZL-INTERFACE-PARAMETERS",
        )

    by_name = {item.name: item for item in declaration.parameters}
    assigned: dict[str, object] = {}
    positional = 0
    for argument in reference.arguments:
        if argument.name is None:
            while (
                positional < len(declaration.parameters)
                and declaration.parameters[positional].name in assigned
            ):
                positional += 1
            if positional >= len(declaration.parameters):
                _signature_error(
                    f"too many interface arguments for '{declaration.name}'",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            name = declaration.parameters[positional].name
            positional += 1
        else:
            name = argument.name
        if name not in by_name:
            _signature_error(
                f"unknown interface parameter '{name}' on '{declaration.name}'",
                code="ZL-INTERFACE-PARAMETERS",
            )
        if name in assigned:
            _signature_error(
                f"interface parameter '{name}' is assigned more than once",
                code="ZL-INTERFACE-PARAMETERS",
            )
        assigned[name] = argument.value

    value_bindings: dict[str, int] = {}
    type_bindings: dict[str, HardwareType] = {}
    records: list[ir_module.ModuleSignatureParameter] = []
    for parameter in declaration.parameters:
        value = assigned.get(parameter.name, parameter.default)
        if value is None:
            _signature_error(
                f"missing required interface argument '{parameter.name}' for "
                f"'{declaration.name}'",
                code="ZL-INTERFACE-PARAMETERS",
            )
        if parameter.kind == "type":
            if isinstance(value, int):
                _signature_error(
                    f"interface type parameter '{parameter.name}' requires a type",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            syntax = value if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)) else ast.TypeName(str(value))
            try:
                resolved = resolver.resolve(syntax)
            except SemanticError as error:
                _signature_error(
                    f"cannot resolve interface type argument '{parameter.name}': {error}",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            type_bindings[parameter.name] = resolved
            records.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name, "type", str(resolved), None
                )
            )
            continue
        if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
            _signature_error(
                f"interface value parameter '{parameter.name}' requires an integer",
                code="ZL-INTERFACE-PARAMETERS",
            )
        try:
            resolved_value = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"interface value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
        except SemanticError as error:
            _signature_error(
                f"cannot resolve interface value argument '{parameter.name}': {error}",
                code="ZL-INTERFACE-PARAMETERS",
            )
        assert isinstance(resolved_value, int)
        declared_default = interface_defaults[parameter.name]
        value_bindings[parameter.name] = resolved_value
        records.append(
            ir_module.ModuleSignatureParameter(
                parameter.name, "value", resolved_value, declared_default
            )
        )

    effective_types = specialization_type_bindings or {}
    effective_parameters: list[ir_module.ModuleSignatureParameter] = []
    effective_source_parameters = {
        item.name: item for item in source_module.parameters
    }
    for parameter in module_parameters:
        if parameter.kind == "type":
            resolved = effective_types.get(parameter.name)
            if resolved is None:
                try:
                    resolved = resolver.resolve(ast.TypeName(parameter.name))
                except SemanticError:
                    _signature_error(
                        f"module type parameter '{parameter.name}' is not specialized",
                        code="ZL-INTERFACE-PARAMETERS",
                    )
            effective_parameters.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name, "type", str(resolved), None
                )
            )
        else:
            effective = effective_source_parameters.get(parameter.name, parameter)
            if effective.default is None:
                _signature_error(
                    f"module value parameter '{parameter.name}' is not specialized",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            value = effective.default
            assert value is not None
            resolved_value = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"module value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
            effective_parameters.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name,
                    "value",
                    resolved_value,
                    module_defaults[parameter.name],
                )
            )
    if tuple(effective_parameters) != tuple(records):
        _signature_error(
            f"module '{source_module.name}' applied parameter values "
            f"{tuple(effective_parameters)} do not match interface "
            f"'{declaration.name}' {tuple(records)}",
            code="ZL-INTERFACE-PARAMETERS",
        )
    return tuple(records), value_bindings, type_bindings


def _signature_port(
    declaration: ast.PortDecl,
    name: str,
    resolver: _TypeResolver,
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
) -> ir_module.Port:
    if declaration.initializer is not None:
        _signature_error(
            f"module interface port '{name}' cannot have an initializer"
        )
    syntax = declaration.type_name
    if isinstance(syntax, ast.InterfaceTypeName):
        protocol = {
            ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE,
            ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID,
            ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT,
            ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET,
            ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT,
        }[syntax.kind]
        type_syntax = syntax.payload_type
        capacity = syntax.capacity
        virtual_channels = syntax.virtual_channels
    else:
        protocol = InterfaceProtocol.WIRE
        type_syntax = syntax
        capacity = None
        virtual_channels = None
    default_domain = clock_domains[0].clock if len(clock_domains) == 1 else None
    domain = declaration.domain or default_domain
    declared_domains = {item.clock for item in clock_domains}
    if declaration.domain is not None and declaration.domain not in declared_domains:
        _signature_error(
            f"module interface port '{name}' references unknown domain "
            f"'{declaration.domain}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if len(clock_domains) > 1 and domain is None:
        _signature_error(
            f"module interface port '{name}' requires an explicit domain",
            code="ZL-INTERFACE-DOMAIN",
        )
    return ir_module.Port(
        ir_module.PortDirection.INPUT
        if declaration.direction is ast.Direction.INPUT
        else ir_module.PortDirection.OUTPUT,
        name,
        resolver.resolve(type_syntax),
        protocol,
        capacity,
        domain,
        virtual_channels,
    )


def _signature_aggregate_endpoint(
    aggregate: ast.AggregateInterfaceDecl,
    *,
    protocols: tuple[ast.ProtocolDecl, ...],
    source_module: ast.Module,
    resolver: _TypeResolver,
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
    identity_namespace: str,
) -> ir_module.AggregateProtocolEndpoint:
    declaration = next(
        (item for item in protocols if item.name == aggregate.protocol), None
    )
    if declaration is None:
        _signature_error(
            f"unknown protocol '{aggregate.protocol}' in module interface",
            code="ZL-INTERFACE-PROTOCOL",
        )
    assert declaration is not None
    if aggregate.role not in declaration.roles:
        _signature_error(
            f"protocol '{aggregate.protocol}' has no role '{aggregate.role}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    arguments: dict[str, int | str] = {
        item.name: item.default
        for item in declaration.parameters
        if item.default is not None
    }
    type_arguments: dict[str, HardwareType] = {}
    assigned: set[str] = set()
    positional = 0
    for argument in aggregate.arguments:
        if argument.name is None:
            if positional >= len(declaration.parameters):
                _signature_error(
                    f"too many arguments for protocol '{aggregate.protocol}'",
                    code="ZL-INTERFACE-PROTOCOL",
                )
            parameter = declaration.parameters[positional]
            positional += 1
        else:
            parameter = next(
                (
                    item for item in declaration.parameters
                    if item.name == argument.name
                ),
                None,
            )
            if parameter is None:
                _signature_error(
                    f"unknown protocol parameter '{argument.name}'",
                    code="ZL-INTERFACE-PROTOCOL",
                )
        assert parameter is not None
        if parameter.name in assigned:
            _signature_error(
                f"protocol parameter '{parameter.name}' is assigned more than once",
                code="ZL-INTERFACE-PROTOCOL",
            )
        assigned.add(parameter.name)
        if parameter.kind == "type":
            value = argument.value
            syntax = value if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)) else ast.TypeName(str(value))
            type_arguments[parameter.name] = resolver.resolve(syntax)
        else:
            value = argument.value
            if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
                _signature_error(
                    f"protocol value parameter '{parameter.name}' requires an integer",
                    code="ZL-INTERFACE-PROTOCOL",
                )
            arguments[parameter.name] = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"protocol parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
    missing = next(
        (
            item for item in declaration.parameters
            if item.kind == "type" and item.name not in type_arguments
            or item.kind == "value" and item.name not in arguments
        ),
        None,
    )
    if missing is not None:
        _signature_error(
            f"missing protocol argument '{missing.name}' for '{aggregate.protocol}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    specialized = _TypeResolver(
        source_module.type_aliases,
        source_module.structs,
        source_module.enums,
        declaration.parameters,
        arguments,
        type_arguments,
        identity_namespace,
        tagged_unions=source_module.tagged_unions,
    )
    default_domain = clock_domains[0].clock if len(clock_domains) == 1 else None
    endpoint_domain = aggregate.domain or default_domain
    declared_domains = {item.clock for item in clock_domains}
    if aggregate.domain is not None and aggregate.domain not in declared_domains:
        _signature_error(
            f"aggregate endpoint '{aggregate.name}' references unknown domain "
            f"'{aggregate.domain}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if len(clock_domains) > 1 and endpoint_domain is None:
        _signature_error(
            f"aggregate endpoint '{aggregate.name}' requires an explicit domain",
            code="ZL-INTERFACE-DOMAIN",
        )
    members: list[ir_module.ProtocolMember] = []
    seen_members: set[str] = set()
    for channel in declaration.channels:
        if channel.name in seen_members:
            _signature_error(
                f"protocol '{declaration.name}' has duplicate member '{channel.name}'",
                code="ZL-INTERFACE-PROTOCOL",
            )
        seen_members.add(channel.name)
        syntax = channel.type_name
        if isinstance(syntax, ast.InterfaceTypeName):
            protocol = {
                ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE,
                ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID,
                ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT,
                ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET,
                ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT,
            }[syntax.kind]
            payload = specialized.resolve(syntax.payload_type)
        else:
            protocol = InterfaceProtocol.WIRE
            payload = specialized.resolve(syntax)
        member_domain = channel.domain or endpoint_domain
        if member_domain is not None and member_domain not in declared_domains:
            _signature_error(
                f"protocol member '{aggregate.name}.{channel.name}' references "
                f"unknown domain '{member_domain}'",
                code="ZL-INTERFACE-DOMAIN",
            )
        members.append(
            ir_module.ProtocolMember(
                channel.name,
                protocol,
                payload,
                channel.source_role,
                channel.sink_role,
                member_domain,
            )
        )
    specialization_identity = (
        f"{aggregate.protocol}<"
        + ",".join(
            f"{parameter.name}="
            f"{type_arguments.get(parameter.name, arguments.get(parameter.name, parameter.default))}"
            for parameter in declaration.parameters
        )
        + ">"
    )
    return ir_module.AggregateProtocolEndpoint(
        aggregate.name,
        aggregate.protocol,
        aggregate.role,
        tuple(members),
        endpoint_domain,
        specialization_identity,
    )


def _named_module_signature(
    source_module: ast.Module,
    *,
    resolver: _TypeResolver,
    specialization_type_bindings: dict[str, HardwareType] | None,
    actual_ports: tuple[ir_module.Port, ...],
    actual_clock_domains: tuple[ir_cdc.ClockDomain, ...],
    actual_request_responses: tuple[ir_module.RequestResponseInterface, ...],
    actual_aggregate_endpoints: tuple[ir_module.AggregateProtocolEndpoint, ...],
    actual_timing: ir_timing.ModuleTimingContract | None,
    source_unit: str | None,
    source_digest: str | None,
    source_digests: dict[str, str],
) -> ir_module.ModuleSignature | None:
    reference = source_module.conforms_to
    if reference is None:
        return None
    declarations = {
        item.name: item for item in source_module.module_interfaces
    }
    if len(declarations) != len(source_module.module_interfaces):
        duplicate = next(
            item.name for item in source_module.module_interfaces
            if sum(other.name == item.name for other in source_module.module_interfaces) > 1
        )
        _signature_error(
            f"duplicate module interface declaration '{duplicate}'",
            code="ZL-INTERFACE-DUPLICATE",
        )
    declaration = declarations.get(reference.name)
    if declaration is None:
        _signature_error(
            f"unknown module interface '{reference.name}'",
            code="ZL-INTERFACE-UNKNOWN",
        )
    assert declaration is not None
    declaration_digest = (
        source_digests.get(declaration.source_identity)
        if declaration.source_identity is not None
        else source_digest
    )
    if declaration.request_responses:
        _signature_error(
            "named module interfaces do not yet accept request_response members "
            "because the requester/responder role is inferred from behavior",
            code="ZL-INTERFACE-UNSUPPORTED",
        )
    explicit_resets = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    module_resets = source_module.reset_domains or tuple(
        (name, None) for name in source_module.resets
    )
    if declaration.clocks != source_module.clocks or explicit_resets != module_resets:
        _signature_error(
            f"module '{source_module.name}' clock/reset declarations do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    expected_clock_physical = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    expected_reset_physical = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in explicit_resets
    )
    actual_clock_physical = source_module.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in source_module.clocks
    )
    actual_reset_physical = source_module.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in module_resets
    )
    if (
        expected_clock_physical != actual_clock_physical
        or expected_reset_physical != actual_reset_physical
    ):
        _signature_error(
            f"module '{source_module.name}' physical clock/reset declarations do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    clock_domains = _interface_clock_domains(declaration)
    parameters, parameter_values, parameter_types = _applied_interface_parameters(
        source_module,
        declaration,
        reference,
        resolver,
        specialization_type_bindings,
    )
    interface_resolver = _TypeResolver(
        source_module.type_aliases,
        source_module.structs,
        source_module.enums,
        declaration.parameters,
        parameter_values,
        parameter_types,
        declaration.source_identity or source_unit or source_module.name,
        tagged_unions=source_module.tagged_unions,
    )
    expected_ports: list[ir_module.Port] = []
    for port in declaration.ports:
        names = port.names or (port.name,)
        if port.initializer is not None:
            _signature_error(
                f"module interface port '{port.name}' cannot have an initializer"
            )
        for name in names:
            expected_ports.append(
                _signature_port(port, name, interface_resolver, clock_domains)
            )
    duplicate_port = next(
        (
            port.name for port in expected_ports
            if sum(other.name == port.name for other in expected_ports) > 1
        ),
        None,
    )
    if duplicate_port is not None:
        _signature_error(
            f"module interface '{declaration.name}' has duplicate port "
            f"'{duplicate_port}'"
        )
    expected_aggregates = tuple(
        _signature_aggregate_endpoint(
            endpoint,
            protocols=source_module.protocols,
            source_module=source_module,
            resolver=interface_resolver,
            clock_domains=clock_domains,
            identity_namespace=(
                declaration.source_identity or source_unit or source_module.name
            ),
        )
        for endpoint in declaration.aggregate_interfaces
    )
    if len({item.name for item in expected_aggregates}) != len(expected_aggregates):
        _signature_error(
            f"module interface '{declaration.name}' has duplicate aggregate endpoints"
        )
    timing: ir_timing.ModuleTimingContract | None = None
    if declaration.timing is not None:
        if declaration.timing.initiation_interval != 1:
            _signature_error(
                "module interface timing currently requires ii 1",
                code="ZL-INTERFACE-TIMING",
            )
        if declaration.timing.latency > 0 and len(clock_domains) != 1:
            _signature_error(
                "positive module interface latency requires one clock/reset domain",
                code="ZL-INTERFACE-TIMING",
            )
        domain = clock_domains[0] if len(clock_domains) == 1 else None
        origin = (
            SourceOrigin(
                declaration.timing.origin,
                f"module interface timing {declaration.name}",
                declaration.source_identity or source_unit,
                declaration_digest,
            )
            if declaration.timing.origin is not None
            else None
        )
        timing = ir_timing.ModuleTimingContract(
            declaration.timing.latency,
            declaration.timing.initiation_interval,
            None if domain is None else domain.clock,
            None if domain is None else domain.reset,
            origin,
        )
    source_origin = (
        SourceOrigin(
            declaration.origin,
            f"module interface {declaration.name}",
            declaration.source_identity or source_unit,
            declaration_digest,
        )
        if declaration.origin is not None
        else None
    )
    expected = ir_module.ModuleSignature(
        declaration.name,
        parameters,
        tuple(expected_ports),
        clock_domains,
        (),
        expected_aggregates,
        timing,
        (
            f"{declaration.source_identity or source_unit or 'compilation:' + source_module.name}"
            f"::interface::{declaration.name}"
        ),
        source_origin,
    )
    actual = ir_module.ModuleSignature(
        declaration.name,
        parameters,
        actual_ports,
        actual_clock_domains,
        actual_request_responses,
        actual_aggregate_endpoints,
        actual_timing,
        expected.declaration_identity,
    )
    if actual.ports != expected.ports:
        expected_by_name = {item.name: item for item in expected.ports}
        actual_by_name = {item.name: item for item in actual.ports}
        missing = tuple(name for name in expected_by_name if name not in actual_by_name)
        extra = tuple(name for name in actual_by_name if name not in expected_by_name)
        if missing or extra:
            _signature_error(
                f"module '{source_module.name}' port set differs from interface "
                f"'{declaration.name}': missing={missing}, extra={extra}"
            )
        expected_order = tuple(item.name for item in expected.ports)
        actual_order = tuple(item.name for item in actual.ports)
        if actual_order != expected_order:
            _signature_error(
                f"module '{source_module.name}' port order {actual_order} does "
                f"not match interface '{declaration.name}' {expected_order}"
            )
        mismatch = next(
            name for name in expected_by_name
            if expected_by_name[name] != actual_by_name[name]
        )
        _signature_error(
            f"module '{source_module.name}' port '{mismatch}' is "
            f"{actual_by_name[mismatch]}, expected exact {expected_by_name[mismatch]}"
        )
    if actual.clock_domains != expected.clock_domains:
        _signature_error(
            f"module '{source_module.name}' clock/reset domains do not match "
            f"interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if actual.request_responses != expected.request_responses:
        _signature_error(
            f"module '{source_module.name}' request_response ABI is not declared "
            f"by interface '{declaration.name}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    if actual.aggregate_protocol_endpoints != expected.aggregate_protocol_endpoints:
        _signature_error(
            f"module '{source_module.name}' aggregate protocol endpoints do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    if actual.timing_contract != expected.timing_contract:
        _signature_error(
            f"module '{source_module.name}' timing contract "
            f"{actual.timing_contract} does not match interface "
            f"'{declaration.name}' {expected.timing_contract}",
            code="ZL-INTERFACE-TIMING",
        )
    return expected


def _validate_value_parameter_shadowing(module: ast.Module) -> None:
    parameters = {
        item.name: item.kind
        for item in module.parameters
        if item.kind in {"value", "constant", "callable"}
    }
    if not parameters:
        return
    declarations = [
        *(item.name for item in module.ports),
        *(item.name for item in module.registers),
        *(item.name for item in module.fifos),
        *(item.name for item in module.memories),
        *(item.name for item in module.roms),
        *(item.name for item in module.instances),
    ]
    port_names = {item.name for item in module.ports}
    declarations.extend(
        item.target for item in module.assignments
        if "." not in item.target and item.target not in port_names
    )
    conflict = next((name for name in declarations if name in parameters), None)
    if conflict is not None:
        kind = parameters[conflict]
        label = (
            "module value parameter"
            if kind == "value"
            else f"compile-time module {kind} parameter"
        )
        raise SemanticError(
            f"declaration '{conflict}' conflicts with {label} '{conflict}'"
        )


def _validate_compile_time_parameter_declarations(module: ast.Module) -> None:
    """Validate the bounded typed/callable parameter surface once."""

    def validate(
        owner: str,
        parameters: tuple[ast.ModuleParameter, ...],
    ) -> None:
        names = tuple(item.name for item in parameters)
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise SemanticError(
                f"duplicate compile-time parameter '{duplicate}' in {owner}"
            )
        shaped_phase = True
        for index, parameter in enumerate(parameters):
            if parameter.kind == "value" and isinstance(parameter.default, str):
                try:
                    default_tree = pyast.parse(parameter.default, mode="eval")
                except SyntaxError as error:
                    raise SemanticError(
                        f"invalid default for value parameter '{parameter.name}' "
                        f"in {owner}"
                    ) from error
                intrinsic_names = {
                    node.func.id
                    for node in pyast.walk(default_tree)
                    if isinstance(node, pyast.Call)
                    and isinstance(node.func, pyast.Name)
                }
                references = {
                    node.id
                    for node in pyast.walk(default_tree)
                    if isinstance(node, pyast.Name)
                    and node.id not in intrinsic_names
                }
                invalid = next(
                    (
                        name
                        for name in names[index:]
                        if name in references
                    ),
                    None,
                )
                if invalid is not None:
                    raise SemanticError(
                        f"value parameter '{parameter.name}' in {owner} default "
                        f"references later parameter '{invalid}'; defaults may "
                        "reference only earlier parameters",
                        code="ZL-SEMANTIC-PARAMETER-DEFAULT",
                    )
            if parameter.kind in {"constant", "callable"}:
                shaped_phase = False
                if parameter.default is not None:
                    raise SemanticError(
                        f"compile-time {parameter.kind} parameter "
                        f"'{parameter.name}' in {owner} cannot have a default"
                    )
                if parameter.kind == "constant" and parameter.type_name is None:
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' in "
                        f"{owner} requires an exact type"
                    )
                if (
                    parameter.kind == "callable"
                    and parameter.callable_return_type is None
                ):
                    raise SemanticError(
                        f"compile-time callable parameter '{parameter.name}' in "
                        f"{owner} requires an exact return type"
                    )
            elif not shaped_phase:
                raise SemanticError(
                    f"type/integer parameter '{parameter.name}' in {owner} must "
                    "precede typed constant and callable parameters"
                )

    validate(f"module '{module.name}'", module.parameters)
    for declaration in module.submodules:
        validate(f"module '{declaration.name}'", declaration.parameters)
    for declaration in module.functions:
        validate(f"function '{declaration.name}'", declaration.generic_parameters)
    for declaration in module.operators:
        validate(
            f"operator '{declaration.operator}'", declaration.generic_parameters
        )
    for kind, declarations in (
        ("struct", module.structs),
        ("protocol", module.protocols),
        ("module interface", module.module_interfaces),
    ):
        for declaration in declarations:
            unsupported = next(
                (
                    item for item in declaration.parameters
                    if item.kind in {"constant", "callable"}
                ),
                None,
            )
            if unsupported is not None:
                raise SemanticError(
                    f"{kind} '{declaration.name}' does not accept compile-time "
                    f"{unsupported.kind} parameters in this first slice"
                )


def _analyze_csr_blocks(
    declarations: tuple[ast.CsrBlockDecl, ...],
    type_resolver: _TypeResolver,
    symbols: dict[str, ir_module.Port],
    *,
    module_identity: str,
    clock_domain: str | None,
    reset_domain: str | None,
    source_unit: str | None,
    source_digest: str | None,
) -> tuple[ir_csr.CsrBlock, ...]:
    blocks: list[ir_csr.CsrBlock] = []
    block_names: set[str] = set()
    absolute_addresses: set[int] = set()
    bound_command_outputs: set[str] = set()
    for block_ordinal, declaration in enumerate(declarations):
        block_identity = ir_csr.CsrBlockIdentity(module_identity, block_ordinal)
        block_origin = (
            SourceOrigin(
                declaration.origin,
                f"csr block {declaration.name}",
                source_unit,
                source_digest,
            )
            if declaration.origin is not None else None
        )
        if declaration.name in block_names:
            raise SemanticError(f"duplicate CSR block '{declaration.name}'")
        block_names.add(declaration.name)
        if declaration.base_address % 4:
            raise SemanticError(
                f"CSR block '{declaration.name}' base address must be 4-byte aligned"
            )
        if not 0 <= declaration.base_address <= 0xFFFF_FFFF:
            raise SemanticError(
                f"CSR block '{declaration.name}' base address does not fit 32 bits"
            )
        register_names: set[str] = set()
        offsets: set[int] = set()
        registers: list[ir_csr.CsrRegister] = []
        state_bindings: list[ir_csr.CsrFieldStateBinding] = []
        for register_ordinal, register in enumerate(declaration.registers):
            register_identity = ir_csr.CsrRegisterIdentity(
                block_identity, register_ordinal
            )
            register_origin = (
                SourceOrigin(
                    register.origin,
                    f"CSR register {declaration.name}.{register.name}",
                    source_unit,
                    source_digest,
                )
                if register.origin is not None else block_origin
            )
            if register.name in register_names:
                raise SemanticError(
                    f"duplicate register '{register.name}' in CSR block "
                    f"'{declaration.name}'"
                )
            register_names.add(register.name)
            if register.offset % 4:
                raise SemanticError(
                    f"CSR register '{register.name}' offset must be 4-byte aligned"
                )
            if register.offset in offsets:
                raise SemanticError(
                    f"duplicate CSR register offset 0x{register.offset:x}"
                )
            offsets.add(register.offset)
            address = declaration.base_address + register.offset
            if address > 0xFFFF_FFFF:
                raise SemanticError(
                    f"CSR register '{register.name}' address does not fit 32 bits"
                )
            if address in absolute_addresses:
                raise SemanticError(f"overlapping CSR address 0x{address:08x}")
            absolute_addresses.add(address)
            if not register.fields:
                raise SemanticError(f"CSR register '{register.name}' has no fields")
            field_names: set[str] = set()
            occupied_bits: set[int] = set()
            next_lsb = 0
            fields: list[ir_csr.CsrField] = []
            for field_ordinal, field in enumerate(register.fields):
                field_identity = ir_csr.CsrFieldIdentity(
                    register_identity, field_ordinal
                )
                field_origin = (
                    SourceOrigin(
                        field.origin,
                        f"CSR field {declaration.name}.{register.name}.{field.name}",
                        source_unit,
                        source_digest,
                    )
                    if field.origin is not None else register_origin
                )
                if field.name in field_names:
                    raise SemanticError(
                        f"duplicate field '{field.name}' in CSR register "
                        f"'{register.name}'"
                    )
                field_names.add(field.name)
                type_ = type_resolver.resolve(field.type_name)
                if not isinstance(type_, (BitType, UIntType, BitsType)):
                    raise SemanticError(
                        f"CSR field '{field.name}' requires bit, unsigned, or bits type"
                    )
                if field.msb is None:
                    lsb = next_lsb
                    msb = lsb + type_.width - 1
                else:
                    if field.lsb is None:
                        raise SemanticError(
                            f"CSR field '{field.name}' has an incomplete bit position"
                        )
                    msb = field.msb
                    lsb = field.lsb
                    if msb < lsb:
                        raise SemanticError(
                            f"CSR field '{field.name}' bit range must be msb:lsb"
                        )
                    if msb - lsb + 1 != type_.width:
                        raise SemanticError(
                            f"CSR field '{field.name}' range width does not match "
                            f"{type_}"
                        )
                if msb >= 32:
                    raise SemanticError(
                        f"CSR field '{field.name}' exceeds 32-bit register width"
                    )
                bits = set(range(lsb, msb + 1))
                if bits & occupied_bits:
                    raise SemanticError(
                        f"CSR field '{field.name}' overlaps another field in "
                        f"'{register.name}'"
                    )
                occupied_bits.update(bits)
                next_lsb = max(next_lsb, msb + 1)
                reset = field.reset if field.reset is not None else 0
                if reset >= (1 << type_.width):
                    raise SemanticError(
                        f"reset value for CSR field '{field.name}' does not fit {type_}"
                    )
                access = ir_csr.CsrAccess(field.access.value)
                if access is ir_csr.CsrAccess.RESERVED and field.reset is not None:
                    raise SemanticError(
                        f"reserved CSR field '{field.name}' must not have a reset value"
                    )
                if access is ir_csr.CsrAccess.PULSE and reset != 0:
                    raise SemanticError(
                        f"pulse CSR field '{field.name}' must reset to zero"
                    )
                binding: ir_csr.CsrHardwareBinding | None = None
                if field.binding is not None:
                    signal_name = field.binding.signal
                    port = symbols.get(signal_name)
                    if port is None and "." in signal_name:
                        port = symbols.get(signal_name.replace(".", "_"))
                    if port is None:
                        raise SemanticError(
                            f"CSR field '{field.name}' references unknown hardware "
                            f"signal '{signal_name}'"
                        )
                    if port.protocol is not InterfaceProtocol.WIRE:
                        raise SemanticError(
                            f"CSR hardware signal '{port.name}' must be a wire port"
                        )
                    if port.type != type_:
                        raise SemanticError(
                            f"CSR field '{field.name}' has type {type_}, but hardware "
                            f"signal '{port.name}' has type {port.type}"
                        )
                    kind = ir_csr.CsrBindingKind(field.binding.kind.value)
                    if kind is ir_csr.CsrBindingKind.STATUS:
                        if access is not ir_csr.CsrAccess.READ_ONLY:
                            raise SemanticError(
                                "direct CSR status bindings require ro access"
                            )
                        if port.direction is not ir_module.PortDirection.INPUT:
                            raise SemanticError(
                                f"CSR status signal '{port.name}' must be an input"
                            )
                        if field.reset is not None:
                            raise SemanticError(
                                f"hardware-driven status field '{field.name}' must "
                                "not declare a reset"
                            )
                    elif kind is ir_csr.CsrBindingKind.STICKY:
                        if access is not ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                            raise SemanticError(
                                "sticky CSR bindings require w1c access"
                            )
                        if port.direction is not ir_module.PortDirection.INPUT:
                            raise SemanticError(
                                f"CSR sticky event '{port.name}' must be an input"
                            )
                    else:
                        if access not in {
                            ir_csr.CsrAccess.PULSE,
                            ir_csr.CsrAccess.WRITE_ONLY,
                        }:
                            raise SemanticError(
                                "CSR command bindings require pulse or wo access"
                            )
                        if port.direction is not ir_module.PortDirection.OUTPUT:
                            raise SemanticError(
                                f"CSR command signal '{port.name}' must be an output"
                            )
                        if port.name in bound_command_outputs:
                            raise SemanticError(
                                f"CSR command output '{port.name}' is bound more than once"
                            )
                        bound_command_outputs.add(port.name)
                    priority = (
                        ir_csr.CsrPriority(field.binding.priority.value)
                        if field.binding.priority is not None
                        else None
                    )
                    binding = ir_csr.CsrHardwareBinding(kind, port.name, priority)
                typed_field = ir_csr.CsrField(
                    field.name, type_, access, msb, lsb, reset, binding,
                    field_identity, field_origin,
                )
                fields.append(typed_field)
                if access in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
                    ir_csr.CsrAccess.PULSE,
                }:
                    state_bindings.append(ir_csr.CsrFieldStateBinding(
                        field_identity,
                        access,
                        type_,
                        typed_field.width,
                        32,
                        reset,
                        field_origin,
                        f"csr-state:{field_identity.render()}",
                        clock_domain,
                        reset_domain,
                    ))
            registers.append(
                ir_csr.CsrRegister(
                    register.name, register.offset, tuple(fields),
                    register_identity, register_origin,
                )
            )
        if not registers:
            raise SemanticError(f"CSR block '{declaration.name}' has no registers")
        typed_block = ir_csr.CsrBlock(
            declaration.name,
            declaration.base_address,
            tuple(registers),
            block_identity,
            block_origin,
            tuple(state_bindings),
        )
        ir_csr.validate_state_bindings(typed_block)
        blocks.append(typed_block)
    return tuple(blocks)


@dataclass(frozen=True)
class _FunctionSignature:
    declaration: ast.FunctionDecl
    parameters: tuple[ir_module.FunctionParameter, ...]
    return_type: HardwareType


@dataclass(frozen=True)
class _FunctionPrototype:
    """One ordinary function before an omitted return has been inferred."""

    declaration: ast.FunctionDecl
    parameters: tuple[ir_module.FunctionParameter, ...]
    declared_return_type: HardwareType | None


@dataclass
class _FunctionCatalog:
    """Compilation-local, demand-driven ordinary-function signatures.

    An inferred return is checked without a caller-supplied expected type.  The
    probe uses private mutable registries and is discarded; the retained body
    is checked later with the final stable callable identity.  This keeps the
    established canonical/backend IR unchanged while allowing forward calls.
    """

    prototypes: dict[str, _FunctionPrototype]
    signatures: dict[str, _FunctionSignature]
    context: "_ExpressionContext | None" = None
    resolving: list[str] = field(default_factory=list)

    def resolve(
        self,
        name: str,
        *,
        call_origin: SourceOrigin | None = None,
    ) -> _FunctionSignature | None:
        existing = self.signatures.get(name)
        if existing is not None:
            return existing
        prototype = self.prototypes.get(name)
        if prototype is None:
            return None
        if name in self.resolving:
            start = self.resolving.index(name)
            cycle_names = (*self.resolving[start:], name)
            cycle = " -> ".join(cycle_names)
            notes: list[str] = []
            for item in dict.fromkeys(cycle_names):
                declaration = self.prototypes[item].declaration
                if declaration.origin is None:
                    continue
                source_prefix = (
                    f"{declaration.source_identity}:"
                    if declaration.source_identity is not None
                    else ""
                )
                notes.append(
                    f"function '{item}' declared at {source_prefix}"
                    f"{declaration.origin.render()}"
                )
            raise SemanticError(
                f"recursive inferred return cycle: {cycle}",
                code="ZL-FUNCTION-RETURN-CYCLE",
                primary=call_origin,
                notes=tuple(notes),
            )
        if self.context is None:
            raise SemanticError("internal function catalog has no expression context")

        self.resolving.append(name)
        try:
            # Return probing must not publish generic specializations, consume
            # the selected module's logical elaboration budget, or retain a
            # temporary functional-binder identity.
            probe_context = replace(
                self.context,
                generic_specializations=[],
                function_definitions={},
                callable_definitions=dict(self.context.callable_definitions),
                callable_use_counts={},
                specializations_in_progress=set(),
                specialization_budget_costs={},
                compile_time_real_quantize_cache={},
                compile_time_budget=_CompileTimeBudget(),
                functional_binder_ordinals={},
                next_functional_binder_ordinal=[0],
                functional_binder_nesting=(),
                exploration_results=None,
            )
            probe_context = _context_for_callable_body(
                probe_context,
                prototype.declaration.source_identity,
                f"return-probe:{name}",
            )
            symbols = {
                parameter.name: parameter for parameter in prototype.parameters
            }
            body = _check_callable_body(
                prototype.declaration,
                symbols,
                None,
                probe_context,
                typed_boundary=False,
            )
            signature = _FunctionSignature(
                prototype.declaration,
                prototype.parameters,
                body.type,
            )
            self.signatures[name] = signature
            return signature
        finally:
            self.resolving.pop()


@dataclass(frozen=True)
class _StaticCallableBinding:
    """One compile-time function parameter after exact specialization."""

    reference: ast.CallableRef
    parameter_types: tuple[HardwareType, ...]
    return_type: HardwareType
    callee_identity: str
    # Generic callable arguments are fully specialized at the binding site.
    # Carry their typed definition closure into recursively analyzed child
    # modules so a use never re-specializes under a different dependency
    # context (and therefore can never acquire a different identity).
    definitions: tuple[ir_module.Function, ...] = ()

    @property
    def canonical_identity(self) -> str:
        """Compatibility spelling for the exact concrete callee identity."""

        return self.callee_identity


def _static_callable_definition_closure(
    root: ir_expr.Call,
    context: "_ExpressionContext",
) -> tuple[ir_module.Function, ...]:
    """Return the concrete generic-definition closure selected by ``root``.

    Ordinary named functions remain owned by ``Module.functions``.  Only
    monomorphic generic/operator definitions need to travel with a static
    callable binding across recursive module analysis.
    """

    definitions = tuple((
        *context.function_definitions.values(),
        *context.callable_definitions.values(),
    ))
    try:
        reachable = reachable_callable_definitions(definitions, (root,))
    except CallableReachabilityError as error:
        raise SemanticError(
            f"invalid statically selected callable graph: {error}"
        ) from error
    generic_identities = set(context.callable_definitions)
    return tuple(
        definition
        for definition in reachable
        if definition.callee_identity in generic_identities
    )


def _inherited_static_callable_definitions(
    bindings: dict[str, _StaticCallableBinding] | None,
) -> dict[str, ir_module.Function]:
    """Merge exact callable closures carried by module parameters."""

    result: dict[str, ir_module.Function] = {}
    for binding in (bindings or {}).values():
        for definition in binding.definitions:
            previous = result.get(definition.callee_identity)
            if previous is not None and previous != definition:
                raise SemanticError(
                    "statically selected callable identity has conflicting "
                    "concrete definitions"
                )
            result[definition.callee_identity] = definition
    return result


def _constant_specialization_binding(
    name: str,
    expression: ir_expr.Expression,
    dependency_identity: tuple[tuple[str, str], ...],
) -> ir_module.SpecializationBinding:
    value = _canonical_rom_runtime_values(
        expression.type,
        constant_runtime_value(expression),
    )
    content_hash = stable_digest({
        "schema": _COMPILE_TIME_EVALUATOR_SCHEMA,
        "kind": ir_module.SpecializationBindingKind.CONSTANT.value,
        "type": str(expression.type),
        "value": value,
        "dependencies": dependency_identity,
    })
    return ir_module.SpecializationBinding(
        name,
        ir_module.SpecializationBindingKind.CONSTANT,
        expression.type,
        value,
        content_hash,
        dependency_identity=dependency_identity,
        evaluator_schema=_COMPILE_TIME_EVALUATOR_SCHEMA,
    )


def _callable_specialization_binding(
    name: str,
    binding: _StaticCallableBinding,
    dependency_identity: tuple[tuple[str, str], ...],
) -> ir_module.SpecializationBinding:
    content_hash = stable_digest({
        "schema": _COMPILE_TIME_EVALUATOR_SCHEMA,
        "kind": ir_module.SpecializationBindingKind.CALLABLE.value,
        "parameters": tuple(map(str, binding.parameter_types)),
        "return": str(binding.return_type),
        "callee_identity": binding.canonical_identity,
        "dependencies": dependency_identity,
    })
    return ir_module.SpecializationBinding(
        name,
        ir_module.SpecializationBindingKind.CALLABLE,
        None,
        None,
        content_hash,
        parameter_types=binding.parameter_types,
        return_type=binding.return_type,
        callee_identity=binding.canonical_identity,
        dependency_identity=dependency_identity,
        evaluator_schema=_COMPILE_TIME_EVALUATOR_SCHEMA,
    )


@dataclass(frozen=True)
class _FifoSymbol:
    name: str
    element_type: HardwareType
    depth: int

    @property
    def count_width(self) -> int:
        return max(1, self.depth.bit_length())


@dataclass(frozen=True)
class _MemorySymbol:
    name: str
    element_type: HardwareType
    depth: int

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())


@dataclass(frozen=True)
class _RomSymbol:
    name: str
    element_type: HardwareType
    depth: int

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())


@dataclass(frozen=True)
class _CompileTimeRealQuantization:
    raw: int
    precision: int
    logical_operations: int


@dataclass
class _ExpressionContext:
    functions: dict[str, _FunctionSignature]
    allow_delay: bool
    generic_functions: dict[str, ast.FunctionDecl] = field(default_factory=dict)
    function_catalog: _FunctionCatalog | None = None
    # Compile-time parameters are immutable elaboration inputs, not hardware
    # ports.  Constants retain their exact typed expression and callable
    # values retain only a statically resolved named-function identity.
    compile_time_constants: dict[str, ir_expr.Expression] = field(default_factory=dict)
    static_callables: dict[str, _StaticCallableBinding] = field(default_factory=dict)
    operator_declarations: tuple[ast.OperatorDecl, ...] = ()
    struct_declarations: tuple[ast.StructDecl, ...] = ()
    generic_specializations: list[ir_module.GenericSpecialization] = field(default_factory=list)
    function_definitions: dict[str, ir_module.Function] = field(default_factory=dict)
    # Exact generic/operator specializations are typed once and retained as
    # monomorphic callable definitions.  ``replace(context, ...)`` deliberately
    # shares these mutable registries across nested source/import contexts.
    callable_definitions: dict[str, ir_module.Function] = field(default_factory=dict)
    # Number of retained typed Call nodes for each monomorphic definition.
    # Functional-region compaction decrements calls that it replaces with a
    # typed table/template and may then discard an otherwise unreachable
    # compile-time-only definition without guessing source names.
    callable_use_counts: dict[str, int] = field(default_factory=dict)
    specializations_in_progress: set[str] = field(default_factory=set)
    # Logical elaboration cost of one specialization body. Cache hits replay
    # this cost so caching changes host work, never the language's bounded
    # compile-time generation semantics.
    specialization_budget_costs: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )
    # Host-work cache for one compilation.  Recursive child specialization
    # contexts share this mapping explicitly; no entry survives a top-level
    # ``analyze`` call. Cache hits replay ``logical_operations`` so bounded
    # elaboration semantics remain independent of host caching.
    compile_time_real_quantize_cache: dict[
        tuple[object, ...], _CompileTimeRealQuantization
    ] = field(default_factory=dict)
    resolution_stack: tuple[str, ...] = ()
    generic_dependency_identity: tuple[tuple[str, str], ...] = ()
    allow_fixed_target_coercion: bool = True
    structs: tuple[StructType, ...] = ()
    instance_outputs: dict[tuple[str, str], HardwareType] = field(default_factory=dict)
    # Keep protocol ownership beside the output type so runtime instance-array
    # projection can remain a wire-only value operation rather than silently
    # selecting a protocol endpoint.
    instance_output_protocols: dict[
        tuple[str, str], InterfaceProtocol
    ] = field(default_factory=dict)
    instance_arrays: dict[str, int] = field(default_factory=dict)
    # The bounded runtime instance-array spelling is only a read-only mux at a
    # module's public scalar-wire output boundary.  Explicit Generate plus
    # RuntimeIndex remains the general value-level representation.
    allow_runtime_instance_projection: bool = False
    allow_implementation_choice: bool = False
    # Formal-aware exploration is compiler configuration, but expression
    # checking may happen inside functions/operators as well as directly in a
    # module assignment.  Keep it on the expression context so nested callable
    # bodies cannot accidentally reach for ``analyze`` locals that are out of
    # scope.
    formal_config: object | None = None
    formal_verifier: object | None = None
    # Candidate generation is semantic work; formal execution is not.  Keep
    # every retained expression-local exploration in the compilation-owned
    # sink so the later selection phase can apply M39 without re-analysis.
    exploration_results: list[object] | None = None
    candidate_site_owner: str | None = None
    next_delay_instance: int = 0
    index_bindings: dict[str, int] = field(default_factory=dict)
    # Rule-local facts proven by the already typed guard.  They refine only
    # unsigned value ranges while typing that rule's atomic actions; facts do
    # not escape to another rule or to ordinary combinational assignments.
    range_refinements: dict[str, ir_expr.ValueRange] = field(default_factory=dict)
    index_types: dict[str, HardwareType] = field(default_factory=dict)
    parameters: dict[str, int] = field(default_factory=dict)
    unresolved_parameters: frozenset[str] = frozenset()
    aggregate_paths: dict[str, str] = field(default_factory=dict)
    union_binders: dict[str, ir_expr.Expression] = field(default_factory=dict)
    type_resolver: _TypeResolver | None = None
    compile_time_budget: "_CompileTimeBudget | None" = None
    source_unit: str | None = None
    source_digest: str | None = None
    source_digests: dict[str, str] = field(default_factory=dict)
    # Parser object identities are used only as an intra-compilation memo key.
    # The retained binder identity contains the deterministic semantic ordinal
    # and nesting path, never a source span, digest, or Python object identity.
    functional_binder_ordinals: dict[int, int] = field(default_factory=dict)
    next_functional_binder_ordinal: list[int] = field(
        default_factory=lambda: [0]
    )
    functional_binder_nesting: tuple[int, ...] = ()
    # Functional regions retained inside a callable use declaration-local
    # identities.  Module expressions deliberately leave this unset so their
    # established identity schema and source-order ordinals remain unchanged.
    functional_binder_callable_identity: str | None = None

    def allocate_delay(self) -> int:
        instance = self.next_delay_instance
        self.next_delay_instance += 1
        return instance


def _context_for_source_declaration(
    context: _ExpressionContext,
    source_unit: str | None,
) -> _ExpressionContext:
    """Select provenance for a declaration body without changing its semantics."""

    if source_unit is None or source_unit == context.source_unit:
        return context
    return replace(
        context,
        source_unit=source_unit,
        source_digest=context.source_digests.get(source_unit),
    )


def _context_for_callable_body(
    context: _ExpressionContext,
    source_unit: str | None,
    callable_identity: str,
) -> _ExpressionContext:
    """Give one callable a declaration-stable functional-region namespace.

    Ordinary function declarations are typed independently for every selected
    module in a hierarchy.  A module-global functional ordinal therefore made
    an otherwise identical function body depend on unrelated declarations and
    import order.  Callable bodies instead start their own ordinal/nesting
    sequence under the exact typed callable identity.  Diagnostic provenance
    remains selected by ``_context_for_source_declaration`` and does not enter
    the semantic binder identity.
    """

    selected = _context_for_source_declaration(context, source_unit)
    return replace(
        selected,
        functional_binder_ordinals={},
        next_functional_binder_ordinal=[0],
        functional_binder_nesting=(),
        functional_binder_callable_identity=callable_identity,
        # Candidate sites inside a retained callable body must be addressable
        # after typing.  The exact callee identity is the semantic boundary;
        # source names are not unique across generic specializations.
        candidate_site_owner=f"callable:{callable_identity}",
    )


def _lookup_function_signature(
    context: _ExpressionContext,
    name: str,
    *,
    call_origin: SourceOrigin | None = None,
) -> _FunctionSignature | None:
    """Resolve one ordinary function without using caller result context."""

    signature = context.functions.get(name)
    if signature is not None or context.function_catalog is None:
        return signature
    return context.function_catalog.resolve(name, call_origin=call_origin)


@dataclass
class _CompileTimeBudget:
    """Shared bounded budget for one selected top elaboration."""

    generated_elements: int = 0
    operations: int = 0
    call_depth: int = 0


def _budget_step(context: _ExpressionContext, operations: int = 1) -> None:
    budget = context.compile_time_budget
    if budget is None:
        return
    budget.operations += operations
    if budget.operations > _COMPILE_TIME_OPERATION_LIMIT:
        raise SemanticError(
            f"compile-time evaluator exceeded {_COMPILE_TIME_OPERATION_LIMIT} operations"
        )


def _replay_specialization_budget(
    context: _ExpressionContext,
    identity: str,
) -> None:
    """Charge the same logical cost for a cached specialization invocation."""

    budget = context.compile_time_budget
    if budget is None:
        return
    generated, operations = context.specialization_budget_costs.get(identity, (0, 0))
    budget.generated_elements += generated
    if budget.generated_elements > _TOTAL_GENERATED_LIMIT:
        raise SemanticError(
            f"compile-time generation exceeds {_TOTAL_GENERATED_LIMIT} elements"
        )
    budget.operations += operations
    if budget.operations > _COMPILE_TIME_OPERATION_LIMIT:
        raise SemanticError(
            f"compile-time evaluator exceeded {_COMPILE_TIME_OPERATION_LIMIT} operations"
        )


def _record_callable_use(context: _ExpressionContext, identity: str) -> None:
    context.callable_use_counts[identity] = (
        context.callable_use_counts.get(identity, 0) + 1
    )


def _release_callable_use(context: _ExpressionContext, identity: str) -> None:
    """Release one retained callable use after symbolic compaction.

    A compact functional table/template may replace a call to a compile-time-
    only, zero-argument specialization. Its definition is removable only after
    every retained use has disappeared. If that definition itself owns calls,
    release those references recursively as well; this keeps the specialization
    table proportional to the retained call graph without guessing source names.
    """

    count = context.callable_use_counts.get(identity, 0)
    if count <= 0:
        # Ordinary non-generic functions live in ``function_definitions`` and
        # are not reference-counted by the monomorphic specialization cache.
        # A region may still inline a pure zero-argument ordinary helper into
        # its template/table; keep that published function unchanged.
        if identity not in context.callable_definitions:
            return
        raise SemanticError(
            f"internal callable-use accounting underflow for '{identity}'"
        )
    if count > 1:
        context.callable_use_counts[identity] = count - 1
        return

    context.callable_use_counts.pop(identity, None)
    definition = context.callable_definitions.pop(identity, None)
    context.specialization_budget_costs.pop(identity, None)
    context.generic_specializations[:] = [
        record
        for record in context.generic_specializations
        if record.identity != identity
    ]
    if definition is None:
        return

    for nested_identity in _retained_callable_identities(definition.body):
        _release_callable_use(context, nested_identity)


def _retained_callable_identities(
    expression: ir_expr.Expression,
) -> tuple[str, ...]:
    """Return retained Call identities, preserving occurrence multiplicity."""

    result: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, ir_expr.Call):
            if value.callee_identity is not None:
                result.append(value.callee_identity)
            for argument in value.arguments:
                walk(argument)
            return
        if isinstance(value, ir_expr.FunctionalRegion):
            walk(value.template)
            for table in value.tables:
                walk(table.values)
            for _, captured in value.captures:
                walk(captured)
            return
        if isinstance(value, tuple):
            for item in value:
                walk(item)
            return
        if is_dataclass(value) and not isinstance(value, type):
            for item in fields(value):
                if item.name in {"origin", "source_origin", "type"}:
                    continue
                walk(getattr(value, item.name))

    walk(expression)
    return tuple(result)


def _compile_time_type_value(
    expression: ast.Expression,
    context: _ExpressionContext,
) -> HardwareType | None:
    if context.type_resolver is None:
        return None
    resolver = context.type_resolver
    if isinstance(expression, ast.TypeValueExpr):
        try:
            return resolver.resolve(expression.type_name)
        except SemanticError:
            return None
    if not isinstance(expression, ast.NameExpr):
        return None
    if expression.name in resolver._type_bindings:
        return resolver._type_bindings[expression.name]
    try:
        return resolver.resolve(ast.TypeName(expression.name))
    except SemanticError:
        return None


def _constant_ir_to_compile_time_real(
    value: ir_expr.Expression,
) -> ct_real.CompileTimeReal | None:
    if not isinstance(value, ir_expr.Constant):
        return None
    if isinstance(value.type, (BitType, UIntType, SIntType, BitsType)):
        return ct_real.CompileTimeReal.rational_value(Fraction(value.value))
    if isinstance(value.type, (FixedType, UFixedType)):
        return ct_real.CompileTimeReal.rational_value(
            Fraction(value.value, 1 << value.type.fraction)
        )
    return None


def _compile_time_real_value(
    expression: ast.Expression,
    inputs: dict[str, "_ValueSymbol"],
    context: _ExpressionContext,
) -> ct_real.CompileTimeReal:
    """Evaluate the frozen compiler-only real subset.

    The result is not hardware IR. Callers must either require an exact integer
    or quantize it immediately into an ordinary fixed-point constant.
    """

    try:
        _budget_step(context)
        if isinstance(expression, ast.CompileTimeIfExpr):
            selected = (
                expression.when_true
                if _compile_time_condition(expression.condition, inputs, context)
                else expression.when_false
            )
            if selected is None:
                raise SemanticError("compile-time if requires an else branch in an expression")
            return _compile_time_real_value(selected, inputs, context)
        if isinstance(expression, ast.NumberExpr):
            return ct_real.CompileTimeReal.rational_value(Fraction(expression.value))
        if isinstance(expression, ast.RationalExpr):
            return ct_real.CompileTimeReal.rational_value(
                Fraction(expression.numerator, expression.denominator)
            )
        if isinstance(expression, ast.NameExpr):
            if expression.name in context.index_bindings:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(context.index_bindings[expression.name])
                )
            if expression.name in context.parameters:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(context.parameters[expression.name])
                )
            symbol = inputs.get(expression.name)
            if isinstance(symbol, ir_module.LocalValue) and symbol.compile_time:
                value = _constant_ir_to_compile_time_real(symbol.expression)
                if value is not None:
                    return value
            raise SemanticError(
                f"compile-time real intrinsic references runtime value '{expression.name}'"
            )
        if isinstance(expression, ast.UnaryExpr):
            if expression.operator is ast.BinaryOperator.SUBTRACT:
                return ct_real.negate(
                    _compile_time_real_value(expression.expression, inputs, context)
                )
            raise SemanticError("unsupported compile-time real unary operator")
        if isinstance(expression, ast.AddExpr):
            return ct_real.add(
                _compile_time_real_value(expression.left, inputs, context),
                _compile_time_real_value(expression.right, inputs, context),
            )
        if isinstance(expression, ast.BinaryExpr):
            left = _compile_time_real_value(expression.left, inputs, context)
            right = _compile_time_real_value(expression.right, inputs, context)
            if expression.operator is ast.BinaryOperator.SUBTRACT:
                return ct_real.subtract(left, right)
            if expression.operator is ast.BinaryOperator.MULTIPLY:
                return ct_real.multiply(left, right)
            if expression.operator is ast.BinaryOperator.DIVIDE:
                return ct_real.divide(left, right)
            raise SemanticError(
                f"operator '{expression.operator.value}' is not allowed in a compile-time real expression"
            )
        if isinstance(expression, ast.CallExpr):
            if expression.function == "pi":
                if expression.arguments:
                    raise SemanticError("intrinsic 'pi' expects no arguments")
                return ct_real.CompileTimeReal.pi()
            if expression.function == "sin":
                if len(expression.arguments) != 1:
                    raise SemanticError("intrinsic 'sin' expects one argument")
                return ct_real.sin(
                    _compile_time_real_value(expression.arguments[0], inputs, context)
                )
            if expression.function == "cos":
                if len(expression.arguments) != 1:
                    raise SemanticError("intrinsic 'cos' expects one argument")
                return ct_real.cos(
                    _compile_time_real_value(expression.arguments[0], inputs, context)
                )
            if expression.function == "log2":
                if len(expression.arguments) != 1:
                    raise SemanticError("intrinsic 'log2' expects one argument")
                return ct_real.log2(
                    _compile_time_real_value(expression.arguments[0], inputs, context)
                )
            if expression.function == "log":
                if len(expression.arguments) != 2:
                    raise SemanticError("intrinsic 'log' expects two arguments")
                return ct_real.log(
                    _compile_time_real_value(expression.arguments[0], inputs, context),
                    _compile_time_real_value(expression.arguments[1], inputs, context),
                )
            if expression.function in {
                "length", "floor_log2", "ceil_log2", "index_width",
                "is_power_of_two",
            }:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(_compile_time_integer_value(expression, inputs, context))
                )
            raise SemanticError(
                f"function '{expression.function}' is not available to compile-time real evaluation"
            )
    except ct_real.CompileTimeRealError as error:
        raise SemanticError(str(error)) from error
    raise SemanticError(
        "compile-time real expression requires constants and compiler intrinsics"
    )


def _contains_compile_time_real_intrinsic(expression: ast.Expression) -> bool:
    if isinstance(expression, ast.CallExpr):
        return expression.function in _REAL_INTRINSICS or any(
            _contains_compile_time_real_intrinsic(argument)
            for argument in expression.arguments
        )
    if isinstance(expression, ast.UnaryExpr):
        return _contains_compile_time_real_intrinsic(expression.expression)
    if isinstance(expression, ast.AddExpr):
        return (
            _contains_compile_time_real_intrinsic(expression.left)
            or _contains_compile_time_real_intrinsic(expression.right)
        )
    if isinstance(expression, ast.BinaryExpr):
        return (
            _contains_compile_time_real_intrinsic(expression.left)
            or _contains_compile_time_real_intrinsic(expression.right)
        )
    if isinstance(expression, ast.CompileTimeIfExpr):
        return (
            _contains_compile_time_real_intrinsic(expression.condition)
            or _contains_compile_time_real_intrinsic(expression.when_true)
            or (
                expression.when_false is not None
                and _contains_compile_time_real_intrinsic(expression.when_false)
            )
        )
    if isinstance(expression, ast.MuxExpr):
        return (
            _contains_compile_time_real_intrinsic(expression.condition)
            or _contains_compile_time_real_intrinsic(expression.when_true)
            or _contains_compile_time_real_intrinsic(expression.when_false)
        )
    if isinstance(expression, ast.StructConstructExpr):
        return any(
            field.expression is not None
            and _contains_compile_time_real_intrinsic(field.expression)
            for field in expression.fields
        )
    if isinstance(expression, ast.FieldExpr):
        return _contains_compile_time_real_intrinsic(expression.expression)
    if isinstance(expression, ast.IndexExpr) and isinstance(expression.index, ast.Expression):
        return (
            _contains_compile_time_real_intrinsic(expression.expression)
            or _contains_compile_time_real_intrinsic(expression.index)
        )
    if isinstance(
        expression,
        (
            ast.SliceExpr,
            ast.VectorRangeExpr,
            ast.BitcastExpr,
            ast.ReshapeExpr,
            ast.PackExpr,
            ast.UnpackExpr,
        ),
    ):
        return _contains_compile_time_real_intrinsic(expression.expression)
    if isinstance(expression, ast.ConcatExpr):
        return any(
            _contains_compile_time_real_intrinsic(argument)
            for argument in expression.arguments
        )
    return False


def _compile_time_integer_value(
    expression: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    context: _ExpressionContext,
) -> int:
    """Evaluate the frozen exact integer/Boolean condition subset."""

    _budget_step(context)
    if isinstance(expression, ast.NumberExpr):
        return expression.value
    if isinstance(expression, ast.RationalExpr):
        value = Fraction(expression.numerator, expression.denominator)
        if value.denominator == 1:
            return value.numerator
        raise SemanticError("non-integral compile-time real value cannot be used as an integer")
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.index_bindings:
            return context.index_bindings[expression.name]
        if expression.name in context.parameters:
            return context.parameters[expression.name]
        raise SemanticError(
            f"compile-time condition references runtime value '{expression.name}'"
        )
    if isinstance(expression, ast.UnaryExpr):
        if expression.operator is ast.BinaryOperator.LOGIC_NOT:
            return int(not _compile_time_integer_value(expression.expression, inputs, context))
        if expression.operator is ast.BinaryOperator.SUBTRACT:
            return -_compile_time_integer_value(expression.expression, inputs, context)
        raise SemanticError("unsupported compile-time unary operator")
    if isinstance(expression, ast.AddExpr):
        return (
            _compile_time_integer_value(expression.left, inputs, context)
            + _compile_time_integer_value(expression.right, inputs, context)
        )
    if isinstance(expression, ast.BinaryExpr):
        operator = expression.operator
        if operator in {ast.BinaryOperator.EQUAL, ast.BinaryOperator.NOT_EQUAL}:
            left_type = _compile_time_type_value(expression.left, context)
            right_type = _compile_time_type_value(expression.right, context)
            if left_type is not None or right_type is not None:
                equal = left_type is not None and right_type is not None and left_type == right_type
                return int(equal if operator is ast.BinaryOperator.EQUAL else not equal)
        left = _compile_time_integer_value(expression.left, inputs, context)
        right = _compile_time_integer_value(expression.right, inputs, context)
        if operator is ast.BinaryOperator.LOGIC_AND:
            return int(bool(left) and bool(right))
        if operator is ast.BinaryOperator.LOGIC_OR:
            return int(bool(left) or bool(right))
        if operator is ast.BinaryOperator.EQUAL:
            return int(left == right)
        if operator is ast.BinaryOperator.NOT_EQUAL:
            return int(left != right)
        if operator is ast.BinaryOperator.LESS:
            return int(left < right)
        if operator is ast.BinaryOperator.LESS_EQUAL:
            return int(left <= right)
        if operator is ast.BinaryOperator.GREATER:
            return int(left > right)
        if operator is ast.BinaryOperator.GREATER_EQUAL:
            return int(left >= right)
        if operator is ast.BinaryOperator.SUBTRACT:
            return left - right
        if operator is ast.BinaryOperator.MULTIPLY:
            return left * right
        if operator is ast.BinaryOperator.DIVIDE:
            if right == 0 or left % right:
                raise SemanticError("compile-time division must be exact and non-zero")
            return left // right
        if operator is ast.BinaryOperator.SHIFT_LEFT:
            if right < 0:
                raise SemanticError("compile-time shift must be non-negative")
            return left << right
        if operator is ast.BinaryOperator.SHIFT_RIGHT:
            if right < 0:
                raise SemanticError("compile-time shift must be non-negative")
            return left >> right
        raise SemanticError(
            f"operator '{operator.value}' is not allowed in a compile-time condition"
        )
    if isinstance(expression, ast.CallExpr):
        if expression.function in _REAL_INTRINSICS:
            value = _compile_time_real_value(expression, inputs, context)
            exact = value.exact_integer()
            if exact is None:
                raise SemanticError(
                    f"intrinsic '{expression.function}' produced a non-integral compile-time real value; "
                    "use explicit quantize(...) for fixed-point hardware"
                )
            return exact
        if len(expression.arguments) != 1:
            raise SemanticError(f"compile-time intrinsic '{expression.function}' expects one argument")
        if expression.function == "length":
            value = _check_expression(expression.arguments[0], inputs, None, context)
            if not isinstance(value.type, VecType):
                raise SemanticError("length(...) requires a concrete vec<N,T>")
            return value.type.length
        value = _compile_time_integer_value(expression.arguments[0], inputs, context)
        if expression.function == "floor_log2":
            if value <= 0:
                raise SemanticError("floor_log2 requires a positive integer")
            return value.bit_length() - 1
        if expression.function == "ceil_log2":
            if value <= 0:
                raise SemanticError("ceil_log2 requires a positive integer")
            return max(0, (value - 1).bit_length())
        if expression.function == "index_width":
            if value <= 0:
                raise SemanticError("index_width requires a positive integer")
            return max(1, (value - 1).bit_length())
        if expression.function == "is_power_of_two":
            return int(value > 0 and (value & (value - 1)) == 0)
        raise SemanticError(
            f"compile-time condition function '{expression.function}' is not supported"
        )
    type_value = _compile_time_type_value(expression, context)
    if type_value is not None:
        raise SemanticError("type values may only be used with == or !=")
    raise SemanticError(
        "compile-time if condition must use compile-time values; use ?:, mux, "
        "switch, when, or priority { ... } for runtime actions"
    )


def _compile_time_condition(
    expression: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    context: _ExpressionContext,
) -> bool:
    # Type equality is handled before integer evaluation so an unresolved type
    # name cannot be mistaken for a runtime signal.
    if isinstance(expression, ast.BinaryExpr) and expression.operator in {
        ast.BinaryOperator.EQUAL,
        ast.BinaryOperator.NOT_EQUAL,
    }:
        left_type = _compile_time_type_value(expression.left, context)
        right_type = _compile_time_type_value(expression.right, context)
        if left_type is not None or right_type is not None:
            if left_type is None or right_type is None:
                result = expression.operator is ast.BinaryOperator.NOT_EQUAL
            else:
                result = left_type == right_type
            return result if expression.operator is ast.BinaryOperator.EQUAL else not result
    return bool(_compile_time_integer_value(expression, inputs, context))


def _resolve_range_bound(
    value: int | str,
    context: _ExpressionContext,
    label: str,
    inputs: dict[str, _ValueSymbol] | None = None,
) -> int:
    if isinstance(value, int):
        result = value
    elif (
        inputs is not None
        and context.type_resolver is not None
        and (match := re.fullmatch(r"length\(([A-Za-z_][A-Za-z0-9_]*)\)", value))
    ):
        symbol = inputs.get(match.group(1))
        if symbol is None:
            raise SemanticError(
                f"unresolved vector '{match.group(1)}' in {label} range bound"
            )
        type_ = getattr(symbol, "type", None)
        if not isinstance(type_, VecType):
            raise SemanticError(f"length(...) requires a concrete vec<N,T> in {label} range bound")
        result = type_.length
    elif context.type_resolver is not None:
        result = context.type_resolver._eval_constant_integer(
            value,
            description=f"{label} range bound",
            allow_zero=True,
            allow_negative=True,
        )
    else:
        raise SemanticError(f"{label} range bound requires compile-time evaluation")
    if result < 0:
        raise SemanticError(f"{label} range bound must be non-negative")
    return result


def _bind_generic_type(
    syntax: ast.TypeSyntax,
    actual: HardwareType,
    parameters: dict[str, ast.ModuleParameter],
    type_bindings: dict[str, HardwareType],
    value_bindings: dict[str, int],
    resolver: _TypeResolver,
) -> None:
    """Unify one declared type pattern with one exact concrete hardware type."""

    if isinstance(syntax, ast.VectorTypeName):
        if not isinstance(actual, VecType):
            raise SemanticError(f"expected {syntax}, got {actual}")
        if isinstance(syntax.length, str) and syntax.length in parameters:
            previous = value_bindings.get(syntax.length)
            if previous is not None and previous != actual.length:
                raise SemanticError(
                    f"conflicting inference for '{syntax.length}': {previous} and {actual.length}"
                )
            value_bindings[syntax.length] = actual.length
        elif int(syntax.length) != actual.length:
            raise SemanticError(f"vector length mismatch: expected {syntax.length}, got {actual.length}")
        _bind_generic_type(
            syntax.element_type, actual.element_type, parameters,
            type_bindings, value_bindings, resolver,
        )
        return

    if isinstance(syntax, ast.TupleTypeName):
        if not isinstance(actual, TupleType):
            raise SemanticError(f"expected {syntax}, got {actual}")
        if len(syntax.elements) != len(actual.elements):
            raise SemanticError(
                f"tuple arity mismatch: expected {len(syntax.elements)}, "
                f"got {len(actual.elements)}"
            )
        for pattern, concrete in zip(syntax.elements, actual.elements, strict=True):
            _bind_generic_type(
                pattern, concrete, parameters, type_bindings,
                value_bindings, resolver,
            )
        return

    assert isinstance(syntax, ast.TypeName)
    tuple_parts = _tuple_type_parts(syntax.text)
    if tuple_parts is not None:
        if not isinstance(actual, TupleType) or len(tuple_parts) != len(actual.elements):
            raise SemanticError(f"expected tuple type {syntax.text}, got {actual}")
        for pattern, concrete in zip(tuple_parts, actual.elements, strict=True):
            _bind_generic_type(
                ast.TypeName(pattern), concrete, parameters, type_bindings,
                value_bindings, resolver,
            )
        return
    parameter = parameters.get(syntax.text)
    if parameter is not None and parameter.kind == "type":
        previous = type_bindings.get(parameter.name)
        if previous is not None and previous != actual:
            raise SemanticError(
                f"conflicting inference for type '{parameter.name}': {previous} and {actual}"
            )
        type_bindings[parameter.name] = actual
        return

    pattern_generic = _TypeResolver._generic_parts(syntax.text)
    actual_generic = (
        _TypeResolver._generic_parts(actual.name)
        if isinstance(actual, StructType) else None
    )
    if pattern_generic is not None and actual_generic is not None:
        pattern_base, pattern_arguments = pattern_generic
        actual_base, actual_arguments = actual_generic
        if pattern_base == actual_base and len(pattern_arguments) == len(actual_arguments):
            for pattern_argument, actual_argument in zip(
                pattern_arguments, actual_arguments, strict=True
            ):
                _bind_generic_type(
                    ast.TypeName(pattern_argument),
                    resolver.resolve(ast.TypeName(actual_argument)),
                    parameters, type_bindings, value_bindings, resolver,
                )
            return

    specialized = _TypeResolver(
        tuple(ast.TypeAlias(name, target) for name, target in resolver._aliases.items()),
        tuple(resolver._structs.values()),
        tuple(resolver._enum_declarations.values()),
        tuple(parameters.values()),
        {**resolver._parameter_values, **value_bindings},
        {**resolver._type_bindings, **type_bindings},
        resolver._identity_namespace,
    ).resolve(syntax)
    if specialized != actual:
        raise SemanticError(f"exact type unification requires {specialized}, got {actual}")


def _bind_module_specialization_type(
    syntax: ast.TypeSyntax,
    actual: HardwareType,
    parameters: dict[str, ast.ModuleParameter],
    type_bindings: dict[str, HardwareType],
    value_bindings: dict[str, int],
    resolver: _TypeResolver,
) -> None:
    """Infer only direct, exact module-parameter occurrences from one type.

    Unlike generic callable inference this helper intentionally does not solve
    arithmetic equations in widths.  ``vec<N,T>`` and ``uint<N>`` are direct
    structural patterns; ``vec<2*N,T>`` can only be checked after ``N`` was
    supplied explicitly or by a default/another direct occurrence.
    """

    def bind_value(name: str, value: int) -> bool:
        parameter = parameters.get(name)
        if parameter is None or parameter.kind != "value":
            return False
        previous = value_bindings.get(name)
        if previous is not None and previous != value:
            raise SemanticError(
                f"conflicting exact inference for value parameter '{name}': "
                f"{previous} and {value}"
            )
        value_bindings[name] = value
        return True

    def bind_type(name: str, value: HardwareType) -> bool:
        parameter = parameters.get(name)
        if parameter is None or parameter.kind != "type":
            return False
        previous = type_bindings.get(name)
        if previous is not None and previous != value:
            raise SemanticError(
                f"conflicting exact inference for type parameter '{name}': "
                f"{previous} and {value}"
            )
        type_bindings[name] = value
        return True

    if isinstance(syntax, ast.VectorTypeName):
        if not isinstance(actual, VecType):
            raise SemanticError(
                f"exact specialization inference expected a vector, got {actual}"
            )
        length = syntax.length
        if isinstance(length, str):
            if not bind_value(length, actual.length):
                unresolved = {
                    name
                    for name, parameter in parameters.items()
                    if parameter.kind == "value"
                    and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                        length,
                    )
                    and name not in value_bindings
                }
                if not unresolved:
                    specialized = _TypeResolver(
                        tuple(
                            ast.TypeAlias(name, target)
                            for name, target in resolver._aliases.items()
                        ),
                        tuple(resolver._structs.values()),
                        tuple(resolver._enum_declarations.values()),
                        tuple(parameters.values()),
                        {**resolver._parameter_values, **value_bindings},
                        {**resolver._type_bindings, **type_bindings},
                        resolver._identity_namespace,
                    )._eval_width(length)
                    if specialized != actual.length:
                        raise SemanticError(
                            "exact specialization inference requires vector "
                            f"length {specialized}, got {actual.length}"
                        )
        elif length != actual.length:
            raise SemanticError(
                f"exact specialization inference requires vector length "
                f"{length}, got {actual.length}"
            )
        _bind_module_specialization_type(
            syntax.element_type,
            actual.element_type,
            parameters,
            type_bindings,
            value_bindings,
            resolver,
        )
        return

    if isinstance(syntax, ast.TupleTypeName):
        if not isinstance(actual, TupleType):
            raise SemanticError(
                f"exact specialization inference expected a tuple, got {actual}"
            )
        if len(syntax.elements) != len(actual.elements):
            raise SemanticError(
                f"exact specialization inference requires tuple arity "
                f"{len(syntax.elements)}, got {len(actual.elements)}"
            )
        for pattern, concrete in zip(syntax.elements, actual.elements, strict=True):
            _bind_module_specialization_type(
                pattern, concrete, parameters, type_bindings,
                value_bindings, resolver,
            )
        return

    assert isinstance(syntax, ast.TypeName)
    tuple_parts = _tuple_type_parts(syntax.text)
    if tuple_parts is not None:
        if not isinstance(actual, TupleType) or len(tuple_parts) != len(actual.elements):
            raise SemanticError(
                f"exact specialization inference expected {syntax.text}, got {actual}"
            )
        for pattern, concrete in zip(tuple_parts, actual.elements, strict=True):
            _bind_module_specialization_type(
                ast.TypeName(pattern), concrete, parameters, type_bindings,
                value_bindings, resolver,
            )
        return
    if bind_type(syntax.text, actual):
        return

    scalar = re.fullmatch(
        r"(uint|sint|bits)<([A-Za-z_][A-Za-z0-9_]*)>", syntax.text
    )
    if scalar is not None:
        family, width_name = scalar.groups()
        expected_class = {
            "uint": UIntType,
            "sint": SIntType,
            "bits": BitsType,
        }[family]
        if not isinstance(actual, expected_class):
            raise SemanticError(
                f"exact specialization inference expected {family}, got {actual}"
            )
        if bind_value(width_name, actual.width):
            return

    fixed = re.fullmatch(
        r"(fixed|ufixed|fixed_sat|ufixed_sat)"
        r"<([A-Za-z_][A-Za-z0-9_]*|[0-9]+),"
        r"([A-Za-z_][A-Za-z0-9_]*|[0-9]+)>",
        syntax.text,
    )
    if fixed is not None:
        family, width_name, fraction_name = fixed.groups()
        expected_class = (
            FixedType if family in {"fixed", "fixed_sat"} else UFixedType
        )
        expected_overflow = (
            FixedOverflowPolicy.SATURATE
            if family.endswith("_sat")
            else FixedOverflowPolicy.WRAP
        )
        if (
            not isinstance(actual, expected_class)
            or actual.overflow is not expected_overflow
        ):
            raise SemanticError(
                f"exact specialization inference expected {family}, got {actual}"
            )
        width_ok = bind_value(width_name, actual.width)
        fraction_ok = bind_value(fraction_name, actual.fraction)
        if width_ok or fraction_ok:
            if width_name.isdigit() and int(width_name) != actual.width:
                raise SemanticError(
                    f"exact specialization inference requires width {width_name}, "
                    f"got {actual.width}"
                )
            if fraction_name.isdigit() and int(fraction_name) != actual.fraction:
                raise SemanticError(
                    "exact specialization inference requires fractional width "
                    f"{fraction_name}, got {actual.fraction}"
                )
            return

    pattern_generic = _TypeResolver._generic_parts(syntax.text)
    actual_generic = (
        _TypeResolver._generic_parts(actual.name)
        if isinstance(actual, StructType)
        else None
    )
    if pattern_generic is not None and actual_generic is not None:
        pattern_base, pattern_arguments = pattern_generic
        actual_base, actual_arguments = actual_generic
        declaration = resolver._structs.get(pattern_base)
        if (
            declaration is not None
            and pattern_base == actual_base
            and len(pattern_arguments) == len(actual_arguments)
            and len(declaration.parameters) == len(pattern_arguments)
        ):
            for struct_parameter, pattern_argument, actual_argument in zip(
                declaration.parameters,
                pattern_arguments,
                actual_arguments,
                strict=True,
            ):
                if struct_parameter.kind == "type":
                    _bind_module_specialization_type(
                        ast.TypeName(pattern_argument),
                        resolver.resolve(ast.TypeName(actual_argument)),
                        parameters,
                        type_bindings,
                        value_bindings,
                        resolver,
                    )
                    continue
                try:
                    actual_value = int(actual_argument)
                except ValueError as error:
                    raise SemanticError(
                        "concrete generic struct value argument is not an integer"
                    ) from error
                if bind_value(pattern_argument, actual_value):
                    continue
                if pattern_argument.isdigit():
                    if int(pattern_argument) != actual_value:
                        raise SemanticError(
                            "exact specialization inference requires generic "
                            f"value {pattern_argument}, got {actual_value}"
                        )
                    continue
                # A compound value expression is intentionally not inverted.
                # If its dependencies were supplied elsewhere, the exact
                # comparison below validates it.
            unresolved_after_struct = {
                name
                for name, parameter in parameters.items()
                if (
                    parameter.kind == "type" and name not in type_bindings
                    or parameter.kind == "value" and name not in value_bindings
                )
                and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                    syntax.text,
                )
            }
            if not unresolved_after_struct:
                return

    # Resolve and compare any pattern which is already fully determined.  An
    # unresolved parameter is left for the caller's explicit ambiguity error;
    # it is never guessed by equation solving or conversion.
    unresolved_names = {
        name
        for name, parameter in parameters.items()
        if (
            parameter.kind == "type" and name not in type_bindings
            or parameter.kind == "value" and name not in value_bindings
        )
        and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            syntax.text,
        )
    }
    if unresolved_names:
        return
    specialized_resolver = _TypeResolver(
        tuple(
            ast.TypeAlias(name, target)
            for name, target in resolver._aliases.items()
        ),
        tuple(resolver._structs.values()),
        tuple(resolver._enum_declarations.values()),
        tuple(parameters.values()),
        {**resolver._parameter_values, **value_bindings},
        {**resolver._type_bindings, **type_bindings},
        resolver._identity_namespace,
    )
    specialized = specialized_resolver.resolve(syntax)
    if specialized != actual:
        raise SemanticError(
            f"exact specialization inference requires {specialized}, got {actual}"
        )


def _specialization_bindings(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    explicit: tuple[ast.SpecializationArgument, ...],
    arguments: tuple[ir_expr.Expression, ...],
    context: _ExpressionContext,
    specialization_symbols: dict[str, object] | None = None,
) -> tuple[
    dict[str, HardwareType],
    dict[str, int],
    dict[str, ir_expr.Expression],
    dict[str, _StaticCallableBinding],
]:
    if context.type_resolver is None:
        raise SemanticError("generic specialization requires a type resolver")
    parameters = {item.name: item for item in declaration.generic_parameters}
    type_bindings: dict[str, HardwareType] = {}
    value_bindings: dict[str, int] = {}
    constant_bindings: dict[str, ir_expr.Expression] = {}
    callable_bindings: dict[str, _StaticCallableBinding] = {}
    positional = [item for item in explicit if item.name is None]
    named = [item for item in explicit if item.name is not None]
    if positional and named:
        raise SemanticError("generic specialization cannot mix positional and named arguments")
    explicit_names = tuple(item.name for item in named)
    if len(explicit_names) != len(set(explicit_names)):
        duplicate = next(
            name for name in explicit_names if explicit_names.count(name) > 1
        )
        raise SemanticError(
            f"generic specialization parameter '{duplicate}' is assigned more than once"
        )
    deferred: list[tuple[ast.ModuleParameter, ast.SpecializationArgument]] = []
    for index, item in enumerate(explicit):
        name = item.name or (
            declaration.generic_parameters[index].name
            if index < len(declaration.generic_parameters) else ""
        )
        parameter = parameters.get(name)
        if parameter is None:
            raise SemanticError(f"unknown or excess generic argument '{name}'")
        if parameter.kind in {"constant", "callable"}:
            if item.name is None:
                raise SemanticError(
                    f"compile-time {parameter.kind} parameter '{name}' requires "
                    "a named specialization argument"
                )
            deferred.append((parameter, item))
            continue
        if parameter.kind == "type":
            syntax = item.value if isinstance(item.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)) else ast.TypeName(str(item.value))
            type_bindings[name] = context.type_resolver.resolve(syntax)
        else:
            try:
                if isinstance(item.value, int):
                    value_bindings[name] = item.value
                else:
                    text = item.value.text if isinstance(item.value, ast.TypeName) else str(item.value)
                    value_bindings[name] = context.type_resolver._eval_constant_integer(
                        text,
                        description=f"value parameter '{name}'",
                        allow_zero=True,
                        allow_negative=True,
                        local_values={
                            **context.parameters,
                            **context.index_bindings,
                        },
                    )
            except (TypeError, ValueError, SemanticError) as error:
                raise SemanticError(f"value parameter '{name}' requires an integer") from error
    if len(arguments) != len(declaration.parameters):
        callable_name = (
            declaration.name if isinstance(declaration, ast.FunctionDecl)
            else f"operator {declaration.operator}"
        )
        raise SemanticError(
            f"'{callable_name}' expects "
            f"{len(declaration.parameters)} arguments, got {len(arguments)}"
        )
    for formal, actual in zip(declaration.parameters, arguments, strict=True):
        _bind_generic_type(
            formal.type_name, actual.type, parameters, type_bindings,
            value_bindings, context.type_resolver,
        )
    for parameter in declaration.generic_parameters:
        if parameter.kind == "type" and parameter.name not in type_bindings:
            raise SemanticError(f"cannot infer type parameter '{parameter.name}'")
        if parameter.kind == "value" and parameter.name not in value_bindings:
            if isinstance(parameter.default, int):
                value_bindings[parameter.name] = parameter.default
            else:
                raise SemanticError(f"cannot infer value parameter '{parameter.name}'")

    assert context.type_resolver is not None
    binding_resolver = _TypeResolver(
        tuple(
            ast.TypeAlias(name, target)
            for name, target in context.type_resolver._aliases.items()
        ),
        tuple(context.type_resolver._structs.values()),
        tuple(context.type_resolver._enum_declarations.values()),
        declaration.generic_parameters,
        {**context.parameters, **value_bindings},
        {
            **type_bindings,
            **{str(type_): type_ for type_ in type_bindings.values()},
        },
        context.type_resolver._identity_namespace,
        tagged_unions=tuple(
            context.type_resolver._tagged_union_declarations.values()
        ),
    )

    symbols = specialization_symbols or {}
    for parameter, item in deferred:
        if parameter.kind == "constant":
            assert parameter.type_name is not None
            expected_type = binding_resolver.resolve(parameter.type_name)
            if isinstance(expected_type, EnumType) or _contains_enum_type(expected_type):
                raise SemanticError(
                    f"compile-time constant parameter '{parameter.name}' cannot contain an enum"
                )
            try:
                ir_packing.packed_width(expected_type)
            except ir_packing.PackingError as error:
                raise SemanticError(
                    f"compile-time constant parameter '{parameter.name}' type "
                    f"{expected_type} is not recursively bit-packable: {error}"
                ) from error
            constant_name = (
                item.value.text
                if isinstance(item.value, ast.TypeName)
                else item.value
            )
            if not isinstance(constant_name, str):
                raise SemanticError(
                    f"compile-time constant parameter '{parameter.name}' requires "
                    "an immutable named value"
                )
            candidate = context.compile_time_constants.get(constant_name)
            if candidate is None:
                symbol = symbols.get(constant_name)
                if isinstance(symbol, ir_module.LocalValue) and symbol.compile_time:
                    candidate = symbol.expression
                elif isinstance(symbol, ir_expr.Expression):
                    candidate = symbol
            if candidate is None:
                raise SemanticError(
                    f"compile-time constant argument '{constant_name}' for "
                    f"'{parameter.name}' is not an immutable compile-time value"
                )
            candidate = _expand_analysis_calls(
                candidate,
                context,
                purpose=f"compile-time constant parameter '{parameter.name}'",
            )
            if candidate.type != expected_type:
                raise SemanticError(
                    f"compile-time constant parameter '{parameter.name}' has type "
                    f"{candidate.type}, expected exact {expected_type}"
                )
            try:
                runtime_value = constant_runtime_value(candidate)
            except ConstantExpressionError as error:
                raise SemanticError(
                    f"compile-time constant parameter '{parameter.name}' is not "
                    f"fully constant: {error}"
                ) from error
            # Re-materialize through the already typed expression; the digest
            # enters specialization identity while source spelling does not.
            constant_bindings[parameter.name] = candidate
            continue

        assert parameter.kind == "callable"
        if not isinstance(item.value, ast.CallableRef):
            raise SemanticError(
                f"compile-time callable parameter '{parameter.name}' requires "
                "an explicit 'fn name' reference"
            )
        expected_parameters = tuple(
            binding_resolver.resolve(type_name)
            for type_name in parameter.callable_parameters
        )
        assert parameter.callable_return_type is not None
        expected_return = binding_resolver.resolve(parameter.callable_return_type)
        forwarded = context.static_callables.get(item.value.name)
        if forwarded is not None:
            if item.value.specializations:
                raise SemanticError(
                    f"forwarded callable '{item.value.name}' cannot be re-specialized"
                )
            if (
                forwarded.parameter_types != expected_parameters
                or forwarded.return_type != expected_return
            ):
                raise SemanticError(
                    f"callable parameter '{parameter.name}' expects "
                    f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                    f"got fn({', '.join(map(str, forwarded.parameter_types))})->"
                    f"{forwarded.return_type}"
                )
            callable_bindings[parameter.name] = forwarded
            continue
        signature = _lookup_function_signature(
            context,
            item.value.name,
        )
        generic = context.generic_functions.get(item.value.name)
        if signature is None and generic is None:
            raise SemanticError(
                f"unknown pure function '{item.value.name}' for callable "
                f"parameter '{parameter.name}'"
            )
        if signature is not None:
            if item.value.specializations:
                raise SemanticError(
                    f"non-generic function '{item.value.name}' does not accept "
                    "specialization arguments"
                )
            actual_parameters = tuple(item.type for item in signature.parameters)
            actual_return = signature.return_type
            if actual_parameters != expected_parameters or actual_return != expected_return:
                raise SemanticError(
                    f"callable parameter '{parameter.name}' expects "
                    f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                    f"got fn({', '.join(map(str, actual_parameters))})->{actual_return}"
                )
            concrete_identity = stable_callee_identity(
                item.value.name,
                signature.parameters,
                signature.return_type,
            )
            concrete_definitions: tuple[ir_module.Function, ...] = ()
        else:
            assert generic is not None
            placeholders = tuple(
                ir_expr.ParameterRef(
                    f"__zlang_callable_argument_{index}",
                    type_,
                )
                for index, type_ in enumerate(expected_parameters)
            )
            concrete_call = _specialize_callable(
                generic,
                placeholders,
                item.value.specializations,
                context,
                specialization_symbols=symbols,
            )
            if concrete_call.type != expected_return:
                raise SemanticError(
                    f"callable parameter '{parameter.name}' expects "
                    f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                    f"got fn({', '.join(map(str, expected_parameters))})->"
                    f"{concrete_call.type}"
                )
            if concrete_call.callee_identity is None:
                raise SemanticError(
                    f"generic callable argument '{item.value.name}' did not "
                    "resolve to a concrete callee identity"
                )
            concrete_definition = context.callable_definitions.get(
                concrete_call.callee_identity
            )
            if concrete_definition is None:
                raise SemanticError(
                    f"generic callable argument '{item.value.name}' did not "
                    "publish its concrete definition"
                )
            actual_parameters = tuple(
                item.type for item in concrete_definition.parameters
            )
            if (
                actual_parameters != expected_parameters
                or concrete_definition.return_type != expected_return
            ):
                raise SemanticError(
                    f"callable parameter '{parameter.name}' expects "
                    f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                    f"got fn({', '.join(map(str, actual_parameters))})->"
                    f"{concrete_definition.return_type}"
                )
            concrete_identity = concrete_definition.callee_identity
            concrete_definitions = _static_callable_definition_closure(
                concrete_call, context
            )
        callable_bindings[parameter.name] = _StaticCallableBinding(
            item.value,
            expected_parameters,
            expected_return,
            concrete_identity,
            concrete_definitions,
        )

    missing_constant = next(
        (
            parameter.name
            for parameter in declaration.generic_parameters
            if parameter.kind == "constant"
            and parameter.name not in constant_bindings
        ),
        None,
    )
    if missing_constant is not None:
        raise SemanticError(
            f"missing required compile-time constant argument '{missing_constant}'"
        )
    missing_callable = next(
        (
            parameter.name
            for parameter in declaration.generic_parameters
            if parameter.kind == "callable"
            and parameter.name not in callable_bindings
        ),
        None,
    )
    if missing_callable is not None:
        raise SemanticError(
            f"missing required compile-time callable argument '{missing_callable}'"
        )
    return type_bindings, value_bindings, constant_bindings, callable_bindings


def _is_direct_integer_literal_syntax(expression: ast.Expression) -> bool:
    """Return whether syntax is one direct integer literal, including ``-N``."""

    return isinstance(expression, ast.NumberExpr) or (
        isinstance(expression, ast.UnaryExpr)
        and expression.operator is ast.BinaryOperator.SUBTRACT
        and isinstance(expression.expression, ast.NumberExpr)
    )


def _contextualize_generic_integer_arguments(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    syntax_arguments: tuple[ast.Expression, ...],
    typed_arguments: tuple[ir_expr.Expression, ...],
    explicit: tuple[ast.SpecializationArgument, ...],
    context: _ExpressionContext,
) -> tuple[ir_expr.Expression, ...]:
    """Retype direct integer arguments after exact generic shape inference.

    A literal must not choose a generic type before a non-literal argument (or
    an explicit specialization) has fixed that type.  Conversely, the call's
    expected result is deliberately absent here: ``id(0)`` still specializes
    to the minimum literal type unless ``T`` is fixed at the call itself.
    """

    literal_positions = tuple(
        index
        for index, value in enumerate(syntax_arguments)
        if _is_direct_integer_literal_syntax(value)
    )
    if (
        not literal_positions
        or context.type_resolver is None
        or len(syntax_arguments) != len(declaration.parameters)
    ):
        return typed_arguments

    parameters = {item.name: item for item in declaration.generic_parameters}
    type_bindings: dict[str, HardwareType] = {}
    value_bindings: dict[str, int] = {}
    positional = [item for item in explicit if item.name is None]
    named = [item for item in explicit if item.name is not None]
    if positional and named:
        return typed_arguments
    for index, item in enumerate(explicit):
        name = item.name or (
            declaration.generic_parameters[index].name
            if index < len(declaration.generic_parameters)
            else ""
        )
        parameter = parameters.get(name)
        if parameter is None:
            continue
        if parameter.kind == "type" and isinstance(
            item.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
        ):
            type_bindings[name] = context.type_resolver.resolve(item.value)
        elif parameter.kind == "value":
            try:
                if isinstance(item.value, int):
                    value_bindings[name] = item.value
                else:
                    text = (
                        item.value.text
                        if isinstance(item.value, ast.TypeName)
                        else str(item.value)
                    )
                    value_bindings[name] = context.type_resolver._eval_constant_integer(
                        text,
                        description=f"value parameter '{name}'",
                        allow_zero=True,
                        allow_negative=True,
                        local_values={
                            **context.parameters,
                            **context.index_bindings,
                        },
                    )
            except (TypeError, ValueError, SemanticError):
                # The authoritative specialization pass below owns the public
                # diagnostic for an invalid explicit argument.
                return typed_arguments

    for index, (formal, actual) in enumerate(
        zip(declaration.parameters, typed_arguments, strict=True)
    ):
        if index in literal_positions:
            continue
        try:
            _bind_generic_type(
                formal.type_name,
                actual.type,
                parameters,
                type_bindings,
                value_bindings,
                context.type_resolver,
            )
        except SemanticError:
            # This is a provisional pass used only to discover a literal's
            # contextual formal type. The authoritative specialization pass
            # below owns conflict wording and call/declaration attribution.
            return typed_arguments

    binding_resolver = _TypeResolver(
        tuple(
            ast.TypeAlias(name, target)
            for name, target in context.type_resolver._aliases.items()
        ),
        tuple(context.type_resolver._structs.values()),
        tuple(context.type_resolver._enum_declarations.values()),
        declaration.generic_parameters,
        {**context.parameters, **value_bindings},
        {
            **type_bindings,
            **{str(type_): type_ for type_ in type_bindings.values()},
        },
        context.type_resolver._identity_namespace,
        tagged_unions=tuple(
            context.type_resolver._tagged_union_declarations.values()
        ),
    )
    result = list(typed_arguments)
    contextual_types = (
        BitType,
        UIntType,
        SIntType,
        BitsType,
        FixedType,
        UFixedType,
    )
    for index in literal_positions:
        try:
            target = binding_resolver.resolve(
                declaration.parameters[index].type_name
            )
        except SemanticError:
            continue
        if isinstance(target, contextual_types):
            syntax = syntax_arguments[index]
            assert _is_direct_integer_literal_syntax(syntax)
            if isinstance(syntax, ast.NumberExpr):
                value = syntax.value
                signed_syntax = False
            else:
                assert isinstance(syntax, ast.UnaryExpr)
                assert isinstance(syntax.expression, ast.NumberExpr)
                value = -syntax.expression.value
                signed_syntax = True
            literal = _check_integer_literal(
                value,
                target,
                signed_syntax=signed_syntax,
            )
            origin = _semantic_origin(syntax, context)
            result[index] = (
                replace(literal, origin=origin) if origin is not None else literal
            )
    return tuple(result)


def _callable_declaration_origin(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    context: _ExpressionContext,
) -> SourceOrigin | None:
    """Return diagnostic-only provenance for one source callable declaration."""

    if declaration.origin is None:
        return None
    owner = (
        f"function {declaration.name}"
        if isinstance(declaration, ast.FunctionDecl)
        else f"operator {declaration.operator}"
    )
    source_unit = declaration.source_identity or context.source_unit
    digest = (
        context.source_digests.get(source_unit)
        if source_unit is not None
        else None
    )
    if digest is None and source_unit == context.source_unit:
        digest = context.source_digest
    return SourceOrigin(
        declaration.origin,
        f"{owner} declaration",
        source_unit,
        digest,
    )


def _annotate_callable_error(
    error: SemanticError,
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    context: _ExpressionContext,
    call_origin: SourceOrigin | None,
) -> SemanticError:
    """Attach call/declaration provenance without changing the legacy message."""

    declaration_origin = _callable_declaration_origin(declaration, context)
    notes = list(error.notes)
    if declaration_origin is not None:
        location = declaration_origin.render()
        if declaration_origin.source_unit is not None:
            location = f"{declaration_origin.source_unit}:{location}"
        note = f"callable declared at {location}"
        if note not in notes:
            notes.append(note)
    return SemanticError(
        str(error),
        code=error.code,
        primary=call_origin or error.primary,
        notes=notes,
        fixes=error.fixes,
    )


def _specialize_callable(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    arguments: tuple[ir_expr.Expression, ...],
    explicit: tuple[ast.SpecializationArgument, ...],
    context: _ExpressionContext,
    *,
    call_origin: SourceOrigin | None = None,
    specialization_symbols: dict[str, object] | None = None,
) -> ir_expr.Expression:
    try:
        return _specialize_callable_unannotated(
            declaration,
            arguments,
            explicit,
            context,
            specialization_symbols=specialization_symbols,
        )
    except SemanticError as error:
        raise _annotate_callable_error(
            error,
            declaration,
            context,
            call_origin,
        ) from error


def _specialize_callable_unannotated(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    arguments: tuple[ir_expr.Expression, ...],
    explicit: tuple[ast.SpecializationArgument, ...],
    context: _ExpressionContext,
    *,
    specialization_symbols: dict[str, object] | None = None,
) -> ir_expr.Expression:
    (
        type_bindings,
        value_bindings,
        constant_bindings,
        callable_bindings,
    ) = _specialization_bindings(
        declaration,
        explicit,
        arguments,
        context,
        specialization_symbols,
    )
    assert context.type_resolver is not None
    # Generic source types are still represented as ``TypeSyntax`` while the
    # inferred arguments are already canonical hardware types.  Register the
    # canonical spellings as resolver aliases as well as the source parameter
    # names so nominal types (notably enums) survive substitution without being
    # mistaken for a user-written type name.
    callable_type_bindings = {
        **type_bindings,
        **{str(type_): type_ for type_ in type_bindings.values()},
    }
    resolver = _TypeResolver(
        tuple(ast.TypeAlias(name, target) for name, target in context.type_resolver._aliases.items()),
        tuple(context.type_resolver._structs.values()),
        tuple(context.type_resolver._enum_declarations.values()),
        declaration.generic_parameters,
        {**context.parameters, **value_bindings},
        callable_type_bindings,
        context.type_resolver._identity_namespace,
        tagged_unions=tuple(
            context.type_resolver._tagged_union_declarations.values()
        ),
    )
    def concrete_syntax(syntax: ast.TypeSyntax) -> ast.TypeSyntax:
        if isinstance(syntax, ast.VectorTypeName):
            length = value_bindings.get(syntax.length, syntax.length) if isinstance(syntax.length, str) else syntax.length
            return ast.VectorTypeName(length, concrete_syntax(syntax.element_type))
        if isinstance(syntax, ast.TupleTypeName):
            return ast.TupleTypeName(
                tuple(concrete_syntax(item) for item in syntax.elements)
            )
        text = syntax.text
        for name, type_ in sorted(type_bindings.items(), key=lambda item: -len(item[0])):
            text = re.sub(rf"\b{re.escape(name)}\b", str(type_), text)
        for name, value in sorted(value_bindings.items(), key=lambda item: -len(item[0])):
            text = re.sub(rf"\b{re.escape(name)}\b", str(value), text)
        return ast.TypeName(text)
    concrete_parameters = tuple(
        ir_module.FunctionParameter(parameter.name, resolver.resolve(concrete_syntax(parameter.type_name)))
        for parameter in declaration.parameters
    )
    for formal, actual in zip(concrete_parameters, arguments, strict=True):
        if formal.type != actual.type:
            raise SemanticError(
                f"argument '{formal.name}' has type {actual.type}, expected exact {formal.type}"
            )
    specialization_bindings = tuple(
        _constant_specialization_binding(
            parameter.name,
            constant_bindings[parameter.name],
            context.generic_dependency_identity,
        )
        if parameter.kind == "constant"
        else _callable_specialization_binding(
            parameter.name,
            callable_bindings[parameter.name],
            context.generic_dependency_identity,
        )
        for parameter in declaration.generic_parameters
        if parameter.kind in {"constant", "callable"}
    )
    specialization_binding_by_name = {
        binding.name: binding for binding in specialization_bindings
    }
    constant_identities = {
        name: specialization_binding_by_name[name].content_hash
        for name in constant_bindings
    }
    rendered = tuple(
        (
            parameter.name,
            str(type_bindings[parameter.name])
            if parameter.kind == "type"
            else str(value_bindings[parameter.name])
            if parameter.kind == "value"
            else f"constant:{constant_identities[parameter.name]}"
            if parameter.kind == "constant"
            else f"callable:{callable_bindings[parameter.name].canonical_identity}"
        )
        for parameter in declaration.generic_parameters
    )
    owner = declaration.name if isinstance(declaration, ast.FunctionDecl) else f"operator{declaration.operator}"
    generic_arguments = tuple(
        GenericArgument(
            parameter.name,
            parameter.kind,
            type_bindings[parameter.name]
            if parameter.kind == "type"
            else value_bindings[parameter.name]
            if parameter.kind == "value"
            else f"{constant_bindings[parameter.name].type}:{constant_identities[parameter.name]}"
            if parameter.kind == "constant"
            else callable_bindings[parameter.name].canonical_identity,
        )
        for parameter in declaration.generic_parameters
    )
    identity = SpecializationIdentity.create(
        declaration=declaration,
        owner=owner,
        arguments=generic_arguments,
        source_identity=declaration.source_identity,
        dependency_identity=context.generic_dependency_identity,
    ).digest
    definition_name = f"zlang_spec_{identity}"
    stack_key = f"{owner}:{identity}"
    if (
        stack_key in context.resolution_stack
        or identity in context.specializations_in_progress
    ):
        raise SemanticError(f"recursive generic specialization cycle for '{owner}'")

    cached = context.callable_definitions.get(identity)
    if cached is not None:
        _replay_specialization_budget(context, identity)
        _record_callable_use(context, identity)
        return ir_expr.Call(
            cached.name,
            arguments,
            cached.return_type,
            cached.callee_identity,
            origin=cached.body.origin,
        )

    budget = context.compile_time_budget
    budget_before = (
        (budget.generated_elements, budget.operations)
        if budget is not None
        else None
    )
    if budget is not None:
        budget.call_depth += 1
        if budget.call_depth > _COMPILE_TIME_CALL_LIMIT:
            budget.call_depth -= 1
            raise SemanticError(
                f"compile-time function nesting exceeds {_COMPILE_TIME_CALL_LIMIT} calls"
            )
    body_context = _context_for_callable_body(
        context,
        declaration.source_identity,
        identity,
    )
    body_context = replace(
        body_context,
        type_resolver=resolver,
        parameters={**context.parameters, **value_bindings},
        compile_time_constants={
            **context.compile_time_constants,
            **constant_bindings,
        },
        static_callables={**context.static_callables, **callable_bindings},
        resolution_stack=(*context.resolution_stack, stack_key),
        allow_fixed_target_coercion=declaration.return_type is None,
    )
    symbols: dict[str, object] = {
        parameter.name: parameter for parameter in concrete_parameters
    }
    symbols.update(constant_bindings)
    expected_return = resolver.resolve(concrete_syntax(declaration.return_type)) if declaration.return_type is not None else None
    context.specializations_in_progress.add(identity)
    try:
        try:
            body = _check_callable_body(
                declaration,
                symbols,
                expected_return,
                body_context,
                typed_boundary=(
                    expected_return is not None
                    and isinstance(declaration, ast.FunctionDecl)
                ),
            )
        finally:
            if budget is not None:
                budget.call_depth -= 1
    finally:
        context.specializations_in_progress.discard(identity)
    if expected_return is not None and body.type != expected_return:
        raise SemanticError(f"'{owner}' returns {body.type}, expected {expected_return}")

    kind = (
        ir_module.CallableKind.FUNCTION
        if isinstance(declaration, ast.FunctionDecl)
        else ir_module.CallableKind.OPERATOR
    )
    declaration_identity = (
        f"{declaration.source_identity or context.source_unit or '<source>'}:{owner}"
    )
    metadata = ir_module.CallableMetadata(
        kind,
        owner,
        declaration_identity,
        identity,
        rendered,
    )
    definition = ir_module.Function(
        definition_name,
        concrete_parameters,
        body.type,
        body,
        identity,
        metadata,
    )
    context.callable_definitions[identity] = definition
    if budget is not None and budget_before is not None:
        context.specialization_budget_costs[identity] = (
            budget.generated_elements - budget_before[0],
            budget.operations - budget_before[1],
        )
    record = ir_module.GenericSpecialization(
        kind.value,
        owner,
        identity,
        rendered,
        body.type,
        specialization_bindings,
    )
    if record not in context.generic_specializations:
        context.generic_specializations.append(record)
    _record_callable_use(context, identity)
    return ir_expr.Call(
        definition.name,
        arguments,
        definition.return_type,
        definition.callee_identity,
        origin=body.origin,
    )


_ValueSymbol = (
    ir_module.Port
    | ir_module.RequestResponseInterface
    | ir_module.FunctionParameter
    | ir_module.Register
    | _FifoSymbol
    | _MemorySymbol
    | _RomSymbol
    | ir_module.LocalValue
    | ir_expr.Expression
)


def _check_callable_body(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    parameters: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
    *,
    typed_boundary: bool,
) -> ir_expr.Expression:
    """Type concise immutable aliases followed by one result expression.

    Bindings are deliberately aliases for already typed pure expressions.  No
    local state or new backend node is created, and the final retained call
    graph is consequently identical to the equivalent explicitly nested
    expression.
    """

    symbols = dict(parameters)
    for binding in declaration.bindings:
        if isinstance(binding, ast.TupleDestructureDecl):
            if len(binding.names) != len(set(binding.names)):
                duplicate = next(
                    name for name in binding.names
                    if binding.names.count(name) > 1
                )
                raise SemanticError(
                    f"tuple destructuring repeats binding '{duplicate}'"
                )
            collision = next(
                (name for name in binding.names if name in symbols), None
            )
            if collision is not None:
                raise SemanticError(
                    f"tuple binding '{collision}' shadows an existing immutable symbol"
                )
            value = _check_expression(binding.expression, symbols, None, context)
            if not isinstance(value.type, TupleType):
                raise SemanticError(
                    f"tuple destructuring requires a tuple value, got {value.type}"
                )
            if len(binding.names) != len(value.type.elements):
                raise SemanticError(
                    f"tuple destructuring has {len(binding.names)} bindings, "
                    f"but {value.type} has {len(value.type.elements)} components"
                )
            for index, (name, type_) in enumerate(
                zip(binding.names, value.type.elements, strict=True)
            ):
                symbols[name] = ir_expr.TupleProject(
                    value, index, type_, origin=value.origin
                )
            continue
        if binding.target in symbols:
            raise SemanticError(
                f"duplicate callable binding '{binding.target}'; callable "
                "parameters and inferred bindings are immutable"
            )
        symbols[binding.target] = _check_expression(
            binding.expression, symbols, None, context
        )
    if typed_boundary and expected is not None:
        return _check_typed_boundary(
            declaration.body, symbols, expected, context
        )
    return _check_expression(declaration.body, symbols, expected, context)


def _validate_tuple_destructure_assignment(
    declaration: ast.Assignment,
    value: ir_expr.Expression,
) -> None:
    """Validate the syntax-only hidden value before projections are typed."""

    arity = declaration.tuple_destructure_arity
    if arity is None:
        return
    if not isinstance(value.type, TupleType):
        raise SemanticError(
            f"tuple destructuring requires a tuple value, got {value.type}"
        )
    if len(value.type.elements) != arity:
        raise SemanticError(
            f"tuple destructuring has {arity} bindings, but {value.type} has "
            f"{len(value.type.elements)} components"
        )


def _select_compile_time_module_items(
    module: ast.Module,
    parameter_values: dict[str, int],
    unresolved_parameters: frozenset[str],
    type_resolver: _TypeResolver,
    compile_time_budget: _CompileTimeBudget,
) -> ast.Module:
    """Select module-item ``if`` branches before ordinary module checking."""

    public = (
        ast.PortDecl,
        ast.RequestResponseDecl,
        ast.AggregateInterfaceDecl,
        ast.ProtocolDecl,
        ast.TypeAlias,
        ast.StructDecl,
        ast.ImportDecl,
        ast.ModuleTimingDecl,
    )

    def validate_branch(items: tuple[object, ...]) -> None:
        for item in items:
            if isinstance(item, public) or (
                isinstance(item, tuple) and item and item[0] in {"clock", "reset"}
            ):
                raise SemanticError(
                    "compile-time module if cannot contain public ABI declarations "
                    "(ports, clocks, resets, protocols, aliases, structs, or imports)"
                )
            if isinstance(item, ast.CompileTimeIfDecl):
                validate_branch(item.when_true)
                validate_branch(item.when_false)
            elif isinstance(item, ast.GenerateBlock):
                validate_branch(item.items)

    context = _ExpressionContext(
        {},
        allow_delay=False,
        parameters=parameter_values,
        unresolved_parameters=unresolved_parameters,
        type_resolver=type_resolver,
        compile_time_budget=compile_time_budget,
    )

    selected: list[object] = []

    visible_names = {
        *[item.name for item in module.ports],
        *[item.target.split(".", 1)[0] for item in module.assignments],
        *[item.name for item in module.registers],
        *[item.name for item in module.fsms],
        *[item.target for item in module.next_assignments],
        *[item.name for item in module.request_responses],
        *[item.name for item in module.fifos],
        *[item.name for item in module.memories],
        *[item.name for item in module.roms],
        *[item.name for item in module.instances],
        *[item.name for item in module.aggregate_interfaces],
        *[item.name for item in module.generic_declarations],
        *[item.name for item in module.rules if isinstance(item, ast.RuleDecl)],
        *[item.name for item in module.protocols],
        *[item.name for item in module.type_aliases],
        *[item.name for item in module.structs],
        *module.clocks,
        *module.resets,
    }

    def substitute_path(text: str, index: str, replacement: int) -> str:
        """Replace only an explicit ``[binder]`` path segment."""

        pattern = re.compile(
            r"^(?P<head>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?P<tail>(?:\[[^\]]+\]|\.[A-Za-z_][A-Za-z0-9_]*)*)$"
        )
        match = pattern.fullmatch(text)
        if match is None:
            return text
        tail = match.group("tail")
        return re.sub(
            rf"\[{re.escape(index)}\]",
            f"[{replacement}]",
            match.group("head") + tail,
        )

    def substitute_index(value: object, index: str, replacement: int) -> object:
        if isinstance(value, ast.NameExpr) and value.name == index:
            return ast.NumberExpr(replacement, origin=value.origin)
        if isinstance(value, tuple):
            return tuple(substitute_index(item, index, replacement) for item in value)
        if is_dataclass(value):
            changes: dict[str, object] = {}
            for field in fields(value):
                if not field.init or field.name == "origin":
                    continue
                item = getattr(value, field.name)
                if field.name in {"target", "source", "destination"} and isinstance(item, str):
                    changes[field.name] = substitute_path(item, index, replacement)
                else:
                    changes[field.name] = substitute_index(item, index, replacement)
            return replace(value, **changes)
        return value

    def select(items: tuple[object, ...], active_binders: tuple[str, ...] = ()) -> None:
        for item in items:
            if isinstance(item, ast.CompileTimeIfDecl):
                validate_branch(item.when_true)
                validate_branch(item.when_false)
                branch = item.when_true if _compile_time_condition(item.condition, {}, context) else item.when_false
                select(branch, active_binders)
            elif isinstance(item, ast.GenerateBlock):
                if item.index in visible_names or item.index in active_binders:
                    raise SemanticError(
                        f"compile-time generation binder '{item.index}' shadows an existing visible symbol"
                    )
                start = _resolve_range_bound(item.start, context, "generated instance")
                stop = _resolve_range_bound(item.stop, context, "generated instance")
                if stop < start:
                    raise SemanticError(
                        f"generated range {start}..{stop} is reversed"
                    )
                length = stop - start
                if length > _FUNCTIONAL_RANGE_LIMIT:
                    raise SemanticError(
                        f"generated range expands to {length} elements; "
                        f"the compile-time generation limit is {_FUNCTIONAL_RANGE_LIMIT}"
                    )
                if length:
                    compile_time_budget.generated_elements += length
                    if compile_time_budget.generated_elements > _TOTAL_GENERATED_LIMIT:
                        raise SemanticError(
                            f"compile-time generation exceeds {_TOTAL_GENERATED_LIMIT} elements"
                        )
                for value in range(start, stop):
                    select(
                        tuple(substitute_index(child, item.index, value) for child in item.items),
                        (*active_binders, item.index),
                    )
            else:
                selected.append(item)

    source_items = module.ordered_items
    if not source_items:
        # Hand-built ASTs from older clients predate ``ordered_items``.  Keep
        # a complete compatibility view here rather than selecting only the
        # compile-time nodes (which would silently drop ordinary locals,
        # ports, and state).  Parsed source always takes the exact source
        # order above; this fallback is deliberately deterministic and is
        # only for legacy AST construction.
        fallback: list[object] = []
        fallback.extend(module.ports)
        clock_specs = module.clock_physical or tuple(
            ast.ClockPhysicalDecl(name) for name in module.clocks
        )
        reset_specs = module.reset_physical or tuple(
            ast.ResetPhysicalDecl(name, domain)
            for name, domain in module.reset_domains
        )
        fallback.extend(("clock", item.name, item) for item in clock_specs)
        fallback.extend(
            ("reset", item.name, item.clock, item) for item in reset_specs
        )
        fallback.extend(module.assignments)
        fallback.extend(module.registers)
        fallback.extend(module.next_assignments)
        fallback.extend(module.request_responses)
        fallback.extend(module.connections)
        fallback.extend(module.connection_chains)
        fallback.extend(module.csr_blocks)
        fallback.extend(module.rules)
        fallback.extend(module.rule_priorities)
        fallback.extend(module.fsms)
        fallback.extend(module.fifos)
        fallback.extend(module.memories)
        fallback.extend(module.roms)
        fallback.extend(module.arbiters)
        fallback.extend(module.contracts)
        fallback.extend(module.verification_goals)
        fallback.extend(module.verification_scopes)
        fallback.extend(module.instances)
        fallback.extend(module.aggregate_interfaces)
        fallback.extend(module.generic_declarations)
        fallback.extend(module.compile_time_ifs)
        fallback.extend(module.generate_blocks)
        source_items = tuple(fallback)
    select(source_items)

    # ``fsm`` is a syntax-only declaration.  Expand it after compile-time
    # selection, but before priority-block normalization, into the existing
    # enum-register/rule model.  This deliberately creates neither a second
    # scheduler nor a backend-visible FSM node.
    fsm_expanded: list[object] = []
    seen_fsm_names: set[str] = set()

    def actions_in(
        declaration: object,
    ) -> tuple[ast.NextAssignment | ast.ResourceAction, ...]:
        if isinstance(declaration, (ast.RuleDecl, ast.AnonymousRuleDecl)):
            return declaration.actions
        if isinstance(declaration, ast.PriorityBlockDecl):
            actions: list[ast.NextAssignment | ast.ResourceAction] = []
            for arm in declaration.arms:
                if isinstance(arm, ast.PriorityRuleArm):
                    actions.extend(arm.actions)
                elif isinstance(arm, ast.PriorityBlockDecl):
                    actions.extend(actions_in(arm))
            return tuple(actions)
        return ()

    for item in selected:
        if not isinstance(item, ast.FsmDecl):
            fsm_expanded.append(item)
            continue
        if item.name in seen_fsm_names:
            raise SemanticError(f"duplicate FSM '{item.name}'")
        seen_fsm_names.add(item.name)
        enum_type = type_resolver.resolve(item.type_name)
        if not isinstance(enum_type, EnumType):
            raise SemanticError(
                f"FSM '{item.name}' state type must be an enum, got {enum_type}"
            )
        if item.initial_member not in enum_type.members:
            raise SemanticError(
                f"FSM '{item.name}' initial state '{item.initial_member}' is not "
                f"a member of enum '{enum_type.name}'"
            )
        state_names = tuple(state.member for state in item.states)
        duplicate_state = next(
            (name for name in state_names if state_names.count(name) > 1), None
        )
        if duplicate_state is not None:
            raise SemanticError(
                f"FSM '{item.name}' defines state '{duplicate_state}' more than once"
            )
        unknown_state = next(
            (name for name in state_names if name not in enum_type.members), None
        )
        if unknown_state is not None:
            raise SemanticError(
                f"FSM '{item.name}' state '{unknown_state}' is not a member "
                f"of enum '{enum_type.name}'"
            )
        missing_states = tuple(
            member for member in enum_type.members if member not in state_names
        )
        if missing_states:
            raise SemanticError(
                f"FSM '{item.name}' is missing enum state(s): "
                + ", ".join(missing_states)
            )
        if any(
            isinstance(other, ast.RegisterDecl) and other.name == item.name
            for other in selected
        ):
            raise SemanticError(
                f"FSM '{item.name}' conflicts with an explicit register of the same name"
            )
        for other in selected:
            if isinstance(other, ast.NextAssignment) and other.target == item.name:
                raise SemanticError(
                    f"FSM register '{item.name}' cannot be written outside its transitions"
                )
            if any(
                isinstance(action, ast.NextAssignment)
                and action.target == item.name
                for action in actions_in(other)
            ):
                raise SemanticError(
                    f"FSM register '{item.name}' cannot be written outside its transitions"
                )

        initial = ast.FieldExpr(
            ast.NameExpr(enum_type.name, origin=item.origin),
            item.initial_member,
            origin=item.origin,
        )
        fsm_expanded.append(ast.RegisterDecl(item.name, item.type_name, initial))
        identity_prefix = (
            f"{module.source_identity or module.name}|{module.source_hash or ''}|"
            f"{tuple((parameter.name, parameter.kind, parameter.default) for parameter in module.parameters)}|"
            f"{item.origin.render() if item.origin is not None else item.name}"
        )
        for state_ordinal, state in enumerate(item.states):
            if state.hold:
                if state.transitions or state.priority:
                    raise SemanticError(
                        f"FSM '{item.name}' state '{state.member}' cannot combine hold and transitions"
                    )
                continue
            if not state.transitions:
                raise SemanticError(
                    f"FSM '{item.name}' state '{state.member}' requires hold or a transition"
                )
            if state.priority and len(state.transitions) < 2:
                raise SemanticError(
                    f"FSM '{item.name}' priority state '{state.member}' requires at least two transitions"
                )
            if not state.priority and len(state.transitions) != 1:
                raise SemanticError(
                    f"FSM '{item.name}' state '{state.member}' has multiple transitions; use priority"
                )
            previous_rule: str | None = None
            for transition_ordinal, transition in enumerate(state.transitions):
                if transition.target not in enum_type.members:
                    raise SemanticError(
                        f"FSM '{item.name}' transition target '{transition.target}' "
                        f"is not a member of enum '{enum_type.name}'"
                    )
                if state.priority and transition.guard is None:
                    raise SemanticError(
                        f"FSM '{item.name}' priority transitions require explicit when guards"
                    )
                if any(
                    isinstance(action, ast.NextAssignment)
                    and action.target == item.name
                    for action in transition.actions
                ):
                    raise SemanticError(
                        f"FSM register '{item.name}' cannot be written explicitly inside a transition"
                    )
                state_value = ast.FieldExpr(
                    ast.NameExpr(enum_type.name, origin=state.origin),
                    state.member,
                    origin=state.origin,
                )
                state_guard: ast.Expression = ast.BinaryExpr(
                    ast.BinaryOperator.EQUAL,
                    ast.NameExpr(item.name, origin=state.origin),
                    state_value,
                    origin=state.origin,
                )
                if transition.guard is not None:
                    state_guard = ast.BinaryExpr(
                        ast.BinaryOperator.BIT_AND,
                        state_guard,
                        transition.guard,
                        origin=transition.origin,
                    )
                target_value = ast.FieldExpr(
                    ast.NameExpr(enum_type.name, origin=transition.origin),
                    transition.target,
                    origin=transition.origin,
                )
                payload = (
                    f"{identity_prefix}|state:{state.member}:{state_ordinal}|"
                    f"transition:{transition_ordinal}"
                )
                rule_name = "__fsm_" + hashlib.sha256(
                    payload.encode()
                ).hexdigest()[:20]
                fsm_expanded.append(ast.RuleDecl(
                    rule_name,
                    state_guard,
                    (
                        ast.NextAssignment(item.name, target_value),
                        *transition.actions,
                    ),
                    transition.origin,
                ))
                if previous_rule is not None:
                    fsm_expanded.append(ast.RulePriority(previous_rule, rule_name))
                previous_rule = rule_name
    selected = fsm_expanded

    # Concise priority blocks are normalized only after compile-time selection,
    # so dead arms cannot contribute rule names, conflicts, or priority edges.
    # The result is ordinary Rule/RulePriority AST, which keeps every later
    # semantic, backend, simulator, and formal path unchanged.
    expanded: list[object] = []
    generated_priorities: list[ast.RulePriority] = []
    generated_anonymous_names: set[str] = set()
    for item in selected:
        if not isinstance(item, ast.PriorityBlockDecl):
            expanded.append(item)
            continue
        if len(item.arms) < 2:
            raise SemanticError(
                "priority block requires at least two arms; use a single when rule instead"
            )
        block_identity = (
            f"{module.source_identity or module.name}|{module.source_hash or ''}|"
            f"{item.origin.render() if item.origin is not None else 'priority'}"
        )
        previous: str | None = None
        labels: set[str] = set()
        for ordinal, arm in enumerate(item.arms):
            if isinstance(arm, ast.PriorityBlockDecl):
                raise SemanticError("nested priority blocks are not supported")
            if not isinstance(arm, ast.PriorityRuleArm):
                raise SemanticError("invalid priority block arm")
            if arm.guard is None:
                raise SemanticError("priority block arm requires a when guard")
            if not arm.actions:
                raise SemanticError("priority block arm cannot be empty")
            if arm.label is None:
                payload = f"{block_identity}|arm:{ordinal}"
                name = "__priority_rule_" + hashlib.sha256(payload.encode()).hexdigest()[:16]
                generated_anonymous_names.add(name)
            else:
                name = arm.label
            if name in labels:
                raise SemanticError(
                    f"duplicate priority block rule label '{name}'"
                )
            labels.add(name)
            expanded.append(ast.RuleDecl(name, arm.guard, arm.actions, arm.origin))
            if previous is not None:
                generated_priorities.append(ast.RulePriority(previous, name))
            previous = name
    expanded.extend(generated_priorities)
    generated_edge_set = set(generated_priorities)
    for item in expanded:
        if (
            isinstance(item, ast.RulePriority)
            and item not in generated_edge_set
            and (
                item.higher in generated_anonymous_names
                or item.lower in generated_anonymous_names
            )
        ):
            raise SemanticError(
                "anonymous priority-block arms cannot be referenced by explicit priority declarations"
            )
    selected = expanded

    # Rebuild compatibility category views from the selected source order.
    # This is the key distinction from the previous append-by-category logic:
    # local bindings and side-effecting declarations retain their source order.
    fields_to_extend: dict[str, list[object]] = {
        name: []
        for name in (
            "ports", "assignments", "clocks", "resets", "reset_domains",
            "clock_physical", "reset_physical",
            "registers", "next_assignments", "request_responses",
            "connections", "csr_blocks", "rules", "rule_priorities", "fifos",
            "connection_chains",
            "memories", "roms", "arbiters", "contracts", "instances",
            "verification_goals", "verification_scopes",
            "aggregate_interfaces", "generic_declarations", "generate_blocks",
        )
    }
    for item in selected:
        if isinstance(item, ast.PortDecl):
            fields_to_extend["ports"].append(item)
        elif isinstance(item, ast.AggregateInterfaceDecl):
            fields_to_extend["aggregate_interfaces"].append(item)
        elif isinstance(item, ast.Assignment):
            fields_to_extend["assignments"].append(item)
        elif isinstance(item, ast.InstanceDecl):
            fields_to_extend["instances"].append(item)
        elif isinstance(item, ast.GenericDeclaration):
            fields_to_extend["generic_declarations"].append(item)
        elif isinstance(item, ast.RegisterDecl):
            fields_to_extend["registers"].append(item)
        elif isinstance(item, ast.NextAssignment):
            fields_to_extend["next_assignments"].append(item)
        elif isinstance(item, ast.RequestResponseDecl):
            fields_to_extend["request_responses"].append(item)
        elif isinstance(item, ast.ConnectionDecl):
            fields_to_extend["connections"].append(item)
        elif isinstance(item, ast.ConnectionChainDecl):
            fields_to_extend["connection_chains"].append(item)
        elif isinstance(item, ast.CsrBlockDecl):
            fields_to_extend["csr_blocks"].append(item)
        elif isinstance(item, (ast.RuleDecl, ast.AnonymousRuleDecl)):
            fields_to_extend["rules"].append(item)
        elif isinstance(item, ast.RulePriority):
            fields_to_extend["rule_priorities"].append(item)
        elif isinstance(item, ast.FifoDecl):
            fields_to_extend["fifos"].append(item)
        elif isinstance(item, ast.MemoryDecl):
            fields_to_extend["memories"].append(item)
        elif isinstance(item, ast.RomDecl):
            fields_to_extend["roms"].append(item)
        elif isinstance(item, ast.ArbiterDecl):
            fields_to_extend["arbiters"].append(item)
        elif isinstance(item, ast.ContractDecl):
            fields_to_extend["contracts"].append(item)
        elif isinstance(item, ast.VerificationGoalDecl):
            fields_to_extend["verification_goals"].append(item)
        elif isinstance(item, ast.VerificationScopeDecl):
            fields_to_extend["verification_scopes"].append(item)
        elif isinstance(item, ast.ModuleTimingDecl):
            # The one public timing block remains on ``module.timing``.  It is
            # retained in source order for provenance, but is not a repeatable
            # category view and therefore needs no list reconstruction here.
            continue
        elif isinstance(item, tuple) and item and item[0] == "clock":
            fields_to_extend["clocks"].append(item[1])
            fields_to_extend["clock_physical"].append(item[2])
        elif isinstance(item, tuple) and item and item[0] == "reset":
            fields_to_extend["resets"].append(item[1])
            fields_to_extend["reset_domains"].append((item[1], item[2]))
            fields_to_extend["reset_physical"].append(item[3])
        else:
            raise SemanticError(
                f"unsupported declaration in compile-time module if: {type(item).__name__}"
            )
    fields_to_extend["clocks"] = list(dict.fromkeys(fields_to_extend["clocks"]))
    fields_to_extend["resets"] = list(dict.fromkeys(fields_to_extend["resets"]))
    return replace(module, compile_time_ifs=(), fsms=(), ordered_items=tuple(selected), **{
        name: tuple(values) for name, values in fields_to_extend.items()
    })


def _reference_parts(type_name: ast.TypeSyntax) -> tuple[str, tuple[ast.SpecializationArgument, ...]]:
    if not isinstance(type_name, ast.TypeName):
        return ("", ())
    text = type_name.text
    if "<" not in text:
        return (text, ())
    base, body = text.split("<", 1)
    body = body[:-1]
    values: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    for index, character in enumerate(body):
        if character == "<":
            angle_depth += 1
        elif character == ">":
            angle_depth -= 1
        elif character == "(":
            paren_depth += 1
        elif character == ")":
            paren_depth -= 1
        elif character == "," and angle_depth == 0 and paren_depth == 0:
            values.append(body[start:index])
            start = index + 1
    values.append(body[start:])
    return (
        base,
        tuple(
            ast.SpecializationArgument(None, int(value) if value.isdigit() else value)
            for value in values
        ),
    )


def _normalize_concise_module_items(
    module: ast.Module,
    type_resolver: _TypeResolver,
    inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None,
) -> ast.Module:
    """Resolve concise surface declarations into the pre-existing AST kinds."""

    known_modules = {item.name for item in (*module.submodules, module)}
    known_protocols = {item.name for item in module.protocols}
    instances = list(module.instances)
    interfaces = list(module.aggregate_interfaces)
    assignments = list(module.assignments)
    destructure_ordinal = 0

    occupied_value_names = {
        *[item.name for item in module.ports],
        *[item.name for item in module.registers],
        *[item.name for item in module.instances],
        *[item.name for item in module.fifos],
        *[item.name for item in module.memories],
        *[item.name for item in module.roms],
        *[item.name for item in module.aggregate_interfaces],
        *[item.name for item in module.generic_declarations],
        *[item.target for item in module.assignments if "." not in item.target],
        *module.clocks,
        *module.resets,
    }

    def expand_port(declaration: ast.PortDecl) -> tuple[object, ...]:
        """Expand grouped/inline ports into the ordinary AST declarations.

        This is deliberately a syntax normalization step.  No new port or
        binding semantics are introduced here: an inline output becomes the
        same PortDecl plus the same Assignment that a verbose source would
        have contained.
        """
        names = declaration.names or (declaration.name,)
        if declaration.initializer is not None and len(names) > 1:
            raise SemanticError(
                "grouped port declarations cannot have an initializer; "
                "use one inline output declaration per driven output"
            )
        if declaration.initializer is not None:
            if declaration.direction is ast.Direction.INPUT:
                raise SemanticError(
                    f"input port '{declaration.name}' cannot have an initializer; "
                    "inputs are driven by the parent/environment"
                )
            if isinstance(declaration.type_name, ast.InterfaceTypeName):
                raise SemanticError(
                    f"protocol port '{declaration.name}' cannot have an initializer"
                )
        expanded: list[object] = []
        for name in names:
            expanded.append(replace(
                declaration,
                name=name,
                names=(),
                initializer=None,
            ))
        if declaration.initializer is not None:
            expanded.append(ast.Assignment(
                declaration.name, declaration.initializer,
                origin=declaration.origin,
            ))
        return tuple(expanded)

    def normalize_generic(declaration: ast.GenericDeclaration) -> object:
        reference, parsed_arguments = _reference_parts(declaration.type_name)
        arguments = declaration.specializations or parsed_arguments
        is_module = reference in known_modules
        is_protocol = reference in known_protocols
        try:
            type_resolver.resolve(declaration.type_name)
            is_value_type = True
        except SemanticError:
            is_value_type = False
        if declaration.role is not None:
            if not is_protocol:
                raise SemanticError(
                    f"concise declaration '{declaration.name}' references unknown "
                    f"protocol '{reference}'"
                )
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError("protocol endpoints cannot have instance arrays or inline bindings")
            inferred_domain = (
                module.clocks[0]
                if len(module.clocks) == 1
                else (
                    inherited_domain.clock
                    if isinstance(inherited_domain, ir_cdc.ClockDomain)
                    else inherited_domain[0]
                )
                if not module.clocks and inherited_domain is not None
                else declaration.domain
            )
            result = ast.AggregateInterfaceDecl(
                declaration.name, reference, arguments,
                declaration.role, inferred_domain,
            )
            interfaces.append(result)
            return result
        categories = sum((is_module, is_value_type, is_protocol))
        if categories > 1:
            raise SemanticError(
                f"concise declaration '{declaration.name}' is ambiguous for '{reference}'; "
                "use explicit 'inst' for a module instance"
            )
        if is_module:
            if declaration.initializer is not None:
                raise SemanticError(f"module instance '{declaration.name}' cannot have a value initializer")
            if declaration.domain is not None:
                raise SemanticError("module instance declarations do not accept @domain")
            result = ast.InstanceDecl(
                declaration.name, reference, arguments,
                declaration.array_length, declaration.bindings,
            )
            instances.append(result)
            return result
        elif is_value_type:
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError(f"immutable value '{declaration.name}' cannot have instance options")
            if declaration.initializer is None:
                raise SemanticError(f"immutable value '{declaration.name}' requires an initializer")
            result = ast.Assignment(declaration.name, declaration.initializer, declaration.type_name)
            assignments.append(result)
            return result
        elif is_protocol:
            raise SemanticError(
                f"protocol declaration '{declaration.name}' requires an explicit role"
            )
        else:
            raise SemanticError(
                f"concise declaration '{declaration.name}' has unknown type, module, "
                f"or protocol '{reference or declaration.type_name}'"
            )

    # Anonymous rules occur in the ordered source stream as well as in the
    # historical category tuple.  Build one identity-preserving replacement
    # map before normalizing either view so source-order selection cannot put a
    # raw AnonymousRuleDecl back into the semantic analyzer.
    anonymous_rules: dict[int, ast.RuleDecl] = {}
    anonymous_ordinal = 0

    def register_anonymous(items: tuple[object, ...]) -> None:
        nonlocal anonymous_ordinal
        for item in items:
            if isinstance(item, ast.AnonymousRuleDecl):
                if id(item) in anonymous_rules:
                    continue
                span = item.origin.render() if item.origin is not None else f"ordinal:{anonymous_ordinal}"
                parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in module.parameters
                )
                payload = (
                    f"{module.source_identity or module.name}|{module.source_hash or ''}|"
                    f"{parameters}|{span}|{anonymous_ordinal}"
                )
                identity = hashlib.sha256(payload.encode()).hexdigest()[:16]
                anonymous_rules[id(item)] = ast.RuleDecl(
                    f"__anonymous_rule_{identity}", item.guard, item.actions, item.origin
                )
                anonymous_ordinal += 1
            elif isinstance(item, ast.CompileTimeIfDecl):
                register_anonymous(item.when_true)
                register_anonymous(item.when_false)
            elif isinstance(item, ast.GenerateBlock):
                register_anonymous(item.items)

    if module.ordered_items:
        register_anonymous(module.ordered_items)
    else:
        register_anonymous(tuple(module.rules))

    def normalize_items(items: tuple[object, ...]) -> tuple[object, ...]:
        nonlocal destructure_ordinal
        normalized: list[object] = []
        for item in items:
            if isinstance(item, ast.PortDecl):
                normalized.extend(expand_port(item))
            elif isinstance(item, ast.GenericDeclaration):
                normalized.append(normalize_generic(item))
            elif isinstance(item, ast.StructDestructureDecl):
                resolved = type_resolver.resolve(item.type_name)
                if not isinstance(resolved, StructType):
                    raise SemanticError(
                        "immutable destructuring requires a nominal struct type, "
                        f"got {resolved}"
                    )
                if len(item.fields) != len(set(item.fields)):
                    duplicate = next(
                        name for name in item.fields if item.fields.count(name) > 1
                    )
                    raise SemanticError(
                        f"struct destructuring repeats field '{duplicate}'"
                    )
                declared_fields = tuple(field.name for field in resolved.fields)
                unknown = tuple(name for name in item.fields if name not in declared_fields)
                missing = tuple(name for name in declared_fields if name not in item.fields)
                if unknown or missing:
                    details: list[str] = []
                    if unknown:
                        details.append("unknown: " + ", ".join(unknown))
                    if missing:
                        details.append("missing: " + ", ".join(missing))
                    raise SemanticError(
                        f"destructuring of '{resolved.name}' must name every field "
                        f"exactly once ({'; '.join(details)})"
                    )
                collision = next(
                    (name for name in item.fields if name in occupied_value_names),
                    None,
                )
                if collision is not None:
                    raise SemanticError(
                        f"destructured field '{collision}' shadows an existing symbol"
                    )
                span = item.origin.render() if item.origin is not None else str(destructure_ordinal)
                hidden_hash = hashlib.sha256(
                    f"{module.source_identity or module.name}|{span}|{destructure_ordinal}".encode()
                ).hexdigest()[:16]
                hidden_name = f"__destructure_{hidden_hash}"
                normalized.append(ast.Assignment(
                    hidden_name,
                    item.expression,
                    item.type_name,
                    origin=item.origin,
                ))
                for name in item.fields:
                    normalized.append(ast.Assignment(
                        name,
                        ast.FieldExpr(
                            ast.NameExpr(hidden_name, origin=item.origin),
                            name,
                            origin=item.origin,
                        ),
                        origin=item.origin,
                    ))
                    occupied_value_names.add(name)
                destructure_ordinal += 1
            elif isinstance(item, ast.TupleDestructureDecl):
                if len(item.names) != len(set(item.names)):
                    duplicate = next(
                        name for name in item.names
                        if item.names.count(name) > 1
                    )
                    raise SemanticError(
                        f"tuple destructuring repeats binding '{duplicate}'"
                    )
                collision = next(
                    (name for name in item.names if name in occupied_value_names),
                    None,
                )
                if collision is not None:
                    raise SemanticError(
                        f"tuple binding '{collision}' shadows an existing symbol"
                    )
                span = (
                    item.origin.render()
                    if item.origin is not None else str(destructure_ordinal)
                )
                hidden_hash = hashlib.sha256(
                    f"{module.source_identity or module.name}|{span}|"
                    f"{destructure_ordinal}|tuple".encode()
                ).hexdigest()[:16]
                hidden_name = f"__tuple_destructure_{hidden_hash}"
                normalized.append(ast.Assignment(
                    hidden_name,
                    item.expression,
                    origin=item.origin,
                    tuple_destructure_arity=len(item.names),
                ))
                for index, name in enumerate(item.names):
                    normalized.append(ast.Assignment(
                        name,
                        ast.IndexExpr(
                            ast.NameExpr(hidden_name, origin=item.origin),
                            index,
                            origin=item.origin,
                        ),
                        origin=item.origin,
                    ))
                    occupied_value_names.add(name)
                destructure_ordinal += 1
            elif isinstance(item, ast.AnonymousRuleDecl):
                normalized.append(anonymous_rules.get(id(item), item))
            elif isinstance(item, ast.CompileTimeIfDecl):
                normalized.append(replace(
                    item,
                    when_true=normalize_items(item.when_true),
                    when_false=normalize_items(item.when_false),
                ))
            elif isinstance(item, ast.GenerateBlock):
                normalized.append(replace(item, items=normalize_items(item.items)))
            else:
                normalized.append(item)
        return tuple(normalized)

    if module.ordered_items:
        ordered_items = normalize_items(module.ordered_items)
    else:
        # ASTs constructed directly by older tests do not carry the ordered
        # view.  Keep their historical category order as a compatibility
        # fallback; parsed source always takes the source-ordered path above.
        fallback: list[object] = []
        fallback.extend(module.ports)
        clock_specs = module.clock_physical or tuple(
            ast.ClockPhysicalDecl(name) for name in module.clocks
        )
        reset_specs = module.reset_physical or tuple(
            ast.ResetPhysicalDecl(name, domain)
            for name, domain in module.reset_domains
        )
        fallback.extend(("clock", item.name, item) for item in clock_specs)
        fallback.extend(
            ("reset", item.name, item.clock, item) for item in reset_specs
        )
        fallback.extend(module.assignments)
        fallback.extend(module.registers)
        fallback.extend(module.next_assignments)
        fallback.extend(module.request_responses)
        fallback.extend(module.connections)
        fallback.extend(module.connection_chains)
        fallback.extend(module.csr_blocks)
        fallback.extend(module.rules)
        fallback.extend(module.rule_priorities)
        fallback.extend(module.fsms)
        fallback.extend(module.fifos)
        fallback.extend(module.memories)
        fallback.extend(module.roms)
        fallback.extend(module.arbiters)
        fallback.extend(module.contracts)
        fallback.extend(module.instances)
        fallback.extend(module.aggregate_interfaces)
        fallback.extend(module.generic_declarations)
        fallback.extend(module.generate_blocks)
        ordered_items = normalize_items(tuple(fallback))

    default_domain = (
        module.clocks[0]
        if len(module.clocks) == 1
        else (
            inherited_domain.clock
            if isinstance(inherited_domain, ir_cdc.ClockDomain)
            else inherited_domain[0]
        )
        if not module.clocks and inherited_domain is not None
        else None
    )
    normalized_interfaces: list[ast.AggregateInterfaceDecl] = []
    for declaration in interfaces:
        domain = declaration.domain
        if domain is None:
            if len(module.clocks) > 1:
                raise SemanticError(
                    f"protocol endpoint '{declaration.name}' requires an explicit domain"
                )
            domain = default_domain
        normalized_interfaces.append(replace(declaration, domain=domain))

    normalized_rules: list[ast.RuleDecl] = []
    for ordinal, declaration in enumerate(module.rules):
        if isinstance(declaration, ast.RuleDecl):
            normalized_rules.append(declaration)
            continue
        normalized_rules.append(anonymous_rules.get(id(declaration), declaration))
    if module.ordered_items:
        normalized_ports = tuple(
            item for item in ordered_items if isinstance(item, ast.PortDecl)
        )
        normalized_assignments = tuple(
            item for item in ordered_items if isinstance(item, ast.Assignment)
        )
    else:
        normalized_ports = tuple(
            item for item in ordered_items if isinstance(item, ast.PortDecl)
        )
        normalized_assignments = tuple(
            item for item in ordered_items if isinstance(item, ast.Assignment)
        )
    return replace(
        module,
        instances=tuple(instances),
        ports=normalized_ports,
        assignments=normalized_assignments or tuple(assignments),
        aggregate_interfaces=tuple(normalized_interfaces),
        generic_declarations=(),
        rules=tuple(normalized_rules),
        ordered_items=ordered_items,
    )


_QUALIFIED_IMPORT_MEMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\."
    r"(?P<member>[A-Za-z_][A-Za-z0-9_]*)"
)


def _normalize_qualified_imports(
    module: ast.Module,
    resolved_by_path: dict[str, object],
    *,
    source_unit: str | None,
    source_digest: str | None,
) -> ast.Module:
    """Erase source-local import qualifiers before ordinary semantic typing.

    Import aliases are deliberately not declaration renames and never become
    runtime namespace values.  A qualifier resolves a declaration exported by
    exactly the directly imported logical source, then the existing stable
    declaration name/identity continues through every semantic and backend IR.
    """

    alias_declarations = tuple(item for item in module.imports if item.alias)
    if not alias_declarations:
        return module

    aliases = tuple(item.alias for item in alias_declarations)
    duplicate = next(
        (alias for alias in aliases if aliases.count(alias) > 1),
        None,
    )
    if duplicate is not None:
        declaration = next(
            item for item in alias_declarations if item.alias == duplicate
        )
        raise SemanticError(
            f"duplicate import alias '{duplicate}'",
            code="ZL-IMPORT-ALIAS-DUPLICATE",
            primary=(
                SourceOrigin(
                    declaration.origin,
                    "import alias",
                    source_unit,
                    source_digest,
                )
                if declaration.origin is not None
                else None
            ),
            fixes=("choose a unique import alias",),
        )

    exports: dict[str, dict[str, frozenset[str]]] = {}
    for declaration in alias_declarations:
        assert declaration.alias is not None
        record = resolved_by_path[declaration.path]
        source = record.ast
        exports[declaration.alias] = {
            "type": frozenset(
                item.name
                for group in (
                    source.type_aliases,
                    source.structs,
                    source.enums,
                    source.tagged_unions,
                )
                for item in group
            ),
            "function": frozenset(item.name for item in source.functions),
            "constructor": frozenset(item.name for item in source.structs),
        }

    # An ordinary direct import retains the historical unqualified surface.
    # Only names available exclusively through aliased imports are hidden.
    unqualified_types: set[str] = set()
    unqualified_functions: set[str] = set()
    unqualified_constructors: set[str] = set()
    for declaration in module.imports:
        if declaration.alias is not None:
            continue
        source = resolved_by_path[declaration.path].ast
        unqualified_types.update(
            item.name
            for group in (
                source.type_aliases,
                source.structs,
                source.enums,
                source.tagged_unions,
            )
            for item in group
        )
        unqualified_functions.update(item.name for item in source.functions)
        unqualified_constructors.update(item.name for item in source.structs)

    hidden_types = set().union(*(item["type"] for item in exports.values()))
    hidden_types.difference_update(unqualified_types)
    hidden_functions = set().union(
        *(item["function"] for item in exports.values())
    )
    hidden_functions.difference_update(unqualified_functions)
    hidden_constructors = set().union(
        *(item["constructor"] for item in exports.values())
    )
    hidden_constructors.difference_update(unqualified_constructors)

    def fail_unknown_alias(alias: str, origin: object | None = None) -> None:
        raise SemanticError(
            f"unknown import alias '{alias}'",
            code="ZL-IMPORT-ALIAS-UNKNOWN",
            primary=(
                SourceOrigin(origin, "qualified import", source_unit, source_digest)
                if origin is not None
                else None
            ),
            fixes=("declare the alias with 'import <logical.module> as <alias>'",),
        )

    def resolve_member(
        alias: str,
        member: str,
        namespace: str,
        origin: object | None = None,
    ) -> str:
        if alias not in exports:
            fail_unknown_alias(alias, origin)
        if member not in exports[alias][namespace]:
            raise SemanticError(
                f"logical module alias '{alias}' has no {namespace} member '{member}'",
                code="ZL-IMPORT-MEMBER-UNKNOWN",
                primary=(
                    SourceOrigin(
                        origin,
                        "qualified import",
                        source_unit,
                        source_digest,
                    )
                    if origin is not None
                    else None
                ),
            )
        return member

    def normalize_type_name(value: ast.TypeName) -> ast.TypeName:
        original = value.text

        def replace_member(match: re.Match[str]) -> str:
            return resolve_member(
                match.group("alias"), match.group("member"), "type"
            )

        normalized = _QUALIFIED_IMPORT_MEMBER.sub(replace_member, original)
        unqualified_probe = _QUALIFIED_IMPORT_MEMBER.sub("", original)
        hidden = next(
            (
                name
                for name in sorted(hidden_types)
                if re.search(rf"(?<![A-Za-z0-9_.]){re.escape(name)}\b", unqualified_probe)
            ),
            None,
        )
        if hidden is not None:
            raise SemanticError(
                f"type '{hidden}' requires its import alias",
                code="ZL-IMPORT-ALIAS-REQUIRED",
            )
        return ast.TypeName(normalized)

    def walk(value: object) -> object:
        if isinstance(value, ast.ImportDecl):
            return value
        if isinstance(value, ast.TypeName):
            return normalize_type_name(value)
        if isinstance(value, ast.CallExpr):
            function = value.function
            if "." in function:
                alias, member = function.split(".", 1)
                function = resolve_member(
                    alias, member, "function", value.origin
                )
            elif function in hidden_functions:
                raise SemanticError(
                    f"function '{function}' requires its import alias",
                    code="ZL-IMPORT-ALIAS-REQUIRED",
                    primary=(
                        SourceOrigin(
                            value.origin,
                            "function call",
                            source_unit,
                            source_digest,
                        )
                        if value.origin is not None
                        else None
                    ),
                )
            return replace(
                value,
                function=function,
                arguments=tuple(walk(item) for item in value.arguments),
                specializations=tuple(walk(item) for item in value.specializations),
            )
        if isinstance(value, ast.StructConstructExpr):
            name = value.struct_name
            if "." in name:
                alias, member = name.split(".", 1)
                name = resolve_member(alias, member, "constructor", value.origin)
            elif name in hidden_constructors:
                raise SemanticError(
                    f"struct constructor '{name}' requires its import alias",
                    code="ZL-IMPORT-ALIAS-REQUIRED",
                )
            return replace(
                value,
                struct_name=name,
                fields=tuple(walk(item) for item in value.fields),
            )
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            changes = {
                item.name: walk(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name != "origin"
            }
            return replace(value, **changes)
        return value

    normalized_module = walk(module)
    assert isinstance(normalized_module, ast.Module)
    return normalized_module


def analyze(
    module: ast.Module,
    *,
    exploration_results: list[object] | None = None,
    formal_config: object | None = None,
    formal_verifier: object | None = None,
    allow_sequential_protocol: bool = False,
    inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None = None,
    specialization_type_bindings: dict[str, HardwareType] | None = None,
    specialization_constant_bindings: dict[str, ir_expr.Expression] | None = None,
    specialization_callable_bindings: dict[str, _StaticCallableBinding] | None = None,
    compile_time_budget: _CompileTimeBudget | None = None,
    _compile_time_real_quantize_cache: dict[
        tuple[object, ...], _CompileTimeRealQuantization
    ] | None = None,
    source_unit: str | None = None,
    source_digest: str | None = None,
    allow_external_enum_inputs: bool = False,
    enum_identity_namespace: str | None = None,
    module_resolver: ModuleResolver | None = None,
    resolution_context: ModuleResolutionContext | None = None,
    root_module_identity: DependencyModuleIdentity | None = None,
    dependency_closure: DependencyClosure | None = None,
    _imports_premerged: bool = False,
    _instance_stack: tuple[str, ...] = (),
    _hierarchy_cache: HierarchyTraversalCache | None = None,
) -> ir_module.Module:
    """Resolve and type-check an AST module into backend-independent IR."""

    if module.name in _instance_stack:
        cycle = " -> ".join((*_instance_stack, module.name))
        raise SemanticError(f"cyclic module hierarchy is not allowed: {cycle}")
    active_instance_stack = (*_instance_stack, module.name)
    selected_hierarchy_cache = _hierarchy_cache or HierarchyTraversalCache()

    if resolution_context is not None and module_resolver is not None:
        if resolution_context.resolver is not module_resolver:
            raise SemanticError(
                "module_resolver conflicts with the active resolution_context",
                code="ZL-IMPORT-RESOLVER",
            )
    if resolution_context is None:
        resolution_context = ModuleResolutionContext(
            module_resolver or StdlibModuleResolver()
        )
    active_module_resolver = resolution_context.resolver

    # Built-in libraries are ordinary source modules.  The resolver is kept
    # deliberately narrow (compiler-shipped files only); protocol meaning is
    # obtained by parsing and analyzing those files below.
    effective_source_unit = module.source_identity or source_unit
    effective_source_digest = module.source_hash or source_digest

    def identity_for_source(
        logical_path: str | None,
    ) -> DependencyModuleIdentity | None:
        if logical_path is None:
            return root_module_identity
        if (
            root_module_identity is not None
            and root_module_identity.logical_path == logical_path
        ):
            return root_module_identity
        if dependency_closure is not None:
            return next(
                (
                    item
                    for item in dependency_closure.modules
                    if item.logical_path == logical_path
                ),
                None,
            )
        return None

    active_module_identity = identity_for_source(effective_source_unit)
    if enum_identity_namespace is None:
        enum_identity_namespace = effective_source_unit
        if enum_identity_namespace is None:
            compilation_modules = tuple(sorted({
                module.name,
                *(child.name for child in module.submodules),
            }))
            enum_identity_namespace = "compilation:" + ",".join(
                compilation_modules
            )
    module = replace(
        module,
        enums=tuple(
            replace(
                declaration,
                source_identity=(
                    declaration.source_identity or enum_identity_namespace
                ),
            )
            for declaration in module.enums
        ),
        tagged_unions=tuple(
            replace(
                declaration,
                source_identity=(
                    declaration.source_identity or enum_identity_namespace
                ),
            )
            for declaration in module.tagged_unions
        ),
    )
    direct_imports = tuple(declaration.path for declaration in module.imports)
    if len(direct_imports) != len(set(direct_imports)):
        duplicate = next(path for path in direct_imports if direct_imports.count(path) > 1)
        declaration = next(item for item in module.imports if item.path == duplicate)
        raise SemanticError(
            f"duplicate import '{duplicate}'",
            code="ZL-IMPORT-DUPLICATE",
            primary=(
                SourceOrigin(
                    declaration.origin,
                    "import",
                    effective_source_unit,
                    effective_source_digest,
                )
                if declaration.origin is not None else None
            ),
            fixes=("remove the duplicate import declaration",),
        )
    try:
        resolved_imports = active_module_resolver.resolve(
            direct_imports,
            importer=effective_source_unit,
        )
    except (ModuleResolutionError, ValueError) as exc:
        primary_decl = module.imports[0] if module.imports else None
        raise SemanticError(
            str(exc),
            code="ZL-IMPORT-RESOLVE",
            primary=(
                SourceOrigin(
                    primary_decl.origin,
                    "import",
                    effective_source_unit,
                    effective_source_digest,
                )
                if primary_decl is not None and primary_decl.origin is not None
                else None
            ),
            fixes=(
                "use an indexed logical module or update the project lock",
            ),
        ) from exc
    imported_sources: list[ast.Module] = []
    resolved_by_path = {item.logical_path: item for item in resolved_imports}
    module = _normalize_qualified_imports(
        module,
        resolved_by_path,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
    )
    source_digests = {
        item.logical_path: item.digest for item in resolved_imports
    }
    generic_dependency_items = {
        item.logical_path: item.digest for item in resolved_imports
    }
    if dependency_closure is not None:
        generic_dependency_items.update(
            {
                item.logical_path: stable_digest(item.to_data())
                for item in dependency_closure.modules
            }
        )
    generic_dependency_identity = tuple(sorted(generic_dependency_items.items()))
    if effective_source_unit is not None and effective_source_digest is not None:
        source_digests[effective_source_unit] = effective_source_digest
    # Preserve the public declaration order of direct imports for compatibility;
    # transitive dependencies follow them while dependency hashes retain their
    # canonical dependency-first order below.
    source_order = tuple(
        (*[resolved_by_path[path] for path in direct_imports],
         *[
             item
             for item in resolved_imports
             if item.logical_path not in direct_imports
         ])
    )
    if not _imports_premerged:
        for imported in source_order:
            source_module = imported.ast
            imported_sources.extend((source_module, *source_module.submodules))
    if imported_sources:
        root_owner = effective_source_unit or f"compilation:{module.name}"

        def reject_name_conflicts(
            kind: str,
            root_declarations: tuple[object, ...],
            attribute: str,
        ) -> None:
            owners: dict[str, str] = {}
            for declaration in root_declarations:
                name = getattr(declaration, "name")
                previous = owners.get(name)
                if previous is not None:
                    raise SemanticError(
                        f"conflicting {kind} declaration '{name}' in '{root_owner}'",
                        code="ZL-IMPORT-CONFLICT",
                    )
                owners[name] = root_owner
            for source in imported_sources:
                owner = source.source_identity or source.name
                for declaration in getattr(source, attribute):
                    name = getattr(declaration, "name")
                    previous = owners.get(name)
                    if previous is not None:
                        raise SemanticError(
                            f"conflicting {kind} declaration '{name}' from "
                            f"'{previous}' and '{owner}'",
                            code="ZL-IMPORT-CONFLICT",
                        )
                    owners[name] = owner

        reject_name_conflicts("type alias", module.type_aliases, "type_aliases")
        reject_name_conflicts("struct", module.structs, "structs")
        reject_name_conflicts("enum", module.enums, "enums")
        reject_name_conflicts(
            "tagged union", module.tagged_unions, "tagged_unions"
        )
        reject_name_conflicts("function", module.functions, "functions")
        reject_name_conflicts("protocol", module.protocols, "protocols")
        reject_name_conflicts(
            "module interface", module.module_interfaces, "module_interfaces"
        )

        module_owners: dict[str, str] = {}
        for source in (module, *module.submodules, *imported_sources):
            if source.declaration_only:
                continue
            owner = source.source_identity or root_owner
            previous = module_owners.get(source.name)
            if previous is not None:
                raise SemanticError(
                    f"conflicting module declaration '{source.name}' from "
                    f"'{previous}' and '{owner}'",
                    code="ZL-IMPORT-CONFLICT",
                )
            module_owners[source.name] = owner

        existing_alias_names = {item.name for item in module.type_aliases}
        imported_aliases = tuple(
            alias
            for source in imported_sources
            for alias in source.type_aliases
            if alias.name not in existing_alias_names
        )
        imported_protocols = tuple(
            protocol
            for source in imported_sources
            for protocol in source.protocols
            if protocol.name not in {item.name for item in module.protocols}
        )
        imported_module_interfaces = tuple(
            declaration
            for source in imported_sources
            for declaration in source.module_interfaces
            if declaration.name not in {
                item.name for item in module.module_interfaces
            }
        )
        existing_struct_names = {item.name for item in module.structs}
        imported_struct_list: list[ast.StructDecl] = []
        for source in imported_sources:
            for struct in source.structs:
                if struct.name in existing_struct_names:
                    continue
                existing_struct_names.add(struct.name)
                imported_struct_list.append(struct)
        imported_structs = tuple(imported_struct_list)
        imported_enums = tuple(
            replace(
                declaration,
                source_identity=(
                    declaration.source_identity or source.source_identity
                ),
            )
            for source in imported_sources
            for declaration in source.enums
        )
        imported_tagged_unions = tuple(
            replace(
                declaration,
                source_identity=(
                    declaration.source_identity or source.source_identity
                ),
            )
            for source in imported_sources
            for declaration in source.tagged_unions
        )
        existing_function_names = {item.name for item in module.functions}
        imported_function_list: list[ast.FunctionDecl] = []
        for source in imported_sources:
            for function in source.functions:
                if function.name in existing_function_names:
                    continue
                existing_function_names.add(function.name)
                imported_function_list.append(function)
        imported_operator_list = [
            operator for source in imported_sources for operator in source.operators
        ]
        module = replace(
            module,
            submodules=tuple((
                *module.submodules,
                *(source for source in imported_sources if not source.declaration_only),
            )),
            type_aliases=tuple((*module.type_aliases, *imported_aliases)),
            protocols=tuple((*module.protocols, *imported_protocols)),
            module_interfaces=tuple(
                (*module.module_interfaces, *imported_module_interfaces)
            ),
            structs=tuple((*module.structs, *imported_structs)),
            enums=tuple((*module.enums, *imported_enums)),
            tagged_unions=tuple(
                (*module.tagged_unions, *imported_tagged_unions)
            ),
            functions=tuple((*module.functions, *imported_function_list)),
            operators=tuple((*module.operators, *imported_operator_list)),
        )

    interface_names: set[str] = set()
    for declaration in module.module_interfaces:
        if declaration.name in interface_names:
            raise SemanticError(
                f"duplicate module interface declaration '{declaration.name}'",
                code="ZL-INTERFACE-DUPLICATE",
            )
        interface_names.add(declaration.name)
        # Declarations are public semantic objects, not templates checked only
        # when referenced by a module.  Reject the bounded first-slice gaps and
        # implementation-bearing port syntax even when an interface is unused.
        invalid_initializer = next(
            (
                port.name
                for port in declaration.ports
                if port.initializer is not None
            ),
            None,
        )
        if invalid_initializer is not None:
            raise SemanticError(
                f"module interface port '{invalid_initializer}' cannot have "
                "an initializer",
                code="ZL-INTERFACE-CONFORMANCE",
            )
        if declaration.request_responses:
            raise SemanticError(
                "named module interfaces do not yet accept request_response "
                "members because the requester/responder role is inferred "
                "from behavior",
                code="ZL-INTERFACE-UNSUPPORTED",
            )
        # Validate physical-domain declarations even when this named interface
        # is not applied by any module in the current source unit.
        _interface_clock_domains(declaration)
    if module.conforms_to is not None:
        active_interface = next(
            (
                item for item in module.module_interfaces
                if item.name == module.conforms_to.name
            ),
            None,
        )
        if active_interface is None:
            raise SemanticError(
                f"unknown module interface '{module.conforms_to.name}'",
                code="ZL-INTERFACE-UNKNOWN",
            )
        module = _inherit_applied_interface_surface(module, active_interface)
        if module.external_model is not None:
            if active_interface.parameters or module.conforms_to.arguments:
                raise SemanticError(
                    f"external module '{module.name}' requires a non-parameterized "
                    "named interface in this first slice",
                    code="ZL-EXTERN-UNSUPPORTED",
                )
            if active_interface.clocks or active_interface.resets:
                raise SemanticError(
                    f"external module '{module.name}' cannot expose clock/reset "
                    "in this first slice",
                    code="ZL-EXTERN-UNSUPPORTED",
                )
            if (
                active_interface.request_responses
                or active_interface.aggregate_interfaces
                or any(
                    isinstance(port.type_name, ast.InterfaceTypeName)
                    and port.type_name.kind is not ast.InterfaceKind.WIRE
                    for port in active_interface.ports
                )
            ):
                raise SemanticError(
                    f"external module '{module.name}' supports scalar wire ports only",
                    code="ZL-EXTERN-UNSUPPORTED",
                )
            timing = active_interface.timing
            if timing is not None and (
                timing.latency != 0 or timing.initiation_interval != 1
            ):
                raise SemanticError(
                    f"external module '{module.name}' requires timing latency 0 ii 1",
                    code="ZL-EXTERN-UNSUPPORTED",
                )
            inputs = tuple(
                port for port in module.ports
                if port.direction is ast.Direction.INPUT
            )
            outputs = tuple(
                port for port in module.ports
                if port.direction is ast.Direction.OUTPUT
            )
            if not inputs or len(outputs) != 1:
                raise SemanticError(
                    f"external module '{module.name}' requires one or more inputs "
                    "and exactly one output",
                    code="ZL-EXTERN-SIGNATURE",
                )
            model_call = ast.CallExpr(
                module.external_model,
                tuple(ast.NameExpr(port.name) for port in inputs),
                origin=module.external_origin,
            )
            model_assignment = ast.Assignment(
                outputs[0].name,
                model_call,
                origin=module.external_origin,
            )
            module = replace(
                module,
                assignments=(model_assignment,),
                ordered_items=(*module.ordered_items, model_assignment),
            )

    seen_imports: set[str] = {
        item.logical_path for item in resolved_imports
    }
    _validate_compile_time_parameter_declarations(module)
    protocol_schemas: list[ir_module.ProtocolSchema] = []
    type_resolver = _TypeResolver(
        module.type_aliases,
        module.structs,
        module.enums,
        module.parameters,
        {
            parameter.name: parameter.default
            for parameter in module.parameters
            if parameter.default is not None
        },
        specialization_type_bindings,
        enum_identity_namespace,
        tagged_unions=module.tagged_unions,
    )
    module = _normalize_concise_module_items(module, type_resolver, inherited_domain)
    _validate_value_parameter_shadowing(module)
    parameter_values, unresolved_parameter_names = _resolved_module_value_parameters(
        module, type_resolver
    )
    # Subsequent structural elaboration sees concrete values, including
    # values that were themselves defined by parameter expressions.
    type_resolver._parameter_values.update(parameter_values)
    if module.parameter_constraint is not None:
        constraint_context = _ExpressionContext(
            {},
            allow_delay=False,
            parameters=parameter_values,
            unresolved_parameters=unresolved_parameter_names,
            type_resolver=type_resolver,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
        )
        try:
            constraint_satisfied = _compile_time_condition(
                module.parameter_constraint, {}, constraint_context
            )
        except SemanticError as error:
            raise SemanticError(
                f"module '{module.name}' parameter constraint cannot be "
                f"discharged: {error}",
                code="ZL-SEMANTIC-PARAMETER-CONSTRAINT",
                primary=_semantic_origin(
                    module.parameter_constraint, constraint_context
                ),
                fixes=("provide concrete type/value specialization arguments",),
            ) from error
        if not constraint_satisfied:
            raise SemanticError(
                f"module '{module.name}' parameter constraint is not satisfied",
                code="ZL-SEMANTIC-PARAMETER-CONSTRAINT",
                primary=_semantic_origin(
                    module.parameter_constraint, constraint_context
                ),
                notes=(
                    "resolved values: "
                    + ", ".join(
                        f"{name}={value}"
                        for name, value in sorted(parameter_values.items())
                    ),
                ),
            )
    module = replace(
        module,
        parameters=tuple(
            replace(parameter, default=parameter_values[parameter.name])
            if parameter.kind == "value" and parameter.name in parameter_values
            else parameter
            for parameter in module.parameters
        ),
    )
    resolved_module_parameters = _resolved_module_parameter_records(
        module,
        specialization_type_bindings,
        specialization_constant_bindings,
        specialization_callable_bindings,
    )
    candidate_site_owner = candidate_specialization_identity(
        module.name, resolved_module_parameters
    )
    selected_budget = compile_time_budget or _CompileTimeBudget()
    selected_real_quantize_cache = (
        _compile_time_real_quantize_cache
        if _compile_time_real_quantize_cache is not None
        else {}
    )
    module = _select_compile_time_module_items(
        module,
        parameter_values,
        unresolved_parameter_names,
        type_resolver,
        selected_budget,
    )
    schema_names = {schema.name for schema in protocol_schemas}
    for declaration in module.protocols:
        if declaration.name in schema_names:
            raise SemanticError(f"protocol name conflict '{declaration.name}'")
        roles = tuple(dict.fromkeys(declaration.roles))
        if len(roles) != len(declaration.roles) or len(roles) < 2:
            raise SemanticError(f"protocol '{declaration.name}' must declare at least two distinct roles")
        members: list[ir_module.ProtocolMember] = []
        member_names: set[str] = set()
        protocol_type_resolver = _TypeResolver(
            module.type_aliases,
            module.structs,
            module.enums,
            declaration.parameters,
            {
                parameter.name: parameter.default
                for parameter in declaration.parameters
                if parameter.default is not None
            },
            identity_namespace=(
                effective_source_unit or module.source_identity or module.name
            ),
            tagged_unions=module.tagged_unions,
        )
        for channel in declaration.channels:
            if channel.name in member_names:
                raise SemanticError(f"duplicate protocol channel '{channel.name}'")
            if channel.source_role not in roles or channel.sink_role not in roles or channel.source_role == channel.sink_role:
                raise SemanticError(f"invalid ownership for protocol channel '{channel.name}'")
            if isinstance(channel.type_name, ast.InterfaceTypeName):
                protocol = {ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID, ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE, ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT, ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET, ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT}[channel.type_name.kind]
                try:
                    payload = protocol_type_resolver.resolve(channel.type_name.payload_type)
                except SemanticError:
                    if isinstance(channel.type_name.payload_type, ast.TypeName) and any(
                        parameter.kind == "type" and parameter.name == channel.type_name.payload_type.text
                        for parameter in declaration.parameters
                    ):
                        payload = BitType()
                    else:
                        raise
            else:
                protocol = InterfaceProtocol.WIRE
                try:
                    payload = protocol_type_resolver.resolve(channel.type_name)
                except SemanticError:
                    if isinstance(channel.type_name, ast.TypeName) and any(
                        parameter.kind == "type" and parameter.name == channel.type_name.text
                        for parameter in declaration.parameters
                    ):
                        payload = BitType()
                    else:
                        raise
            members.append(ir_module.ProtocolMember(channel.name, protocol, payload, channel.source_role, channel.sink_role, channel.domain))
            member_names.add(channel.name)
        library_path = next(
            (
                source.source_identity
                for source in imported_sources
                if declaration in source.protocols
            ),
            None,
        )
        protocol_schemas.append(
            ir_module.ProtocolSchema(
                declaration.name,
                roles,
                tuple(members),
                library_path,
                tuple((parameter.name, parameter.kind, parameter.default) for parameter in declaration.parameters),
            )
        )
        schema_names.add(declaration.name)
    struct_types = type_resolver.resolve_all()
    enum_types = type_resolver.resolve_enums()
    tagged_union_types = type_resolver.resolve_tagged_unions()
    _validate_operator_declarations(module.operators, module.structs)
    reserved_intrinsics = {
        "length", "floor_log2", "ceil_log2", "index_width", "is_power_of_two",
        "pi", "sin", "cos", "log2", "log", "parity",
        "enum_encode", "enum_valid", "enum_decode",
    }
    for declaration in module.functions:
        if declaration.name in reserved_intrinsics:
            raise SemanticError(
                f"function name '{declaration.name}' is reserved for a compiler intrinsic"
            )
    for declaration in module.operators:
        if declaration.operator in reserved_intrinsics:
            raise SemanticError(
                f"operator name '{declaration.operator}' conflicts with a compiler intrinsic"
            )
    function_signatures: dict[str, _FunctionSignature] = {}
    function_prototypes: dict[str, _FunctionPrototype] = {}
    generic_functions: dict[str, ast.FunctionDecl] = {}
    for declaration in module.functions:
        if declaration.name in function_prototypes or declaration.name in generic_functions:
            raise SemanticError(f"duplicate function '{declaration.name}'")
        if declaration.generic_parameters:
            generic_functions[declaration.name] = declaration
            continue
        parameter_names: set[str] = set()
        parameters: list[ir_module.FunctionParameter] = []
        for parameter in declaration.parameters:
            if parameter.name in parameter_names:
                raise SemanticError(
                    f"duplicate parameter '{parameter.name}' in function "
                    f"'{declaration.name}'"
                )
            parameter_names.add(parameter.name)
            parameters.append(
                ir_module.FunctionParameter(
                    parameter.name, type_resolver.resolve(parameter.type_name)
                )
            )
        declared_return_type = (
            type_resolver.resolve(declaration.return_type)
            if declaration.return_type is not None
            else None
        )
        prototype = _FunctionPrototype(
            declaration,
            tuple(parameters),
            declared_return_type,
        )
        function_prototypes[declaration.name] = prototype
        if declared_return_type is not None:
            function_signatures[declaration.name] = _FunctionSignature(
                declaration,
                prototype.parameters,
                declared_return_type,
            )

    function_catalog = _FunctionCatalog(
        function_prototypes,
        function_signatures,
    )

    pure_context = _ExpressionContext(
        function_signatures,
        allow_delay=False,
        generic_functions=generic_functions,
        function_catalog=function_catalog,
        compile_time_constants=dict(specialization_constant_bindings or {}),
        static_callables=dict(specialization_callable_bindings or {}),
        callable_definitions=_inherited_static_callable_definitions(
            specialization_callable_bindings
        ),
        operator_declarations=module.operators,
        struct_declarations=module.structs,
        structs=struct_types,
        parameters=parameter_values,
        unresolved_parameters=unresolved_parameter_names,
        type_resolver=type_resolver,
        generic_dependency_identity=generic_dependency_identity,
        compile_time_budget=selected_budget,
        compile_time_real_quantize_cache=selected_real_quantize_cache,
        formal_config=formal_config,
        formal_verifier=formal_verifier,
        exploration_results=exploration_results,
        candidate_site_owner=candidate_site_owner,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
        source_digests=source_digests,
    )
    function_catalog.context = pure_context
    for function_name in sorted(function_prototypes):
        function_catalog.resolve(function_name)

    functions: list[ir_module.Function] = []
    for declaration in module.functions:
        if declaration.generic_parameters:
            continue
        signature = function_signatures[declaration.name]
        parameter_symbols = {
            parameter.name: parameter for parameter in signature.parameters
        }
        callable_identity = stable_callee_identity(
            signature.declaration.name,
            signature.parameters,
            signature.return_type,
        )
        function_context = _context_for_callable_body(
            pure_context,
            signature.declaration.source_identity,
            callable_identity,
        )
        body = _check_callable_body(
            signature.declaration,
            parameter_symbols,
            (
                signature.return_type
                if signature.declaration.return_type is not None
                else None
            ),
            function_context,
            typed_boundary=signature.declaration.return_type is not None,
        )
        if body.type != signature.return_type:
            raise SemanticError(
                f"function '{signature.declaration.name}' returns {body.type}, "
                f"expected {signature.return_type}"
            )
        functions.append(
            ir_module.Function(
                signature.declaration.name,
                signature.parameters,
                signature.return_type,
                body,
            )
        )
    # Ordinary function bodies may call monomorphic generic/operator
    # specializations created while those bodies are typed.  Include the
    # concrete definitions in the same recursion graph: considering only the
    # source-named functions leaves a valid ``zlang_spec_*`` edge dangling and
    # can both crash the checker and miss an ordinary<->generic cycle.
    _reject_recursive_functions(
        tuple((*functions, *pure_context.callable_definitions.values()))
    )
    pure_context.function_definitions.update(
        (function.name, function) for function in functions
    )

    if len(set(module.clocks)) != len(module.clocks):
        raise SemanticError("duplicate clock declaration")
    reset_bindings = (
        module.reset_domains
        if module.reset_domains
        else tuple((name, None) for name in module.resets)
    )
    if len({name for name, _ in reset_bindings}) != len(reset_bindings):
        raise SemanticError("duplicate reset declaration")
    if bool(module.clocks) != bool(reset_bindings):
        raise SemanticError("clock and reset must be declared together")
    clock_specs = module.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in module.clocks
    )
    reset_specs = module.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in reset_bindings
    )
    if tuple(item.name for item in clock_specs) != module.clocks:
        raise SemanticError("physical clock declarations must match clock declarations")
    if tuple(item.name for item in reset_specs) != module.resets:
        raise SemanticError("physical reset declarations must match reset declarations")
    clocks_by_name = {item.name: item for item in clock_specs}
    resets_by_name = {item.name: item for item in reset_specs}

    clock_domains: tuple[ir_cdc.ClockDomain, ...]
    if not module.clocks and inherited_domain is not None:
        if isinstance(inherited_domain, ir_cdc.ClockDomain):
            inherited = inherited_domain
            clock, reset = inherited.clock, inherited.reset
            clock_domains = (inherited,)
        else:
            clock, reset = inherited_domain
            clock_domains = (ir_cdc.ClockDomain(clock, reset),)
    elif not module.clocks:
        clock_domains = ()
        clock = None
        reset = None
    elif len(module.clocks) == 1:
        if len(reset_bindings) != 1:
            raise SemanticError("a single-clock module requires exactly one reset")
        clock = module.clocks[0]
        reset, reset_domain = reset_bindings[0]
        if reset_domain is not None and reset_domain != clock:
            raise SemanticError(
                f"reset '{reset}' references unknown clock domain '{reset_domain}'"
            )
        clock_domains = (
            _clock_domain_from_source(
                clocks_by_name[clock],
                resets_by_name[reset],
                source_unit=effective_source_unit,
                source_digest=effective_source_digest,
            ),
        )
    else:
        clock = None
        reset = None
        by_clock: dict[str, str] = {}
        for reset_name, reset_domain in reset_bindings:
            if reset_domain is None:
                raise SemanticError(
                    f"reset '{reset_name}' requires an explicit clock domain"
                )
            if reset_domain not in module.clocks:
                raise SemanticError(
                    f"reset '{reset_name}' references unknown clock domain "
                    f"'{reset_domain}'"
                )
            if reset_domain in by_clock:
                raise SemanticError(
                    f"clock domain '{reset_domain}' has more than one reset"
                )
            by_clock[reset_domain] = reset_name
        missing_domains = set(module.clocks) - by_clock.keys()
        if missing_domains:
            missing_domain = sorted(missing_domains)[0]
            raise SemanticError(
                f"clock domain '{missing_domain}' has no reset"
            )
        clock_domains = tuple(
            _clock_domain_from_source(
                clocks_by_name[name],
                resets_by_name[by_clock[name]],
                source_unit=effective_source_unit,
                source_digest=effective_source_digest,
            )
            for name in module.clocks
        )

    _validate_async_reset_domain_scope(clock_domains)

    timing_names = {
        item
        for domain in clock_domains
        for item in (domain.clock, domain.reset)
    }
    if any(domain.clock == domain.reset for domain in clock_domains):
        raise SemanticError("clock and reset must have different names")

    symbols: dict[str, ir_module.Port] = {}
    ports: list[ir_module.Port] = []
    for declaration in module.ports:
        if declaration.name in symbols:
            raise SemanticError(f"duplicate port '{declaration.name}'")
        direction = (
            ir_module.PortDirection.INPUT
            if declaration.direction is ast.Direction.INPUT
            else ir_module.PortDirection.OUTPUT
        )
        port_syntax = declaration.type_name
        if isinstance(port_syntax, ast.InterfaceTypeName):
            protocol = {
                ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE,
                ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID,
                ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT,
                ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET,
                ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT,
            }[port_syntax.kind]
            payload_syntax = port_syntax.payload_type
            capacity = port_syntax.capacity
            virtual_channels = port_syntax.virtual_channels
        else:
            protocol = InterfaceProtocol.WIRE
            payload_syntax = port_syntax
            capacity = None
            virtual_channels = None
        port = ir_module.Port(
            direction=direction,
            name=declaration.name,
            type=type_resolver.resolve(payload_syntax),
            protocol=protocol,
            capacity=capacity,
            domain=(
                declaration.domain
                if declaration.domain is not None
                else clock
            ),
            virtual_channels=virtual_channels,
        )
        if (
            direction is ir_module.PortDirection.INPUT
            and not allow_external_enum_inputs
            and _contains_enum_type(port.type)
        ):
            raise SemanticError(
                f"top-level input '{port.name}' cannot expose enum type "
                f"{port.type}; use an internal child interface or an explicit "
                "future pack/unpack boundary"
            )
        if (
            direction is ir_module.PortDirection.INPUT
            and not allow_external_enum_inputs
            and _contains_tagged_union_type(port.type)
        ):
            raise SemanticError(
                f"top-level input '{port.name}' cannot expose tagged-union type "
                f"{port.type}; use an internal child interface"
            )
        if protocol is InterfaceProtocol.VC_CREDIT:
            if (
                virtual_channels is None
                or virtual_channels < 2
                or virtual_channels & (virtual_channels - 1)
            ):
                raise SemanticError(
                    f"vc_credit interface '{port.name}' virtual-channel count "
                    "must be a power of two and at least 2"
                )
            if capacity is None or capacity < 1:
                raise SemanticError(
                    f"vc_credit interface '{port.name}' requires at least one "
                    "credit per virtual channel"
                )
        if declaration.domain is not None and declaration.domain not in module.clocks:
            raise SemanticError(
                f"port '{declaration.name}' references unknown clock domain "
                f"'{declaration.domain}'"
            )
        if len(clock_domains) > 1 and port.domain is None:
            raise SemanticError(
                f"port '{declaration.name}' requires an explicit clock domain"
            )
        symbols[port.name] = port
        ports.append(port)

    source_scalar_ports = tuple(ports)

    external_model_function: ir_module.Function | None = None
    if module.external_model is not None:
        aggregate_port = next(
            (
                port
                for port in source_scalar_ports
                if isinstance(port.type, (StructType, TupleType, VecType))
            ),
            None,
        )
        if aggregate_port is not None:
            raise SemanticError(
                f"external module '{module.name}' port '{aggregate_port.name}' "
                "must be a scalar wire in this first slice",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        external_model_function = next(
            (item for item in functions if item.name == module.external_model), None
        )
        if external_model_function is None:
            if module.external_model in generic_functions:
                detail = "must be non-generic"
            else:
                detail = "does not name an existing function"
            raise SemanticError(
                f"external module '{module.name}' model '{module.external_model}' "
                f"{detail}",
                code="ZL-EXTERN-MODEL",
            )
        external_inputs = tuple(
            port for port in source_scalar_ports
            if port.direction is ir_module.PortDirection.INPUT
        )
        external_outputs = tuple(
            port for port in source_scalar_ports
            if port.direction is ir_module.PortDirection.OUTPUT
        )
        expected_parameters = tuple(
            (port.name, port.type) for port in external_inputs
        )
        actual_parameters = tuple(
            (parameter.name, parameter.type)
            for parameter in external_model_function.parameters
        )
        if actual_parameters != expected_parameters:
            raise SemanticError(
                f"external module '{module.name}' model parameters must exactly "
                f"match inputs {expected_parameters}, got {actual_parameters}",
                code="ZL-EXTERN-MODEL",
            )
        if external_model_function.return_type != external_outputs[0].type:
            raise SemanticError(
                f"external module '{module.name}' model returns "
                f"{external_model_function.return_type}, expected "
                f"{external_outputs[0].type}",
                code="ZL-EXTERN-MODEL",
            )

    csr_module_payload = (
        f"{module.source_identity or module.name}|{module.source_hash or ''}|"
        f"{tuple((p.name, p.kind, p.default) for p in module.parameters)}"
    )
    csr_module_identity = hashlib.sha256(
        csr_module_payload.encode()
    ).hexdigest()[:24]
    csr_blocks = _analyze_csr_blocks(
        module.csr_blocks,
        type_resolver,
        symbols,
        module_identity=csr_module_identity,
        clock_domain=clock,
        reset_domain=reset,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
    )

    connections: list[ir_module.Connection] = []
    hierarchical_connections: list[ir_module.HierarchicalConnection] = []
    request_response_connections: list[ir_module.RequestResponseConnection] = []
    aggregate_protocol_connections: list[ir_module.AggregateProtocolConnection] = []
    protocol_endpoints: list[ir_module.ProtocolEndpoint] = []
    connected_sources: set[str] = set()
    connected_destinations: set[str] = set()
    aggregate_interface_names = {
        declaration.name for declaration in module.aggregate_interfaces
    }
    for declaration in module.connections:
        if declaration.transform is not None:
            # The transform owns ready/valid data and control.  It is lowered
            # after locals/callables are available and must not also become an
            # ordinary pass-through connection.
            continue
        if "." in declaration.source or "." in declaration.destination:
            continue
        if (
            declaration.source in aggregate_interface_names
            or declaration.destination in aggregate_interface_names
        ):
            # Aggregate endpoints are specialized below, after their source
            # protocol declarations have been resolved.  Do not misclassify
            # an undotted aggregate pass-through as a scalar/rv port edge.
            continue
        source = symbols.get(declaration.source)
        destination = symbols.get(declaration.destination)
        if source is None or destination is None:
            missing_name = (
                declaration.source if source is None else declaration.destination
            )
            raise SemanticError(f"connection endpoint '{missing_name}' is not a port")
        if source.direction is not ir_module.PortDirection.INPUT:
            raise SemanticError(
                f"connection source '{source.name}' must be an input interface",
                code="ZL-PROTOCOL-OWNERSHIP",
            )
        if destination.direction is not ir_module.PortDirection.OUTPUT:
            raise SemanticError(
                f"connection destination '{destination.name}' must be an output interface",
                code="ZL-PROTOCOL-OWNERSHIP",
            )
        if source.type != destination.type:
            raise SemanticError(
                f"connection payload mismatch: {source.name} is {source.type}, "
                f"{destination.name} is {destination.type}",
                code="ZL-PROTOCOL-TYPE",
            )
        if source.protocol in {
            InterfaceProtocol.PACKET,
            InterfaceProtocol.VC_CREDIT,
        } or destination.protocol in {
            InterfaceProtocol.PACKET,
            InterfaceProtocol.VC_CREDIT,
        }:
            raise SemanticError(
                "packet and virtual-channel credit ports require their "
                "Milestone 15 explicit composition forms"
            )
        if source.name in connected_sources or destination.name in connected_destinations:
            raise SemanticError("an interface may participate in only one connection")
        adapter = (
            ConnectionAdapter(declaration.adapter.value)
            if declaration.adapter is not None
            else None
        )
        crossing = (
            ir_cdc.Crossing(
                ir_cdc.CrossingKind(declaration.crossing.kind.value),
                declaration.crossing.depth,
            )
            if declaration.crossing is not None
            else None
        )
        if crossing is not None and (
            declaration.buffer_depth or adapter is not None
        ):
            raise SemanticError(
                "a clock-domain crossing cannot also specify a buffer or adapter"
            )
        expected_adapter: ConnectionAdapter | None
        if source.protocol is destination.protocol:
            expected_adapter = None
        elif (
            source.protocol is InterfaceProtocol.READY_VALID
            and destination.protocol is InterfaceProtocol.CREDIT
        ):
            expected_adapter = ConnectionAdapter.READY_VALID_TO_CREDIT
        elif (
            source.protocol is InterfaceProtocol.CREDIT
            and destination.protocol is InterfaceProtocol.READY_VALID
        ):
            expected_adapter = ConnectionAdapter.CREDIT_TO_READY_VALID
        else:
            raise SemanticError(
                f"no explicit adapter exists from {source.protocol.value} to "
                f"{destination.protocol.value}"
            )
        if expected_adapter is None and adapter is not None:
            raise SemanticError("identical protocols must not specify an adapter")
        if expected_adapter is not None and adapter is not expected_adapter:
            raise SemanticError(
                f"connection from {source.protocol.value} to "
                f"{destination.protocol.value} requires adapter "
                f"{expected_adapter.value}"
            )
        if (
            adapter is ConnectionAdapter.CREDIT_TO_READY_VALID
            and declaration.buffer_depth == 0
        ):
            raise SemanticError("credit_to_rv requires an explicit buffer depth")
        if (
            source.protocol is InterfaceProtocol.CREDIT
            and destination.protocol is InterfaceProtocol.CREDIT
            and source.capacity != destination.capacity
        ):
            raise SemanticError(
                "identical credit connections require equal capacities"
            )
        if declaration.buffer_depth and expected_adapter is None and (
            source.protocol is not InterfaceProtocol.READY_VALID
        ):
            raise SemanticError(
                "explicit buffers are currently supported only for ready/valid "
                "connections and credit_to_rv adapters"
            )
        if (
            adapter is ConnectionAdapter.READY_VALID_TO_CREDIT
            and declaration.buffer_depth
        ):
            raise SemanticError(
                "rv_to_credit does not accept a buffer; add a separate buffered "
                "ready/valid connection"
            )
        if (
            adapter is ConnectionAdapter.CREDIT_TO_READY_VALID
            and source.capacity is not None
            and declaration.buffer_depth < source.capacity
        ):
            raise SemanticError(
                f"credit_to_rv buffer depth {declaration.buffer_depth} is smaller "
                f"than source capacity {source.capacity}"
            )
        if source.domain != destination.domain:
            if crossing is None:
                raise SemanticError(
                    f"implicit clock-domain crossing from '{source.name}' "
                    f"({source.domain}) to '{destination.name}' "
                    f"({destination.domain}) is not allowed",
                    code="ZL-DOMAIN-CROSSING",
                    fixes=("use an explicit crossing with compatible endpoints",),
                )
        elif crossing is not None:
            raise SemanticError(
                f"crossing '{crossing.kind.value}' requires different domains"
            )
        if crossing is not None:
            if crossing.kind in {
                ir_cdc.CrossingKind.SYNC_LEVEL,
                ir_cdc.CrossingKind.PULSE_TOGGLE,
            }:
                if (
                    source.protocol is not InterfaceProtocol.WIRE
                    or destination.protocol is not InterfaceProtocol.WIRE
                    or source.type != BitType()
                ):
                    raise SemanticError(
                        f"{crossing.kind.value} crossing requires bit wire endpoints"
                    )
                if crossing.depth is not None:
                    raise SemanticError(
                        f"{crossing.kind.value} crossing does not accept a depth"
                    )
            else:
                if (
                    source.protocol is not InterfaceProtocol.READY_VALID
                    or destination.protocol is not InterfaceProtocol.READY_VALID
                ):
                    raise SemanticError(
                        f"{crossing.kind.value} crossing requires ready/valid endpoints"
                    )
                if crossing.kind is ir_cdc.CrossingKind.HANDSHAKE:
                    if not isinstance(
                        source.type, (BitType, UIntType, SIntType, BitsType)
                    ):
                        raise SemanticError(
                            "handshake crossing currently requires a scalar payload"
                        )
                    if crossing.depth is not None:
                        raise SemanticError(
                            "handshake crossing does not accept a depth"
                        )
                else:
                    depth = crossing.depth
                    if (
                        depth is None
                        or depth < 4
                        or depth & (depth - 1)
                    ):
                        raise SemanticError(
                            "async_fifo depth must be a power of two and at least 4"
                        )
        connections.append(
            ir_module.Connection(
                source,
                destination,
                declaration.buffer_depth,
                adapter,
                crossing,
            )
        )
        connected_sources.add(source.name)
        connected_destinations.add(destination.name)

    arbiters: list[ir_arbitration.PacketArbiter] = []
    arbitrated_ports: set[str] = set()
    if len(module.arbiters) > 1:
        raise SemanticError("a module currently supports exactly one packet arbiter")
    for declaration in module.arbiters:
        if clock is None or reset is None:
            raise SemanticError("packet arbiters require one module clock and reset")
        source_ports: list[ir_module.Port] = []
        for source_name in declaration.sources:
            source = symbols.get(source_name)
            if source is None:
                raise SemanticError(f"arbiter source '{source_name}' is not a port")
            if source.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(f"arbiter source '{source_name}' must be an input")
            if source.protocol is not InterfaceProtocol.PACKET:
                raise SemanticError(
                    f"arbiter source '{source_name}' must use packet<T>"
                )
            if source.name in arbitrated_ports:
                raise SemanticError(
                    f"packet port '{source.name}' participates in more than one arbiter"
                )
            source_ports.append(source)
        if len({port.name for port in source_ports}) != len(source_ports):
            raise SemanticError("arbiter sources must be distinct")
        destination = symbols.get(declaration.destination)
        if destination is None:
            raise SemanticError(
                f"arbiter destination '{declaration.destination}' is not a port"
            )
        if destination.direction is not ir_module.PortDirection.OUTPUT:
            raise SemanticError(
                f"arbiter destination '{destination.name}' must be an output"
            )
        if destination.protocol is not InterfaceProtocol.PACKET:
            raise SemanticError(
                f"arbiter destination '{destination.name}' must use packet<T>"
            )
        if destination.name in arbitrated_ports:
            raise SemanticError(
                f"packet port '{destination.name}' participates in more than one arbiter"
            )
        for source in source_ports:
            if source.type != destination.type:
                raise SemanticError(
                    f"arbiter payload mismatch: {source.name} is {source.type}, "
                    f"{destination.name} is {destination.type}"
                )
            if source.domain != destination.domain:
                raise SemanticError("packet arbiter endpoints must share one clock domain")
        arbiter = ir_arbitration.PacketArbiter(
            tuple(source_ports),
            destination,
            ir_arbitration.ArbitrationPolicy(declaration.policy.value),
            ir_arbitration.GrantScope(declaration.grant_scope.value),
        )
        arbiters.append(arbiter)
        arbitrated_ports.update(port.name for port in source_ports)
        arbitrated_ports.add(destination.name)

    request_responses: list[ir_module.RequestResponseInterface] = []
    request_response_symbols: dict[str, ir_module.RequestResponseInterface] = {}
    for declaration in module.request_responses:
        if declaration.name in symbols or declaration.name in request_response_symbols:
            raise SemanticError(f"duplicate interface or port '{declaration.name}'")
        request_type = type_resolver.resolve(declaration.request_type)
        response_type = type_resolver.resolve(declaration.response_type)
        if declaration.max_outstanding <= 0:
            raise SemanticError(
                f"request/response interface '{declaration.name}' requires "
                "max_outstanding > 0"
            )
        ordering = RequestResponseOrdering(declaration.ordering.value)
        match_by = declaration.match_by
        id_type: HardwareType | None = None
        if ordering is RequestResponseOrdering.IN_ORDER:
            if match_by is not None:
                raise SemanticError(
                    f"in-order interface '{declaration.name}' must not use match_by"
                )
        else:
            if match_by is None:
                raise SemanticError(
                    f"out-of-order interface '{declaration.name}' requires match_by"
                )
            if not isinstance(request_type, StructType) or not isinstance(
                response_type, StructType
            ):
                raise SemanticError(
                    f"out-of-order interface '{declaration.name}' requires struct "
                    "request and response payloads"
                )
            request_id = request_type.field(match_by)
            response_id = response_type.field(match_by)
            if request_id is None or response_id is None:
                raise SemanticError(
                    f"match field '{match_by}' must exist in both request and "
                    "response payloads"
                )
            if request_id.type != response_id.type:
                raise SemanticError(
                    f"match field '{match_by}' has different request and response types"
                )
            if not isinstance(
                request_id.type, (BitType, UIntType, SIntType, BitsType)
            ):
                raise SemanticError(
                    f"match field '{match_by}' must be a scalar hardware type"
                )
            id_type = request_id.type
        interface = ir_module.RequestResponseInterface(
            declaration.name,
            request_type,
            response_type,
            declaration.max_outstanding,
            ordering,
            match_by,
            id_type,
        )
        request_responses.append(interface)
        request_response_symbols[interface.name] = interface

    # A sequential ready/valid module has the same typed semantics whether it
    # is selected as the top or elaborated as a child.  Backend capability is
    # validated after this context-independent semantic lowering; do not make
    # source legality depend on the private child-specialization entry point.
    if not clock_domains and any(
        port.protocol is InterfaceProtocol.CREDIT for port in ports
    ):
        raise SemanticError("credit interfaces require a module clock and reset")
    if not clock_domains and any(
        port.protocol is InterfaceProtocol.VC_CREDIT for port in ports
    ):
        raise SemanticError(
            "virtual-channel credit interfaces require a module clock and reset"
        )
    if any(port.protocol is InterfaceProtocol.VC_CREDIT for port in ports) and (
        clock is None or reset is None
    ):
        raise SemanticError(
            "virtual-channel credit interfaces require one module clock and reset"
        )
    if any(port.protocol is InterfaceProtocol.PACKET for port in ports) and not arbiters:
        raise SemanticError(
            "packet interfaces currently require an explicit packet arbiter"
        )
    if not clock_domains and request_responses:
        raise SemanticError(
            "request/response interfaces require a module clock and reset"
        )
    if not clock_domains and csr_blocks:
        raise SemanticError("CSR blocks require a module clock and reset")
    if not clock_domains and any(
        connection.buffer_depth or connection.adapter is not None
        for connection in connections
    ):
        raise SemanticError(
            "buffered and adapted connections require a module clock and reset"
        )
    for timing_name in timing_names:
        if timing_name in symbols or timing_name in request_response_symbols:
            raise SemanticError(
                f"clock/reset name '{timing_name}' conflicts with a port"
            )
    csr_names = {block.name for block in csr_blocks}
    if csr_names & (symbols.keys() | request_response_symbols.keys()):
        conflict = sorted(
            csr_names & (symbols.keys() | request_response_symbols.keys())
        )[0]
        raise SemanticError(f"CSR block name '{conflict}' conflicts with a port")

    resource_symbols: dict[str, _FifoSymbol | _MemorySymbol | _RomSymbol] = {}
    fifo_declarations: dict[str, ast.FifoDecl] = {}
    memory_declarations: dict[str, ast.MemoryDecl] = {}
    rom_declarations: dict[str, ast.RomDecl] = {}
    for declaration in module.fifos:
        if clock is None:
            raise SemanticError(
                f"FIFO '{declaration.name}' requires a clock and reset"
            )
        if declaration.name in (
            symbols.keys()
            | request_response_symbols.keys()
            | csr_names
            | resource_symbols.keys()
        ):
            raise SemanticError(
                f"duplicate storage, interface, CSR, or port name "
                f"'{declaration.name}'"
            )
        if declaration.name in {clock, reset}:
            raise SemanticError(
                f"FIFO name '{declaration.name}' conflicts with clock or reset"
            )
        depth = type_resolver._eval_storage_depth(
            declaration.depth, kind="FIFO", name=declaration.name
        )
        symbol = _FifoSymbol(
            declaration.name,
            type_resolver.resolve(declaration.element_type),
            depth,
        )
        resource_symbols[symbol.name] = symbol
        fifo_declarations[symbol.name] = declaration

    for declaration in module.memories:
        if clock is None:
            raise SemanticError(
                f"memory '{declaration.name}' requires a clock and reset"
            )
        if declaration.name in (
            symbols.keys()
            | request_response_symbols.keys()
            | csr_names
            | resource_symbols.keys()
        ):
            raise SemanticError(
                f"duplicate storage, interface, CSR, or port name "
                f"'{declaration.name}'"
            )
        if declaration.name in {clock, reset}:
            raise SemanticError(
                f"memory name '{declaration.name}' conflicts with clock or reset"
            )
        depth = type_resolver._eval_storage_depth(
            declaration.depth, kind="memory", name=declaration.name
        )
        if depth < 2 or depth & (depth - 1):
            raise SemanticError(
                f"memory '{declaration.name}' depth must be a power of two "
                "and at least 2"
            )
        if declaration.read_latency != 1:
            raise SemanticError(
                f"memory '{declaration.name}' currently requires read_latency 1"
            )
        element_type = type_resolver.resolve(declaration.element_type)
        if not ir_packing.is_bit_packable(element_type):
            raise SemanticError(
                f"memory '{declaration.name}' requires a recursively "
                "bit-packable non-enum element type"
            )
        symbol = _MemorySymbol(
            declaration.name,
            element_type,
            depth,
        )
        resource_symbols[symbol.name] = symbol
        memory_declarations[symbol.name] = declaration

    for declaration in module.roms:
        if clock is None:
            raise SemanticError(
                f"ROM '{declaration.name}' requires a clock and reset"
            )
        if declaration.name in (
            symbols.keys()
            | request_response_symbols.keys()
            | csr_names
            | resource_symbols.keys()
        ):
            raise SemanticError(
                f"duplicate storage, interface, CSR, or port name "
                f"'{declaration.name}'"
            )
        if declaration.name in {clock, reset}:
            raise SemanticError(
                f"ROM name '{declaration.name}' conflicts with clock or reset"
            )
        depth = type_resolver._eval_storage_depth(
            declaration.depth, kind="ROM", name=declaration.name
        )
        if declaration.read_latency != 1:
            raise SemanticError(
                f"ROM '{declaration.name}' requires exactly read_latency 1"
            )
        element_type = type_resolver.resolve(declaration.element_type)
        try:
            ir_packing.packed_width(element_type)
        except ir_packing.PackingError as error:
            raise SemanticError(
                f"ROM '{declaration.name}' element type {element_type} is not "
                f"recursively bit-packable and non-enum: {error}"
            ) from error
        symbol = _RomSymbol(declaration.name, element_type, depth)
        resource_symbols[symbol.name] = symbol
        rom_declarations[symbol.name] = declaration

    if resource_symbols and (
        csr_blocks
        or request_responses
        or connections
        or any(port.protocol is InterfaceProtocol.CREDIT for port in ports)
    ):
        raise SemanticError(
            "storage resources cannot yet be mixed with CSR, credit, "
            "request/response, or connection backends"
        )
    scheduled_memory_names = {
        action.resource
        for rule in module.rules
        for action in rule.actions
        if isinstance(action, ast.ResourceAction)
        and action.resource in memory_declarations
    }
    scheduled_masked_memory_names = {
        action.resource
        for rule in module.rules
        for action in rule.actions
        if isinstance(action, ast.ResourceAction)
        and action.resource in memory_declarations
        and action.operation == "write"
        and len(action.operands) == 3
    }
    if len(scheduled_memory_names) > 1:
        raise SemanticError("a module currently supports at most one rule-owned memory")
    if scheduled_memory_names and len(memory_declarations) != 1:
        raise SemanticError(
            "global and rule-owned memory resources cannot be mixed in one module"
        )
    if memory_declarations and not scheduled_memory_names and (
        module.registers or module.next_assignments or module.rules
    ):
        raise SemanticError(
            "memory resources cannot yet be mixed with user registers or rules"
        )

    inputs = {
        name: port
        for name, port in symbols.items()
        if port.direction is ir_module.PortDirection.INPUT
        and port.protocol is InterfaceProtocol.WIRE
    }
    outputs = {
        name: port
        for name, port in symbols.items()
        if port.direction is ir_module.PortDirection.OUTPUT
        and port.protocol is InterfaceProtocol.WIRE
    }
    protocol_interfaces = {
        name: port
        for name, port in symbols.items()
        if port.protocol is not InterfaceProtocol.WIRE
    }
    registers: list[ir_module.Register] = []
    register_symbols: dict[str, ir_module.Register] = {}
    for declaration in module.registers:
        if not clock_domains:
            raise SemanticError(
                f"register '{declaration.name}' requires a clock and reset"
            )
        if declaration.name in symbols or declaration.name in register_symbols:
            raise SemanticError(f"duplicate state or port name '{declaration.name}'")
        if declaration.name in timing_names:
            raise SemanticError(
                f"register name '{declaration.name}' conflicts with clock or reset"
            )
        register_domain = declaration.domain or clock
        if declaration.domain is not None and declaration.domain not in module.clocks:
            raise SemanticError(
                f"register '{declaration.name}' references unknown clock domain "
                f"'{declaration.domain}'"
            )
        if len(clock_domains) > 1 and register_domain is None:
            raise SemanticError(
                f"register '{declaration.name}' requires an explicit clock domain"
            )
        type_ = type_resolver.resolve(declaration.type_name)
        initial = _check_typed_boundary(
            declaration.initial, {}, type_, pure_context
        )
        initial_analysis = _expand_analysis_calls(
            initial, pure_context, purpose=f"register '{declaration.name}' initial value"
        )
        if initial.type != type_ or not _is_constant_expression(initial_analysis):
            raise SemanticError(
                f"initial value for register '{declaration.name}' must be a "
                f"constant of type {type_}"
            )
        register = ir_module.Register(
            declaration.name, type_, initial, register_domain
        )
        registers.append(register)
        register_symbols[register.name] = register

    module_context = _ExpressionContext(
        function_signatures,
        allow_delay=bool(clock_domains),
        generic_functions=generic_functions,
        compile_time_constants=pure_context.compile_time_constants,
        static_callables=pure_context.static_callables,
        operator_declarations=module.operators,
        struct_declarations=module.structs,
        generic_specializations=pure_context.generic_specializations,
        function_definitions=pure_context.function_definitions,
        callable_definitions=pure_context.callable_definitions,
        callable_use_counts=pure_context.callable_use_counts,
        specializations_in_progress=pure_context.specializations_in_progress,
        specialization_budget_costs=pure_context.specialization_budget_costs,
        compile_time_real_quantize_cache=(
            pure_context.compile_time_real_quantize_cache
        ),
        structs=struct_types,
        parameters=parameter_values,
        unresolved_parameters=unresolved_parameter_names,
        type_resolver=type_resolver,
        generic_dependency_identity=pure_context.generic_dependency_identity,
        compile_time_budget=pure_context.compile_time_budget,
        formal_config=pure_context.formal_config,
        formal_verifier=pure_context.formal_verifier,
        exploration_results=pure_context.exploration_results,
        candidate_site_owner=candidate_site_owner,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
        source_digests=source_digests,
        functional_binder_ordinals=pure_context.functional_binder_ordinals,
        next_functional_binder_ordinal=(
            pure_context.next_functional_binder_ordinal
        ),
    )
    value_symbols: dict[str, _ValueSymbol] = {
        **inputs,
        **protocol_interfaces,
        **request_response_symbols,
        **register_symbols,
        **resource_symbols,
    }

    # Aggregate protocol endpoints are expanded once, at semantic elaboration,
    # into the same leaf ports used by ordinary hierarchy.  The aggregate
    # identity is retained separately for manifests and diagnostics.
    aggregate_protocol_endpoints: list[ir_module.AggregateProtocolEndpoint] = []
    aggregate_paths: dict[str, str] = {}
    protocol_declarations = {item.name: item for item in module.protocols}
    for aggregate in module.aggregate_interfaces:
        declaration = protocol_declarations.get(aggregate.protocol)
        if declaration is None:
            raise SemanticError(f"unknown protocol '{aggregate.protocol}'")
        if aggregate.role not in declaration.roles:
            raise SemanticError(
                f"protocol '{aggregate.protocol}' has no role '{aggregate.role}'"
            )
        arguments: dict[str, int | str] = {
            parameter.name: parameter.default
            for parameter in declaration.parameters
            if parameter.default is not None
        }
        type_arguments: dict[str, HardwareType] = {}
        positional = 0
        for argument in aggregate.arguments:
            parameter = (
                next((item for item in declaration.parameters if item.name == argument.name), None)
                if argument.name is not None
                else (declaration.parameters[positional] if positional < len(declaration.parameters) else None)
            )
            if argument.name is None:
                positional += 1
            if parameter is None:
                raise SemanticError(f"invalid specialization argument on protocol '{aggregate.protocol}'")
            if parameter.kind == "type":
                value = argument.value
                syntax = (
                    value
                    if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName))
                    else ast.TypeName(str(value))
                )
                type_arguments[parameter.name] = type_resolver.resolve(syntax)
            else:
                value = argument.value
                if isinstance(value, str):
                    parent_parameter = next((item for item in module.parameters if item.name == value), None)
                    if parent_parameter is not None and parent_parameter.default is not None:
                        value = parent_parameter.default
                arguments[parameter.name] = value
        specialized_resolver = _TypeResolver(
            module.type_aliases,
            module.structs,
            module.enums,
            declaration.parameters,
            arguments,
            type_bindings=type_arguments,
            identity_namespace=(
                effective_source_unit or module.source_identity or module.name
            ),
            tagged_unions=module.tagged_unions,
        )
        members: list[ir_module.ProtocolMember] = []
        for channel in declaration.channels:
            if isinstance(channel.type_name, ast.InterfaceTypeName):
                protocol = {
                    ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID,
                    ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE,
                    ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT,
                    ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET,
                    ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT,
                }[channel.type_name.kind]
                payload = specialized_resolver.resolve(channel.type_name.payload_type)
            else:
                protocol = InterfaceProtocol.WIRE
                payload = specialized_resolver.resolve(channel.type_name)
            member_domain = channel.domain or aggregate.domain or clock
            member = ir_module.ProtocolMember(
                channel.name, protocol, payload, channel.source_role,
                channel.sink_role, member_domain,
            )
            members.append(member)
            synthetic = f"{aggregate.name}__{channel.name}"
            if synthetic in symbols:
                raise SemanticError(f"aggregate member '{aggregate.name}.{channel.name}' conflicts with a port")
            direction = (
                ir_module.PortDirection.OUTPUT
                if aggregate.role == channel.source_role
                else ir_module.PortDirection.INPUT
            )
            port = ir_module.Port(
                direction, synthetic, payload,
                protocol=protocol,
                domain=member_domain,
            )
            symbols[synthetic] = port
            ports.append(port)
            if protocol is not InterfaceProtocol.WIRE:
                protocol_endpoints.append(
                    ir_module.ProtocolEndpoint(
                        module.name, synthetic, direction, protocol, payload,
                        domain=member_domain,
                    )
                )
            aggregate_paths[f"{aggregate.name}.{channel.name}"] = synthetic
        specialization_identity = (
            f"{aggregate.protocol}<" + ",".join(
                f"{parameter.name}={type_arguments.get(parameter.name, arguments.get(parameter.name, parameter.default))}"
                for parameter in declaration.parameters
            ) + ">"
        )
        aggregate_protocol_endpoints.append(
            ir_module.AggregateProtocolEndpoint(
                aggregate.name, aggregate.protocol, aggregate.role,
                tuple(members), aggregate.domain or clock, specialization_identity,
            )
        )
    module_context.aggregate_paths.update(aggregate_paths)
    specialized_structs = tuple(
        member.payload_type
        for endpoint_ in aggregate_protocol_endpoints
        for member in endpoint_.members
        if isinstance(member.payload_type, StructType)
    )
    if specialized_structs:
        struct_types = tuple({item.name: item for item in (*struct_types, *specialized_structs)}.values())
        module_context.structs = struct_types
    value_symbols.update({
        synthetic: symbols[synthetic]
        for synthetic in aggregate_paths.values()
    })

    resource_controls: dict[str, dict[str, ast.Expression]] = {
        name: {} for name in resource_symbols
    }
    # Hierarchical endpoint elaboration is deferred until child IR exists.
    def concrete_hierarchical_endpoint(path: str) -> str:
        """Resolve one bounded instance-array selector to a physical owner.

        Bracketed source spelling is compile-time structure.  Resolve it once
        here so ProtocolEndpoint ownership, HierarchyIndex lookup, simulation,
        and both backends all consume the same exact physical instance name.
        """

        match = re.fullmatch(
            r"(?P<owner>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\[(?P<index>[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\])?"
            r"(?P<tail>(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
            path,
        )
        if match is None:
            raise SemanticError(f"invalid hierarchical protocol endpoint '{path}'")
        owner = match.group("owner")
        selector = match.group("index")
        tail = match.group("tail")
        length = module_context.instance_arrays.get(owner)
        if selector is None:
            if length is not None:
                raise SemanticError(
                    f"instance array '{owner}' requires a compile-time index "
                    "before selecting a protocol endpoint"
                )
            return path
        if length is None:
            raise SemanticError(
                f"hierarchical protocol instance '{owner}' is not an instance array"
            )
        syntax_index: int | ast.Expression = (
            int(selector)
            if selector.isdecimal()
            else ast.NameExpr(selector)
        )
        index = _resolve_instance_array_index(
            syntax_index, module_context, array=owner
        )
        if index < 0 or index >= length:
            raise SemanticError(
                f"instance array '{owner}' index {index} is out of range "
                f"0..{length - 1}"
            )
        return f"{owner}[{index}]{tail}"

    def endpoint(path: str, *, source: bool) -> ir_module.ProtocolEndpoint:
        path = concrete_hierarchical_endpoint(path)
        parts = path.split(".")
        if len(parts) == 1:
            port = symbols.get(parts[0])
            if port is None or port.protocol is InterfaceProtocol.WIRE:
                raise SemanticError(f"protocol endpoint '{path}' is not a protocol port")
            owner, name, direction, endpoint_type = module.name, port.name, port.direction, port.type
            capacity, domain = port.capacity, port.domain
        elif len(parts) == 2:
            inst = next((item for item in instances if item.name == parts[0]), None)
            child = child_irs.get(parts[0])
            if inst is None or child is None:
                raise SemanticError(f"unknown hierarchical protocol instance '{parts[0]}'")
            port = next((item for item in child.ports if item.name == parts[1]), None)
            if port is None:
                raise SemanticError(f"'{path}' is not a child protocol endpoint")
            owner, name, direction, endpoint_type = inst.name, port.name, port.direction, port.type
            capacity, domain = port.capacity, child.clock
        else:
            raise SemanticError(f"protocol endpoint path '{path}' is too deep")
        expected = (
            ir_module.PortDirection.INPUT if source and len(parts) == 1
            else ir_module.PortDirection.OUTPUT if source
            else ir_module.PortDirection.OUTPUT if len(parts) == 1
            else ir_module.PortDirection.INPUT
        )
        if direction is not expected:
            raise SemanticError(f"protocol endpoint '{path}' has wrong direction")
        result = ir_module.ProtocolEndpoint(owner, name, direction, port.protocol, endpoint_type, capacity, domain)
        protocol_endpoints.append(result)
        return result

    def request_response_endpoint(
        path: str, *, source: bool, channel: RequestResponseChannel
    ) -> ir_module.ProtocolEndpoint:
        parts = path.split(".")
        if len(parts) != 2:
            raise SemanticError(
                f"request/response hierarchy endpoint '{path}' must select "
                "an instance interface"
            )
        inst = next((item for item in instances if item.name == parts[0]), None)
        child = child_irs.get(parts[0])
        if inst is None or child is None:
            raise SemanticError(f"unknown hierarchical protocol instance '{parts[0]}'")
        interface = next(
            (item for item in child.request_responses if item.name == parts[1]),
            None,
        )
        if interface is None:
            raise SemanticError(f"'{path}' is not a child request/response endpoint")
        if interface.max_outstanding <= 0:
            raise SemanticError(
                f"hierarchical request/response endpoint '{path}' requires "
                "a positive max_outstanding"
            )
        if interface.ordering is not RequestResponseOrdering.IN_ORDER:
            raise SemanticError(
                f"hierarchical request/response endpoint '{path}' requires "
                "ordering in_order"
            )
        requester = interface.role is RequestResponseRole.REQUESTER
        is_output = requester if channel is RequestResponseChannel.REQUEST else not requester
        expected = (
            ir_module.PortDirection.OUTPUT if source else ir_module.PortDirection.INPUT
        )
        if is_output != (expected is ir_module.PortDirection.OUTPUT):
            raise SemanticError(
                f"request/response endpoint '{path}' has wrong requester/responder direction"
            )
        payload_type = (
            interface.request_type
            if channel is RequestResponseChannel.REQUEST
            else interface.response_type
        )
        result = ir_module.ProtocolEndpoint(
            inst.name,
            interface.name,
            expected,
            InterfaceProtocol.READY_VALID,
            payload_type,
            None,
            child.clock,
            channel,
        )
        protocol_endpoints.append(result)
        return result

    for declaration in ():
        if "." not in declaration.source and "." not in declaration.destination:
            continue
        source = endpoint(declaration.source, source=True)
        destination = endpoint(declaration.destination, source=False)
        if source.protocol is not destination.protocol:
            raise SemanticError("hierarchical protocol connections require identical protocols")
        if source.payload_type != destination.payload_type:
            raise SemanticError("hierarchical protocol payload types do not match")
        if source.domain != destination.domain:
            raise SemanticError("hierarchical protocol endpoints must share a clock domain")
        if declaration.adapter is not None:
            raise SemanticError("hierarchical protocol connections do not accept adapters")
        hierarchical_connections.append(ir_module.HierarchicalConnection(source, destination, declaration.buffer_depth))

    for assignment in module.assignments:
        parts = assignment.target.split(".")
        if parts[0] not in resource_symbols:
            continue
        if len(parts) != 2:
            raise SemanticError(
                f"storage control target '{assignment.target}' must select one field"
            )
        resource = resource_symbols[parts[0]]
        field = parts[1]
        if isinstance(resource, _FifoSymbol):
            try:
                signal = ir_storage.FifoSignal(field)
            except ValueError as error:
                raise SemanticError(
                    f"FIFO '{resource.name}' has no field '{field}'"
                ) from error
            writable = {
                ir_storage.FifoSignal.DATA,
                ir_storage.FifoSignal.PUSH,
                ir_storage.FifoSignal.POP,
            }
        elif isinstance(resource, _MemorySymbol):
            try:
                signal = ir_storage.MemorySignal(field)
            except ValueError as error:
                raise SemanticError(
                    f"memory '{resource.name}' has no field '{field}'"
                ) from error
            writable = {
                ir_storage.MemorySignal.READ_ADDRESS,
                ir_storage.MemorySignal.WRITE_ENABLE,
                ir_storage.MemorySignal.WRITE_ADDRESS,
                ir_storage.MemorySignal.WRITE_DATA,
                ir_storage.MemorySignal.WRITE_MASK,
            }
        else:
            assert isinstance(resource, _RomSymbol)
            try:
                signal = ir_storage.RomSignal(field)
            except ValueError as error:
                raise SemanticError(
                    f"ROM '{resource.name}' has no field '{field}'"
                ) from error
            writable = {ir_storage.RomSignal.READ_ADDRESS}
        if signal not in writable:
            raise SemanticError(
                f"storage field '{assignment.target}' is read-only"
            )
        if field in resource_controls[resource.name]:
            raise SemanticError(
                f"storage control '{assignment.target}' is assigned more than once"
            )
        resource_controls[resource.name][field] = assignment.expression

    fifos: list[ir_storage.Fifo] = []
    scheduled_fifo_names = {
        action.resource
        for rule in module.rules
        for action in rule.actions
        if isinstance(action, ast.ResourceAction)
        and action.resource in fifo_declarations
    }
    identity_payload = f"{module.name}|{tuple((p.name, p.kind, p.default) for p in module.parameters)}"
    transition_prefix = hashlib.sha256(identity_payload.encode()).hexdigest()[:16]
    for name, declaration in fifo_declarations.items():
        symbol = resource_symbols[name]
        assert isinstance(symbol, _FifoSymbol)
        controls = resource_controls[name]
        required = {"data", "push", "pop"}
        scheduled = name in scheduled_fifo_names
        if controls and scheduled:
            raise SemanticError(
                f"FIFO '{name}' cannot mix global controls with rule-local actions"
            )
        missing = required - controls.keys()
        if controls and missing:
            raise SemanticError(
                f"FIFO '{name}' has no '{sorted(missing)[0]}' control assignment"
            )
        if not controls and not scheduled:
            raise SemanticError(
                f"FIFO '{name}' requires global controls or rule-local push/pop actions"
            )
        data = (
            _check_typed_boundary(
                controls["data"], value_symbols, symbol.element_type, module_context
            )
            if controls else None
        )
        push = (
            _check_expression(controls["push"], value_symbols, BitType(), module_context)
            if controls else None
        )
        pop = (
            _check_expression(controls["pop"], value_symbols, BitType(), module_context)
            if controls else None
        )
        if data is not None and data.type != symbol.element_type:
            raise SemanticError(
                f"FIFO '{name}.data' has type {data.type}, expected "
                f"{symbol.element_type}"
            )
        if push is not None and (push.type != BitType() or pop is None or pop.type != BitType()):
            raise SemanticError(f"FIFO '{name}' push and pop controls must be bit")
        fifos.append(
            ir_storage.Fifo(
                name, symbol.element_type, symbol.depth, data, push, pop,
                SourceOrigin(
                    declaration.origin,
                    f"FIFO {name}",
                    effective_source_unit,
                    effective_source_digest,
                )
                if getattr(declaration, "origin", None) is not None else None,
            )
        )

    memories: list[ir_storage.Memory] = []
    for name, declaration in memory_declarations.items():
        symbol = resource_symbols[name]
        assert isinstance(symbol, _MemorySymbol)
        controls = resource_controls[name]
        required = {"read_address", "write_enable", "write_address", "write_data"}
        scheduled = name in scheduled_memory_names
        if controls and scheduled:
            raise SemanticError(
                f"memory '{name}' cannot mix global controls with rule-local actions"
            )
        missing = required - controls.keys()
        if controls and missing:
            raise SemanticError(
                f"memory '{name}' has no '{sorted(missing)[0]}' control assignment"
            )
        if not controls and not scheduled:
            raise SemanticError(
                f"memory '{name}' requires global controls or rule-local read/write actions"
            )
        address_type = UIntType(symbol.address_width)
        masked = "write_mask" in controls or name in scheduled_masked_memory_names
        if masked and symbol.element_type.width % 8:
            raise SemanticError(
                f"memory '{name}' byte write mask requires an element width "
                "divisible by 8"
            )
        write_mask_width = symbol.element_type.width // 8 if masked else None
        read_address = write_enable = write_address = write_data = write_mask = None
        if controls:
            read_address = _check_expression(
                controls["read_address"], value_symbols, address_type, module_context
            )
            write_enable = _check_expression(
                controls["write_enable"], value_symbols, BitType(), module_context
            )
            write_address = _check_expression(
                controls["write_address"], value_symbols, address_type, module_context
            )
            write_data = _check_typed_boundary(
                controls["write_data"], value_symbols, symbol.element_type,
                module_context,
            )
            if masked:
                assert write_mask_width is not None
                write_mask = _check_expression(
                    controls["write_mask"], value_symbols,
                    BitsType(write_mask_width), module_context,
                )
            expected_types = (
                ("read_address", read_address.type, address_type),
                ("write_enable", write_enable.type, BitType()),
                ("write_address", write_address.type, address_type),
                ("write_data", write_data.type, symbol.element_type),
                *(
                    (("write_mask", write_mask.type, BitsType(write_mask_width)),)
                    if write_mask is not None and write_mask_width is not None else ()
                ),
            )
            for field, actual, expected_type in expected_types:
                if actual != expected_type:
                    raise SemanticError(
                        f"memory '{name}.{field}' has type {actual}, expected "
                        f"{expected_type}"
                    )
        origin = (
            SourceOrigin(
                declaration.origin, f"memory {name}", effective_source_unit,
                effective_source_digest,
            )
            if declaration.origin is not None else None
        )
        memories.append(
            ir_storage.Memory(
                name,
                f"state:{transition_prefix}:memory:{name}",
                symbol.element_type,
                symbol.depth,
                declaration.read_latency,
                ir_storage.MemoryCollision(declaration.collision.value),
                read_address,
                write_enable,
                write_address,
                write_data,
                origin,
                write_mask_width=write_mask_width,
                write_mask=write_mask,
            )
        )

    roms: list[ir_storage.Rom] = []
    for name, declaration in rom_declarations.items():
        symbol = resource_symbols[name]
        assert isinstance(symbol, _RomSymbol)
        controls = resource_controls[name]
        missing = {"read_address"} - controls.keys()
        if missing:
            raise SemanticError(f"ROM '{name}' has no 'read_address' assignment")
        address_type = UIntType(symbol.address_width)
        read_address = _check_expression(
            controls["read_address"], value_symbols, address_type, module_context
        )
        if read_address.type != address_type:
            raise SemanticError(
                f"ROM '{name}.read_address' has type {read_address.type}, "
                f"expected exact {address_type}"
            )
        address_range = _static_value_range(read_address)
        if address_range is None or address_range.minimum < 0 or address_range.maximum >= symbol.depth:
            described = (
                "unknown"
                if address_range is None
                else f"{address_range.minimum}..{address_range.maximum}"
            )
            raise SemanticError(
                f"ROM '{name}.read_address' static range {described} is not "
                f"within 0..{symbol.depth - 1}"
            )

        initializer_type = VecType(symbol.depth, symbol.element_type)
        initializer = _check_expression(
            declaration.initializer, value_symbols, initializer_type, module_context
        )
        if initializer.type != initializer_type:
            raise SemanticError(
                f"ROM '{name}' initializer has type {initializer.type}, expected "
                f"exact {initializer_type}"
            )
        initializer_analysis = _expand_analysis_calls(
            initializer,
            module_context,
            purpose=f"ROM '{name}' initializer",
        )
        if not isinstance(
            initializer_analysis,
            (ir_expr.Generate, ir_expr.Map, ir_expr.FunctionalRegion),
        ):
            raise SemanticError(
                f"ROM '{name}' initializer must specialize to a concrete "
                f"{initializer_type} vector"
            )
        try:
            runtime_contents = constant_runtime_value(initializer_analysis)
        except ConstantExpressionError as error:
            raise SemanticError(
                f"ROM '{name}' initializer must contain compile-time constants "
                f"only: {error}"
            ) from error
        assert isinstance(runtime_contents, tuple)
        contents = (
            initializer_analysis.elements
            if isinstance(initializer_analysis, (ir_expr.Generate, ir_expr.Map))
            else tuple(
                _rom_constant_expression(
                    symbol.element_type,
                    word,
                    initializer_analysis.template.origin,
                )
                for word in runtime_contents
            )
        )
        canonical_contents = _canonical_rom_runtime_values(
            initializer_type, runtime_contents
        )
        content_hash = hashlib.sha256(
            repr((str(initializer_type), canonical_contents)).encode("utf-8")
        ).hexdigest()
        identity_payload = (
            effective_source_unit or module.source_identity or module.name,
            module.name,
            tuple(sorted(parameter_values.items())),
            tuple(sorted(
                (
                    name,
                    str(value.type),
                    hashlib.sha256(
                        repr((str(value.type), constant_runtime_value(value))).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                )
                for name, value in (
                    specialization_constant_bindings or {}
                ).items()
            )),
            tuple(sorted(
                (name, binding.canonical_identity)
                for name, binding in (
                    specialization_callable_bindings or {}
                ).items()
            )),
            name,
            str(symbol.element_type),
            symbol.depth,
        )
        semantic_id = "rom:" + hashlib.sha256(
            repr(identity_payload).encode("utf-8")
        ).hexdigest()[:24]
        origin = (
            SourceOrigin(
                declaration.origin,
                f"ROM {name}",
                effective_source_unit,
                effective_source_digest,
            )
            if declaration.origin is not None else None
        )
        roms.append(ir_storage.Rom(
            name=name,
            semantic_id=semantic_id,
            element_type=symbol.element_type,
            depth=symbol.depth,
            address_type=address_type,
            read_latency=declaration.read_latency,
            contents=contents,
            read_address=read_address,
            initialization_identity=expression_semantic_identity(initializer),
            dependency_identity=module_context.generic_dependency_identity,
            evaluator_schema=_COMPILE_TIME_EVALUATOR_SCHEMA,
            content_hash=content_hash,
            source_origin=origin,
        ))

    # Parent state/rule expressions are checked before physical child
    # elaboration below.  Publish the exact specialized scalar output
    # signatures first so those expressions can contain InstanceOutputRef
    # values without analyzing a child twice or guessing a backend name.
    known_modules = {item.name: item for item in module.submodules}
    known_modules[module.name] = module

    def inherit_named_declarations(
        local: tuple[object, ...], inherited: tuple[object, ...]
    ) -> tuple[object, ...]:
        names = {getattr(item, "name") for item in local}
        return tuple(
            (*local, *(item for item in inherited if getattr(item, "name") not in names))
        )

    def child_declaration_ast(module_name: str) -> ast.Module:
        child = known_modules[module_name]
        child = replace(
            child,
            type_aliases=inherit_named_declarations(
                child.type_aliases, module.type_aliases
            ),
            structs=inherit_named_declarations(child.structs, module.structs),
            enums=inherit_named_declarations(child.enums, module.enums),
            tagged_unions=inherit_named_declarations(
                child.tagged_unions, module.tagged_unions
            ),
            protocols=inherit_named_declarations(child.protocols, module.protocols),
            module_interfaces=inherit_named_declarations(
                child.module_interfaces, module.module_interfaces
            ),
            functions=inherit_named_declarations(child.functions, module.functions),
            operators=child.operators or module.operators,
            equivalences=module.equivalences,
            submodules=module.submodules,
        )
        if child.conforms_to is not None:
            declaration = next(
                (
                    item
                    for item in child.module_interfaces
                    if item.name == child.conforms_to.name
                ),
                None,
            )
            if declaration is not None:
                if child.external_model is not None and declaration.parameters:
                    raise SemanticError(
                        f"external module '{child.name}' requires a "
                        "non-parameterized named interface in this first slice",
                        code="ZL-EXTERN-UNSUPPORTED",
                    )
                child = _inherit_applied_interface_surface(child, declaration)
        return child

    # Compile-time aggregate instance arguments are resolved on demand before
    # child signatures are predeclared.  This preserves source-order
    # independence without turning the constant into a runtime child port.
    constant_local_cache: dict[str, ir_module.LocalValue] = {}
    constant_local_stack: set[str] = set()
    local_declarations = {
        item.target: item
        for item in module.assignments
        if "." not in item.target
        and item.target not in symbols
        and item.target not in resource_symbols
        and item.target not in request_response_symbols
    }

    def resolve_parent_constant(name: str) -> ir_expr.Expression:
        inherited = module_context.compile_time_constants.get(name)
        if inherited is not None:
            return inherited
        cached = constant_local_cache.get(name)
        if cached is not None:
            return cached.expression
        declaration = local_declarations.get(name)
        if declaration is None:
            raise SemanticError(
                f"compile-time constant argument '{name}' is not a module-local value"
            )
        if name in constant_local_stack:
            raise SemanticError(
                f"compile-time constant dependency cycle contains '{name}'"
            )
        constant_local_stack.add(name)
        try:
            dependencies: set[str] = set()

            def collect(value: object) -> None:
                if isinstance(value, ast.NameExpr):
                    dependencies.add(value.name)
                    return
                if isinstance(value, tuple):
                    for item in value:
                        collect(item)
                    return
                if is_dataclass(value) and not isinstance(value, type):
                    for item in fields(value):
                        if item.name != "origin":
                            collect(getattr(value, item.name))

            collect(declaration.expression)
            for dependency in sorted(dependencies):
                if dependency in local_declarations:
                    resolve_parent_constant(dependency)
            constant_symbols: dict[str, object] = {
                **value_symbols,
                **constant_local_cache,
            }
            expected_type = (
                type_resolver.resolve(declaration.type_name)
                if declaration.type_name is not None
                else None
            )
            value = (
                _check_typed_boundary(
                    declaration.expression,
                    constant_symbols,
                    expected_type,
                    module_context,
                )
                if expected_type is not None
                else _check_expression(
                    declaration.expression,
                    constant_symbols,
                    None,
                    module_context,
                )
            )
            _validate_tuple_destructure_assignment(declaration, value)
            value = _expand_analysis_calls(
                value,
                module_context,
                purpose=f"compile-time local '{name}'",
            )
            compile_time, value_range = _local_constant_and_range(
                value, module_context, name=name
            )
            if not compile_time:
                raise SemanticError(
                    f"compile-time constant argument '{name}' depends on runtime hardware"
                )
            try:
                constant_runtime_value(value)
            except ConstantExpressionError as error:
                raise SemanticError(
                    f"compile-time constant argument '{name}' is not fully "
                    f"constant: {error}"
                ) from error
            local = ir_module.LocalValue(
                name,
                value.type,
                value,
                True,
                value_range,
                expression_semantic_identity(value),
            )
            constant_local_cache[name] = local
            value_symbols[name] = local
            module_context.compile_time_constants[name] = value
            return value
        finally:
            constant_local_stack.discard(name)

    instance_specialization_cache: dict[
        int,
        tuple[
            dict[str, int | str],
            dict[str, HardwareType],
            dict[str, ir_expr.Expression],
            dict[str, _StaticCallableBinding],
            tuple[ast.ModuleParameter, ...],
            tuple[ir_module.Specialization, ...],
        ],
    ] = {}

    def resolve_instance_specialization(
        declaration: ast.InstanceDecl,
    ) -> tuple[
        dict[str, int | str],
        dict[str, HardwareType],
        dict[str, ir_expr.Expression],
        dict[str, _StaticCallableBinding],
        tuple[ast.ModuleParameter, ...],
        tuple[ir_module.Specialization, ...],
    ]:
        cached = instance_specialization_cache.get(id(declaration))
        if cached is not None:
            return cached
        if declaration.module not in known_modules:
            raise SemanticError(
                f"unknown instantiated module '{declaration.module}'"
            )
        child = child_declaration_ast(declaration.module)
        child_parameters = child.parameters
        parameter_names = {item.name for item in child_parameters}
        seen_arguments: set[str] = set()
        positional = 0
        resolved_arguments: dict[str, int | str] = {}
        resolved_type_arguments: dict[str, HardwareType] = {}
        deferred_arguments: list[ast.SpecializationArgument] = []
        for argument in declaration.arguments:
            key = argument.name
            if key is None:
                while (
                    positional < len(child_parameters)
                    and child_parameters[positional].name in seen_arguments
                ):
                    positional += 1
                if positional >= len(child_parameters):
                    raise SemanticError(
                        f"too many specialization arguments for '{declaration.module}'"
                    )
                key = child_parameters[positional].name
                positional += 1
            if key not in parameter_names:
                raise SemanticError(f"unknown specialization parameter '{key}'")
            if key in seen_arguments:
                raise SemanticError(
                    f"specialization parameter '{key}' is assigned more than once"
                )
            seen_arguments.add(key)
            parameter = next(
                item for item in child_parameters if item.name == key
            )
            value = argument.value
            if parameter.kind in {"constant", "callable"}:
                if argument.name is None:
                    raise SemanticError(
                        f"compile-time {parameter.kind} parameter '{key}' of "
                        f"'{declaration.module}' requires a named argument"
                    )
                deferred_arguments.append(argument)
                continue
            if parameter.kind == "type":
                if isinstance(value, int):
                    raise SemanticError(
                        f"type parameter '{key}' of '{declaration.module}' "
                        "requires a type argument"
                    )
                syntax = (
                    value
                    if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName))
                    else ast.TypeName(str(value))
                )
                try:
                    resolved_type = type_resolver.resolve(syntax)
                except SemanticError as error:
                    raise SemanticError(
                        f"cannot resolve type argument for parameter '{key}' of "
                        f"'{declaration.module}': {error}"
                    ) from error
                resolved_type_arguments[key] = resolved_type
                resolved_arguments[key] = str(resolved_type)
                continue

            if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
                raise SemanticError(
                    f"value parameter '{key}' of '{declaration.module}' cannot "
                    "receive a type argument; a compile-time integer is required"
                )
            if isinstance(value, int):
                resolved_value = value
            else:
                text = str(value)
                parent_parameter = next(
                    (
                        item
                        for item in module.parameters
                        if item.name == text and item.kind == "value"
                    ),
                    None,
                )
                if (
                    parent_parameter is not None
                    and isinstance(parent_parameter.default, int)
                ):
                    resolved_value = parent_parameter.default
                else:
                    try:
                        resolved_value = type_resolver._eval_constant_integer(
                            text,
                            description=f"value parameter '{key}'",
                            allow_zero=True,
                            allow_negative=True,
                        )
                    except SemanticError as value_error:
                        try:
                            type_resolver.resolve(ast.TypeName(text))
                        except SemanticError:
                            raise value_error
                        raise SemanticError(
                            f"value parameter '{key}' of '{declaration.module}' "
                            "cannot receive a type argument"
                        ) from value_error
            resolved_arguments[key] = resolved_value

        # Exact inference is intentionally bounded to a complete set of scalar
        # input bindings.  It never consults output destinations, protocol
        # connections, implicit conversions, or backend names.
        unresolved = {
            parameter.name
            for parameter in child_parameters
            if parameter.name not in resolved_arguments
        }
        if unresolved and declaration.array_length is None:
            scalar_inputs: list[tuple[str, ast.TypeSyntax]] = []
            for port in child.ports:
                if port.direction is not ast.Direction.INPUT:
                    continue
                port_type = port.type_name
                if isinstance(port_type, ast.InterfaceTypeName):
                    if port_type.kind is not ast.InterfaceKind.WIRE:
                        continue
                    payload_type = port_type.payload_type
                else:
                    payload_type = port_type
                for port_name in port.names or (port.name,):
                    scalar_inputs.append((port_name, payload_type))

            bindings: dict[str, ast.Expression] = {}
            duplicate_binding = False
            candidates = tuple(
                (*declaration.bindings, *(
                    ast.Assignment(
                        assignment.target.split(".", 1)[1],
                        assignment.expression,
                    )
                    for assignment in module.assignments
                    if assignment.target.startswith(declaration.name + ".")
                    and assignment.target.count(".") == 1
                ))
            )
            for binding in candidates:
                if binding.target in bindings:
                    duplicate_binding = True
                    break
                bindings[binding.target] = binding.expression

            if (
                scalar_inputs
                and not duplicate_binding
                and all(name in bindings for name, _ in scalar_inputs)
            ):
                parameter_map = {
                    item.name: item for item in child_parameters
                }
                value_bindings = {
                    name: value
                    for name, value in resolved_arguments.items()
                    if isinstance(value, int)
                }
                inference_resolver = _TypeResolver(
                    child.type_aliases,
                    child.structs,
                    child.enums,
                    child_parameters,
                    {
                        **{
                            item.name: item.default
                            for item in child_parameters
                            if item.default is not None
                        },
                        **value_bindings,
                    },
                    resolved_type_arguments,
                    enum_identity_namespace,
                    tagged_unions=child.tagged_unions,
                )
                for port_name, formal_type in scalar_inputs:
                    actual = _check_expression(
                        bindings[port_name], value_symbols, None, module_context
                    )
                    _bind_module_specialization_type(
                        formal_type,
                        actual.type,
                        parameter_map,
                        resolved_type_arguments,
                        value_bindings,
                        inference_resolver,
                    )
                for name, type_ in resolved_type_arguments.items():
                    resolved_arguments[name] = str(type_)
                for name, value in value_bindings.items():
                    resolved_arguments[name] = value
        for parameter in child_parameters:
            if parameter.name in resolved_arguments:
                continue
            if parameter.kind in {"constant", "callable"}:
                continue
            if parameter.kind == "type":
                raise SemanticError(
                    f"missing required type argument '{parameter.name}' for "
                    f"'{declaration.module}'; exact specialization inference "
                    "is ambiguous"
                )
            if parameter.default is None:
                raise SemanticError(
                    f"missing required value argument '{parameter.name}' for "
                    f"'{declaration.module}'"
                )
            default = parameter.default
            if isinstance(default, str):
                default = type_resolver._eval_constant_integer(
                    default,
                    description=f"value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                    local_values={
                        name: value
                        for name, value in resolved_arguments.items()
                        if isinstance(value, int)
                    },
                    allow_resolver_parameters=False,
                )
            assert isinstance(default, int)
            resolved_arguments[parameter.name] = default

        # Phase two binds immutable aggregate values and statically selected
        # callables after all shape/type parameters are concrete.
        for argument in deferred_arguments:
            assert argument.name is not None
            parameter = next(
                item for item in child_parameters if item.name == argument.name
            )
            if parameter.kind != "constant":
                continue
            constant_name = (
                argument.value.text
                if isinstance(argument.value, ast.TypeName)
                else argument.value
            )
            if isinstance(constant_name, str):
                resolve_parent_constant(constant_name)
        complete_arguments: list[ast.SpecializationArgument] = []
        deferred_names = {item.name for item in deferred_arguments}
        for parameter in child_parameters:
            if parameter.name in deferred_names:
                complete_arguments.append(
                    next(
                        item for item in deferred_arguments
                        if item.name == parameter.name
                    )
                )
            elif parameter.kind == "type":
                complete_arguments.append(ast.SpecializationArgument(
                    parameter.name,
                    ast.TypeName(str(resolved_type_arguments[parameter.name])),
                ))
            elif parameter.kind == "value":
                complete_arguments.append(ast.SpecializationArgument(
                    parameter.name,
                    int(resolved_arguments[parameter.name]),
                ))
        binding_declaration = ast.FunctionDecl(
            f"__module_specialization_{child.name}",
            (),
            ast.TypeName("bit"),
            ast.NumberExpr(0),
            child_parameters,
            source_identity=child.source_identity,
        )
        (
            _bound_types,
            _bound_values,
            constant_arguments,
            callable_arguments,
        ) = _specialization_bindings(
            binding_declaration,
            tuple(complete_arguments),
            (),
            module_context,
            value_symbols,
        )
        for name, expression in constant_arguments.items():
            binding = _constant_specialization_binding(
                name,
                expression,
                module_context.generic_dependency_identity,
            )
            resolved_arguments[name] = (
                f"constant:{expression.type}:{binding.content_hash}"
            )
        for name, binding in callable_arguments.items():
            metadata = _callable_specialization_binding(
                name,
                binding,
                module_context.generic_dependency_identity,
            )
            resolved_arguments[name] = f"callable:{metadata.content_hash}"
        specialized_parameters = tuple(
            replace(
                parameter,
                default=(
                    resolved_arguments.get(parameter.name, parameter.default)
                    if parameter.kind == "value"
                    else parameter.default
                ),
            )
            for parameter in child_parameters
        )
        specializations = tuple(
            ir_module.Specialization(
                parameter.name, resolved_arguments[parameter.name]
            )
            for parameter in child_parameters
        )
        result = (
            resolved_arguments,
            resolved_type_arguments,
            constant_arguments,
            callable_arguments,
            specialized_parameters,
            specializations,
        )
        instance_specialization_cache[id(declaration)] = result
        return result

    def specialized_child_ast(
        declaration: ast.InstanceDecl,
        specialized_parameters: tuple[ast.ModuleParameter, ...],
    ) -> ast.Module:
        child = child_declaration_ast(declaration.module)
        return replace(
            child,
            parameters=specialized_parameters,
        )

    predeclared_instance_names: set[str] = set()
    for declaration in module.instances:
        if declaration.name in predeclared_instance_names or declaration.name in symbols:
            raise SemanticError(f"duplicate instance '{declaration.name}'")
        predeclared_instance_names.add(declaration.name)
        (
            _resolved_arguments,
            resolved_type_arguments,
            _constant_arguments,
            _callable_arguments,
            specialized_parameters,
            _specializations,
        ) = resolve_instance_specialization(declaration)
        child_ast = specialized_child_ast(declaration, specialized_parameters)
        child_resolver = _TypeResolver(
            child_ast.type_aliases,
            child_ast.structs,
            child_ast.enums,
            child_ast.parameters,
            {
                parameter.name: parameter.default
                for parameter in child_ast.parameters
                if parameter.default is not None
            },
            resolved_type_arguments,
            enum_identity_namespace,
            tagged_unions=child_ast.tagged_unions,
        )
        child_ast = _normalize_concise_module_items(
            child_ast,
            child_resolver,
            (clock, reset)
            if len(clock_domains) == 1 and clock is not None and reset is not None
            else None,
        )
        array_length = (
            _resolve_range_bound(
                declaration.array_length, module_context, "instance array"
            )
            if declaration.array_length is not None
            else None
        )
        if array_length is not None:
            if array_length < 1:
                raise SemanticError("instance array length must be positive")
            module_context.instance_arrays[declaration.name] = array_length
        physical_names = (
            tuple(
                f"{declaration.name}[{index}]" for index in range(array_length)
            )
            if array_length is not None
            else (declaration.name,)
        )
        output_fields: list[StructField] = []
        for port in child_ast.ports:
            if port.direction is not ast.Direction.OUTPUT:
                continue
            syntax = port.type_name
            payload_syntax = (
                syntax.payload_type
                if isinstance(syntax, ast.InterfaceTypeName)
                else syntax
            )
            output_type = child_resolver.resolve(payload_syntax)
            output_fields.append(StructField(port.name, output_type))
            for physical_name in physical_names:
                module_context.instance_outputs[
                    (physical_name, port.name)
                ] = output_type
                module_context.instance_output_protocols[
                    (physical_name, port.name)
                ] = (
                    InterfaceProtocol.WIRE
                    if not isinstance(syntax, ast.InterfaceTypeName)
                    else {
                        ast.InterfaceKind.WIRE: InterfaceProtocol.WIRE,
                        ast.InterfaceKind.READY_VALID: InterfaceProtocol.READY_VALID,
                        ast.InterfaceKind.CREDIT: InterfaceProtocol.CREDIT,
                        ast.InterfaceKind.PACKET: InterfaceProtocol.PACKET,
                        ast.InterfaceKind.VC_CREDIT: InterfaceProtocol.VC_CREDIT,
                    }[syntax.kind]
                )
        if output_fields and array_length is None:
            aggregate_type = StructType(
                f"__instance_{declaration.name}", tuple(output_fields)
            )
            value_symbols[declaration.name] = ir_module.LocalValue(
                declaration.name,
                aggregate_type,
                ir_expr.InputRef(declaration.name, aggregate_type),
            )

    # Locals are pure bindings, but next-state expressions may use them.  Make
    # the bindings available before checking register transitions; the later
    # source-order pass skips names already elaborated here.
    locals_: list[ir_module.LocalValue] = [
        constant_local_cache[item.target]
        for item in module.assignments
        if item.target in constant_local_cache
    ]
    for declaration in module.assignments:
        if "." in declaration.target or declaration.target in symbols or declaration.target in resource_symbols or declaration.target in request_response_symbols:
            continue
        if declaration.target in {item.name for item in locals_}:
            continue
        expected = type_resolver.resolve(declaration.type_name) if declaration.type_name is not None else None
        value = (
            _check_typed_boundary(
                declaration.expression, value_symbols, expected, module_context
            )
            if expected is not None
            else _check_expression(
                declaration.expression, value_symbols, None, module_context
            )
        )
        _validate_tuple_destructure_assignment(declaration, value)
        if expected is not None and value.type != expected:
            raise SemanticError(f"local '{declaration.target}' has type {value.type}, expected {expected}")
        compile_time, value_range = _local_constant_and_range(
            value, module_context, name=declaration.target
        )
        local = ir_module.LocalValue(
            declaration.target,
            value.type,
            value,
            compile_time,
            value_range,
            expression_semantic_identity(value),
        )
        locals_.append(local)
        value_symbols[local.name] = local

    next_assignments: list[ir_module.NextAssignment] = []
    assigned_registers: set[str] = set()
    for assignment in module.next_assignments:
        target = register_symbols.get(assignment.target)
        if target is None:
            raise SemanticError(
                f"next-state target '{assignment.target}' is not a register"
            )
        if target.name in assigned_registers:
            raise SemanticError(
                f"register '{target.name}' has more than one next-state assignment"
            )
        expression = _check_typed_boundary(
            assignment.expression, value_symbols, target.type, module_context
        )
        if expression.type != target.type:
            raise SemanticError(
                f"next state for register '{target.name}' has type "
                f"{expression.type}, expected {target.type}"
            )
        next_assignments.append(ir_module.NextAssignment(target, expression))
        assigned_registers.add(target.name)

    rules: list[ir_module.Rule] = []
    rule_resource_actions: dict[str, list[tuple[ir_state.StateActionKind, str, tuple[ir_expr.Expression, ...], SourceOrigin | None]]] = {}
    rule_names: set[str] = set()
    for declaration in module.rules:
        if not clock_domains:
            raise SemanticError(f"rule '{declaration.name}' requires a clock and reset")
        if declaration.name in rule_names:
            raise SemanticError(f"duplicate rule '{declaration.name}'")
        rule_names.add(declaration.name)
        guard = _check_expression(
            declaration.guard, value_symbols, BitType(), module_context
        )
        if guard.type != BitType():
            raise SemanticError(f"guard for rule '{declaration.name}' must be bit")
        guard_analysis = _expand_analysis_calls(
            _expand_immutable_locals(guard, value_symbols),
            module_context,
            purpose=f"rule '{declaration.name}' guard refinement",
        )
        rule_context = replace(
            module_context,
            range_refinements=_guard_range_refinements(guard_analysis),
        )
        actions: list[ir_module.NextAssignment] = []
        resource_actions: list[tuple[ir_state.StateActionKind, str, tuple[ir_expr.Expression, ...], SourceOrigin | None]] = []
        action_targets: set[str] = set()
        for action in declaration.actions:
            if isinstance(action, ast.ResourceAction):
                resource = resource_symbols.get(action.resource)
                if resource is None:
                    raise SemanticError(
                        f"rule '{declaration.name}' references unknown state resource '{action.resource}'"
                    )
                resource_kind = "FIFO" if isinstance(resource, _FifoSymbol) else "memory"
                origin = (
                    SourceOrigin(
                        action.origin,
                        f"{resource_kind} {resource.name}.{action.operation}",
                        effective_source_unit,
                        effective_source_digest,
                    )
                    if action.origin is not None else None
                )
                if isinstance(resource, _FifoSymbol):
                    if resource.name not in scheduled_fifo_names:
                        raise SemanticError(
                            f"FIFO '{resource.name}' is not owned by scheduled rule actions"
                        )
                    if action.operation == "push":
                        if len(action.operands) != 1:
                            raise SemanticError("FIFO push requires exactly one payload")
                        operand = _check_typed_boundary(
                            action.operands[0], value_symbols, resource.element_type,
                            rule_context,
                        )
                        if operand.type != resource.element_type:
                            raise SemanticError(
                                f"FIFO '{resource.name}' push has type {operand.type}, expected {resource.element_type}"
                            )
                        kind = ir_state.StateActionKind.FIFO_PUSH
                        operands = (operand,)
                    elif action.operation == "pop":
                        if action.operands:
                            raise SemanticError("FIFO pop does not accept operands")
                        kind = ir_state.StateActionKind.FIFO_POP
                        operands = ()
                    else:
                        raise SemanticError(
                            f"FIFO '{resource.name}' has no rule action '{action.operation}'"
                        )
                elif isinstance(resource, _MemorySymbol):
                    if resource.name not in scheduled_memory_names:
                        raise SemanticError(
                            f"memory '{resource.name}' is not owned by scheduled rule actions"
                        )
                    address_type = UIntType(resource.address_width)
                    if action.operation == "read":
                        if len(action.operands) != 1:
                            raise SemanticError(
                                "memory read requires exactly one address operand"
                            )
                        address = _check_expression(
                            action.operands[0], value_symbols, address_type,
                            rule_context,
                        )
                        if address.type != address_type:
                            raise SemanticError(
                                f"memory '{resource.name}' read address has type {address.type}, expected {address_type}"
                            )
                        kind = ir_state.StateActionKind.MEMORY_READ_REQUEST
                        operands = (address,)
                    elif action.operation == "write":
                        if len(action.operands) not in {2, 3}:
                            raise SemanticError(
                                "memory write requires exactly address and data operands, "
                                "with an optional byte mask"
                            )
                        address = _check_expression(
                            action.operands[0], value_symbols, address_type,
                            rule_context,
                        )
                        data = _check_typed_boundary(
                            action.operands[1], value_symbols, resource.element_type,
                            rule_context,
                        )
                        if address.type != address_type:
                            raise SemanticError(
                                f"memory '{resource.name}' write address has type {address.type}, expected {address_type}"
                            )
                        if data.type != resource.element_type:
                            raise SemanticError(
                                f"memory '{resource.name}' write data has type {data.type}, expected {resource.element_type}"
                            )
                        memory_masked = resource.name in scheduled_masked_memory_names
                        if len(action.operands) == 3 and resource.element_type.width % 8:
                            raise SemanticError(
                                f"memory '{resource.name}' byte write mask requires an "
                                "element width divisible by 8"
                            )
                        if memory_masked:
                            mask_type = BitsType(resource.element_type.width // 8)
                            if len(action.operands) == 3:
                                mask = _check_expression(
                                    action.operands[2], value_symbols, mask_type,
                                    rule_context,
                                )
                                if mask.type != mask_type:
                                    raise SemanticError(
                                        f"memory '{resource.name}' write mask has type "
                                        f"{mask.type}, expected {mask_type}"
                                    )
                            else:
                                mask = ir_expr.Constant((1 << mask_type.width) - 1, mask_type)
                        kind = ir_state.StateActionKind.MEMORY_WRITE
                        operands = (
                            (address, data, mask) if memory_masked else (address, data)
                        )
                    else:
                        raise SemanticError(
                            f"memory '{resource.name}' has no rule action '{action.operation}'"
                        )
                else:
                    raise SemanticError(
                        f"state resource '{resource.name}' does not support rule actions"
                    )
                key = f"{resource.name}:{kind.value}"
                if key in action_targets:
                    raise SemanticError(
                        f"rule '{declaration.name}' performs state action '{resource.name}.{action.operation}' twice"
                    )
                action_targets.add(key)
                resource_actions.append((kind, resource.name, operands, origin))
                continue
            if isinstance(action.target, ast.IndexedAssignmentTarget):
                indexed = action.target
                target = register_symbols.get(indexed.register)
                if target is None:
                    raise SemanticError(
                        f"rule '{declaration.name}' indexed target "
                        f"'{indexed.register}' is not a register"
                    )
                if not isinstance(target.type, VecType):
                    raise SemanticError(
                        f"rule '{declaration.name}' indexed target "
                        f"'{indexed.register}' must be a one-dimensional vector "
                        f"register, got {target.type}"
                    )
                if target.name in action_targets:
                    raise SemanticError(
                        f"rule '{declaration.name}' writes register "
                        f"'{target.name}' twice"
                    )
                if target.name in assigned_registers:
                    raise SemanticError(
                        f"register '{target.name}' has both a rule action and "
                        "next-state assignment"
                    )
                index = _check_expression(
                    indexed.index, value_symbols, None, rule_context
                )
                index = _expand_immutable_locals(index, value_symbols)
                index = _expand_analysis_calls(
                    index, rule_context, purpose="vector-register update index"
                )
                if not isinstance(index.type, (UIntType, BitsType)):
                    raise SemanticError(
                        "vector-register update index must be an unsigned "
                        f"integral expression; got {index.type}"
                    )
                value_range = _static_value_range(
                    index, rule_context.range_refinements
                )
                if value_range is None:
                    raise SemanticError(
                        "vector-register update index has no statically provable "
                        f"unsigned range; required 0..{target.type.length - 1}"
                    )
                if (
                    value_range.minimum < 0
                    or value_range.maximum >= target.type.length
                ):
                    raise SemanticError(
                        f"vector-register update index range "
                        f"{value_range.minimum}..{value_range.maximum} is not "
                        f"provably within vector length {target.type.length} "
                        f"(required 0..{target.type.length - 1}, type {index.type})"
                    )
                value = _check_typed_boundary(
                    action.expression,
                    value_symbols,
                    target.type.element_type,
                    rule_context,
                )
                if value.type != target.type.element_type:
                    raise SemanticError(
                        f"rule '{declaration.name}' writes {value.type} to "
                        f"element type {target.type.element_type} of register "
                        f"'{target.name}'"
                    )
                update_origin = (
                    SourceOrigin(
                        indexed.origin,
                        f"vector update {target.name}",
                        effective_source_unit,
                        effective_source_digest,
                    )
                    if indexed.origin is not None
                    else action.expression.origin
                )
                vector = ir_expr.RegisterRef(
                    target.name, target.type, origin=update_origin
                )
                update = ir_expr.VectorUpdate(
                    vector,
                    index,
                    value,
                    target.type.length,
                    value_range,
                    target.type,
                    origin=update_origin,
                )
                actions.append(ir_module.NextAssignment(target, update))
                action_targets.add(target.name)
                continue
            target = register_symbols.get(action.target)
            if target is None:
                target = outputs.get(action.target)
            if target is None:
                raise SemanticError(
                    f"rule '{declaration.name}' target '{action.target}' is not a "
                    "register or output wire"
                )
            if isinstance(target, ir_module.Port) and not isinstance(
                target.type, (BitType, UIntType, SIntType, BitsType)
            ):
                raise SemanticError("rule output actions require a scalar wire")
            if target.name in action_targets:
                raise SemanticError(
                    f"rule '{declaration.name}' writes register '{target.name}' twice"
                )
            if isinstance(target, ir_module.Register) and target.name in assigned_registers:
                raise SemanticError(
                    f"register '{target.name}' has both a rule action and next-state assignment"
                )
            expression = _check_typed_boundary(
                action.expression, value_symbols, target.type, rule_context
            )
            if expression.type != target.type:
                raise SemanticError(
                    f"rule '{declaration.name}' writes {expression.type} to "
                    f"{target.type} target '{target.name}'"
                )
            actions.append(ir_module.NextAssignment(target, expression))
            action_targets.add(target.name)
        rules.append(ir_module.Rule(declaration.name, guard, tuple(actions)))
        rule_resource_actions[declaration.name] = resource_actions

    priorities: list[ir_module.RulePriority] = []
    priority_edges: set[tuple[str, str]] = set()
    for declaration in module.rule_priorities:
        edge = (declaration.higher, declaration.lower)
        if declaration.higher not in rule_names or declaration.lower not in rule_names:
            raise SemanticError("rule priority references an unknown rule")
        if declaration.higher == declaration.lower:
            raise SemanticError("a rule cannot have priority over itself")
        if edge in priority_edges:
            raise SemanticError("duplicate rule priority")
        priority_edges.add(edge)
        priorities.append(ir_module.RulePriority(*edge))
    if _has_priority_cycle(rule_names, priority_edges):
        raise SemanticError("rule priority graph contains a cycle")
    resources: list[ir_state.StateResource] = []
    resource_ids: dict[tuple[ir_state.StateResourceKind, str], str] = {}
    for register in registers:
        semantic_id = f"state:{transition_prefix}:register:{register.name}"
        resource_ids[(ir_state.StateResourceKind.REGISTER, register.name)] = semantic_id
        resources.append(ir_state.StateResource(
            semantic_id, register.name, ir_state.StateResourceKind.REGISTER,
            register.type, register.domain or clock, register.initial.origin,
        ))
    for fifo in fifos:
        semantic_id = f"state:{transition_prefix}:fifo:{fifo.name}"
        resource_ids[(ir_state.StateResourceKind.FIFO, fifo.name)] = semantic_id
        resources.append(ir_state.StateResource(
            semantic_id, fifo.name, ir_state.StateResourceKind.FIFO,
            fifo.element_type, clock, fifo.source_origin, fifo.depth,
        ))
    for memory in memories:
        if not memory.scheduled:
            continue
        semantic_id = memory.semantic_id
        resource_ids[(ir_state.StateResourceKind.MEMORY, memory.name)] = semantic_id
        resources.append(ir_state.StateResource(
            semantic_id, memory.name, ir_state.StateResourceKind.MEMORY,
            memory.element_type, clock, memory.source_origin, memory.depth,
        ))
    action_groups: list[ir_state.ActionGroup] = []
    for rule in rules:
        group_id = f"action-group:{transition_prefix}:{rule.name}"
        state_actions: list[ir_state.StateAction] = []
        for ordinal, action in enumerate(rule.actions):
            if not isinstance(action.target, ir_module.Register):
                continue
            resource_id = resource_ids[(ir_state.StateResourceKind.REGISTER, action.target.name)]
            state_actions.append(ir_state.StateAction(
                f"{group_id}:register_write:{action.target.name}:{ordinal}",
                resource_id, ir_state.StateActionKind.REGISTER_WRITE,
                (action.expression,), group_id, action.expression.origin,
            ))
        for ordinal, (kind, resource_name, operands, origin) in enumerate(rule_resource_actions[rule.name]):
            resource_kind = (
                ir_state.StateResourceKind.FIFO
                if kind in {
                    ir_state.StateActionKind.FIFO_PUSH,
                    ir_state.StateActionKind.FIFO_POP,
                }
                else ir_state.StateResourceKind.MEMORY
            )
            resource_id = resource_ids[(resource_kind, resource_name)]
            state_actions.append(ir_state.StateAction(
                f"{group_id}:{kind.value}:{resource_name}:{ordinal}", resource_id,
                kind, operands, group_id, origin,
            ))
        action_groups.append(ir_state.ActionGroup(
            group_id, rule.name, rule.guard, tuple(state_actions), rule.guard.origin,
        ))
    for index, first in enumerate(action_groups):
        for second in action_groups[index + 1:]:
            if (
                ir_state.groups_conflict(first, second)
                and not _priority_orders(
                    first.rule_name, second.rule_name, priority_edges
                )
                and not _guards_are_provably_disjoint(first.guard, second.guard)
            ):
                raise SemanticError(
                    f"rules '{first.rule_name}' and '{second.rule_name}' have conflicting state actions; add explicit priority"
                )
    writes: dict[str, list[ir_module.Rule]] = {}
    for rule in rules:
        for action in rule.actions:
            writes.setdefault(action.target.name, []).append(rule)
    for target_name, writers in writes.items():
        for index, first in enumerate(writers):
            for second in writers[index + 1:]:
                if (
                    not _priority_orders(first.name, second.name, priority_edges)
                    and not _guards_are_provably_disjoint(
                        first.guard, second.guard
                    )
                ):
                    raise SemanticError(
                        f"rules '{first.name}' and '{second.name}' both write "
                        f"target '{target_name}'; add explicit priority"
                    )
    transition_payload = (
        clock,
        reset,
        tuple(
            (item.semantic_id, item.kind.value, repr(item.type), item.domain, item.depth)
            for item in resources
        ),
        tuple(
            (
                group.semantic_id,
                expression_semantic_identity(group.guard),
                tuple(
                    (
                        action.semantic_id,
                        action.resource_id,
                        action.kind.value,
                        tuple(expression_semantic_identity(value) for value in action.operands),
                    )
                    for action in group.actions
                ),
            )
            for group in action_groups
        ),
        tuple(sorted(priority_edges)),
    )
    transition_identity = hashlib.sha256(repr(transition_payload).encode()).hexdigest()
    resolved_transition = ir_state.ResolvedTransition(
        f"transition:{transition_identity}", clock, reset, tuple(resources),
        tuple(action_groups), tuple(sorted(priority_edges)),
    )

    assignments: list[ir_module.Assignment] = []
    pipeline_explorations: list[ir_pipelines.PipelineExploration] = []
    elastic_pipeline_regions: list[ir_elastic.ElasticPipelineRegion] = []
    architecture_explorations: list[
        ir_architectures.ArchitectureExploration
    ] = []
    assigned_outputs: set[
        tuple[str, RequestResponseChannel | None, InterfaceSignal | None]
    ] = set()
    equivalences = _analyze_equivalences(module.equivalences)

    rule_output_targets: set[str] = set()
    for rule in rules:
        for action in rule.actions:
            if isinstance(action.target, ir_module.Port):
                rule_output_targets.add(action.target.name)
                assigned_outputs.add((action.target.name, None, None))

    for block in csr_blocks:
        for register in block.registers:
            for field in register.fields:
                if (
                    field.binding is not None
                    and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
                ):
                    if field.binding.signal in rule_output_targets:
                        raise SemanticError(
                            f"output '{field.binding.signal}' is driven by both "
                            "a CSR command binding and a rule action"
                        )
                    assigned_outputs.add((field.binding.signal, None, None))

    for arbiter in arbiters:
        for source in arbiter.sources:
            assigned_outputs.add((source.name, None, PacketSignal.READY))
        assigned_outputs.update(
            {
                (arbiter.destination.name, None, PacketSignal.PAYLOAD),
                (arbiter.destination.name, None, PacketSignal.VALID),
                (arbiter.destination.name, None, PacketSignal.LAST),
            }
        )

    transform_declarations = tuple(
        declaration
        for declaration in module.connections
        if declaration.transform is not None
    )
    if transform_declarations:
        if len(transform_declarations) != 1 or len(module.connections) != 1:
            raise SemanticError(
                "the bounded elastic pipeline slice requires exactly one "
                "ready/valid transform connection"
            )
        declaration = transform_declarations[0]
        transform = declaration.transform
        assert transform is not None
        if any((
            declaration.buffer_depth,
            declaration.request_buffer_depth,
            declaration.response_buffer_depth,
            declaration.adapter is not None,
            declaration.crossing is not None,
        )):
            raise SemanticError(
                "an elastic pipeline transform cannot also specify buffering, "
                "an adapter, or a crossing"
            )
        source = symbols.get(declaration.source)
        destination = symbols.get(declaration.destination)
        if source is None or destination is None:
            missing = declaration.source if source is None else declaration.destination
            raise SemanticError(f"elastic connection endpoint '{missing}' is not a port")
        if (
            source.direction is not ir_module.PortDirection.INPUT
            or destination.direction is not ir_module.PortDirection.OUTPUT
            or source.protocol is not InterfaceProtocol.READY_VALID
            or destination.protocol is not InterfaceProtocol.READY_VALID
        ):
            raise SemanticError(
                "elastic pipeline requires one ready/valid input source and "
                "one ready/valid output destination"
            )
        if len(ports) != 2 or any(
            port.protocol is not InterfaceProtocol.READY_VALID for port in ports
        ):
            raise SemanticError(
                "the first elastic pipeline slice supports exactly two "
                "ready/valid ports"
            )
        if clock is None or reset is None or len(clock_domains) != 1:
            raise SemanticError(
                "elastic pipeline requires exactly one synchronous clock/reset domain"
            )
        physical_domain = clock_domains[0]
        if not physical_domain.is_legacy_default:
            raise SemanticError(
                "elastic pipeline requires the common Clash/direct-SV clock/reset "
                "contract: rising-edge clock, synchronous active-high reset, "
                "and unspecified power-up"
            )
        if source.domain != destination.domain or source.domain != clock:
            raise SemanticError("elastic pipeline endpoints must share the module clock domain")
        if any((
            module.registers,
            module.next_assignments,
            module.rules,
            module.fifos,
            module.memories,
            module.roms,
            module.instances,
            module.request_responses,
            module.csr_blocks,
            module.arbiters,
            module.aggregate_interfaces,
            module.timing is not None,
        )):
            raise SemanticError(
                "elastic pipeline compiler-owned state cannot be mixed with "
                "user state, storage, hierarchy, CSR, arbitration, aggregate "
                "protocols, or a module timing contract"
            )
        operand = _check_expression(
            transform.expression,
            value_symbols,
            destination.type,
            module_context,
        )
        operand = _expand_exploration_calls(operand, functions, module_context)
        _validate_elastic_kernel_capture(operand, declaration.source)
        constraints = tuple(
            ir_pipelines.PipelineConstraint(
                ir_pipelines.PipelineMetric(
                    "throughput"
                    if constraint.metric is ast.PipelineMetric.INITIATION_INTERVAL
                    else constraint.metric.value
                ),
                ir_pipelines.PipelineRelation(constraint.relation.value),
                constraint.value,
            )
            for constraint in transform.constraints
        )
        try:
            exploration = explore_pipeline(
                f"{destination.name}.payload",
                operand,
                destination.type,
                constraints,
                module_context.allocate_delay,
            )
        except PipelineExplorationError as error:
            raise SemanticError(str(error)) from error
        # Elastic bookkeeping is real implementation state: one valid bit per
        # advance stage plus the bounded ready/advance control.  Preserve the
        # unchanged M31/M28 rank while publishing the complete estimate.  The
        # added control cost is common to every candidate, and valid FF cost is
        # a function of latency which is already an earlier deterministic
        # tie-break dimension.
        elastic_candidates = tuple(
            replace(
                candidate,
                estimate=replace(
                    candidate.estimate,
                    lut=candidate.estimate.lut + 2,
                    ff=candidate.estimate.ff + candidate.latency,
                ),
            )
            for candidate in exploration.candidates
        )
        selected = next(
            candidate
            for candidate in elastic_candidates
            if candidate.name == exploration.selected
        )
        _validate_elastic_kernel_capture(selected.expression, declaration.source)
        selected_timing = timing_info(selected.expression)
        if (
            selected.latency < 1
            or selected.initiation_interval != 1
            or selected_timing.latency != selected.latency
        ):
            raise SemanticError(
                "selected pipeline expression cannot be safely enable-gated: "
                "its exact typed stage latency does not match the selected plan"
            )
        staged = tuple(
            node
            for node in walk_expression(
                selected.expression,
                policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
            )
            if isinstance(node, ir_expr.Pipeline)
        )
        if not staged or any(
            isinstance(node, ir_expr.Delay)
            for node in walk_expression(
                selected.expression,
                policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
            )
        ):
            raise SemanticError(
                "elastic pipeline requires an M31 selected Pipeline-only plan"
            )
        stage_instances = tuple(sorted((node.instance, node.stages) for node in staged))
        if len(stage_instances) != len({instance for instance, _ in stage_instances}):
            raise SemanticError("elastic pipeline contains conflicting stage identities")
        origin = _semantic_origin(transform, module_context)
        semantic_id = "elastic:" + stable_digest({
            "source": source.name,
            "destination": destination.name,
            "kernel": expression_semantic_identity(operand),
            "constraints": tuple(item.render() for item in constraints),
            "selected": selected.name,
            "latency": selected.latency,
        })
        plan = ir_elastic.ElasticPipelinePlan(
            selected.name,
            selected.latency,
            stage_instances,
            selected.latency,
        )
        timing = ir_elastic.ElasticTimingContract(
            selected.latency,
            1,
            selected.latency,
        )
        elastic_pipeline_regions.append(ir_elastic.ElasticPipelineRegion(
            semantic_id,
            source.name,
            destination.name,
            source.type,
            destination.type,
            operand,
            exploration.constraints,
            elastic_candidates,
            exploration.selected,
            plan,
            timing,
            clock,
            reset,
            origin,
        ))
        assigned_outputs.update({
            (source.name, None, ReadyValidSignal.READY),
            (destination.name, None, ReadyValidSignal.PAYLOAD),
            (destination.name, None, ReadyValidSignal.VALID),
        })

    # Module-local value bindings are elaborated in source order.  They are
    # pure, single-assignment names and therefore do not become output ports.
    for declaration in module.assignments:
        if "." in declaration.target:
            continue
        if declaration.target in symbols or declaration.target in resource_symbols \
                or declaration.target in request_response_symbols:
            continue
        if declaration.target in {item.name for item in locals_}:
            continue
        expected = (
            type_resolver.resolve(declaration.type_name)
            if declaration.type_name is not None else None
        )
        value = (
            _check_typed_boundary(
                declaration.expression, value_symbols, expected, module_context
            )
            if expected is not None
            else _check_expression(
                declaration.expression, value_symbols, None, module_context
            )
        )
        _validate_tuple_destructure_assignment(declaration, value)
        if expected is not None and value.type != expected:
            raise SemanticError(
                f"local '{declaration.target}' has type {value.type}, expected {expected}"
            )
        compile_time, value_range = _local_constant_and_range(
            value, module_context, name=declaration.target
        )
        local = ir_module.LocalValue(
            declaration.target, value.type, value,
            compile_time,
            value_range,
            expression_semantic_identity(value),
        )
        locals_.append(local)
        value_symbols[local.name] = local

    # Validate hierarchy references and freeze specialization identity.  The
    # backend-neutral IR deliberately keeps child-module lowering separate.
    instances: list[ir_module.Instance] = []
    child_irs: dict[str, ir_module.Module] = {}
    instance_bindings: list[ir_module.InstancePortBinding] = []
    elaborated_instances: list[ir_module.ElaboratedInstance] = []
    instance_names: set[str] = set()
    for declaration in module.instances:
        if declaration.name in instance_names or declaration.name in symbols:
            raise SemanticError(f"duplicate instance '{declaration.name}'")
        if declaration.module not in known_modules:
            raise SemanticError(f"unknown instantiated module '{declaration.module}'")
        array_length = (
            _resolve_range_bound(declaration.array_length, module_context, "instance array")
            if declaration.array_length is not None
            else None
        )
        if array_length is not None and array_length < 1:
            raise SemanticError("instance array length must be positive")
        if array_length is not None and declaration.bindings:
            raise SemanticError(
                f"instance array '{declaration.name}' cannot use an inline binding "
                "block; bind each physical element with a compile-time indexed "
                "generate block"
            )
        if array_length is not None:
            module_context.instance_arrays[declaration.name] = array_length
        instance_names.add(declaration.name)
        (
            resolved_arguments,
            resolved_type_arguments,
            constant_arguments,
            callable_arguments,
            specialized_parameters,
            specialization_tuple,
        ) = resolve_instance_specialization(declaration)
        child_ast = specialized_child_ast(declaration, specialized_parameters)
        child_ir = analyze(
            child_ast,
            exploration_results=exploration_results,
            formal_config=formal_config,
            formal_verifier=formal_verifier,
            allow_sequential_protocol=True,
            specialization_type_bindings=resolved_type_arguments,
            specialization_constant_bindings=constant_arguments,
            specialization_callable_bindings=callable_arguments,
            compile_time_budget=selected_budget,
            _compile_time_real_quantize_cache=selected_real_quantize_cache,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
            allow_external_enum_inputs=True,
            enum_identity_namespace=enum_identity_namespace,
            resolution_context=resolution_context,
            root_module_identity=identity_for_source(child_ast.source_identity),
            dependency_closure=dependency_closure,
            _imports_premerged=True,
            inherited_domain=clock_domains[0]
            if len(clock_domains) == 1 and (
                child_ast.registers
                or child_ast.next_assignments
                or child_ast.rules
                or child_ast.fifos
                or child_ast.memories
                or child_ast.roms
                or child_ast.csr_blocks
                or any(
                    isinstance(port.type_name, ast.InterfaceTypeName)
                    for port in child_ast.ports
                )
                or child_ast.connections
                or child_ast.connection_chains
            )
            else None,
            _instance_stack=active_instance_stack,
            _hierarchy_cache=selected_hierarchy_cache,
        )
        if array_length is not None:
            # Aggregate wire values use the same typed hierarchical ABI as
            # scalar wires.  Their physical leaves are a backend-boundary
            # concern and must not make an otherwise static instance array a
            # distinct semantic feature.
            scalar_wire_profile = all(
                port.protocol is InterfaceProtocol.WIRE
                for port in child_ir.ports
            )
            primitive_ready_valid_profile = (
                any(
                    port.protocol is InterfaceProtocol.READY_VALID
                    for port in child_ir.ports
                )
                and all(
                    port.protocol in {
                        InterfaceProtocol.WIRE,
                        InterfaceProtocol.READY_VALID,
                    }
                    for port in child_ir.ports
                )
            )
            if child_ir.aggregate_protocol_endpoints:
                raise SemanticError(
                    f"instance array '{declaration.name}' does not support "
                    "aggregate protocol children"
                )
            if child_ir.request_responses:
                if len(child_ir.request_responses) != 1:
                    raise SemanticError(
                        f"instance array '{declaration.name}' currently supports "
                        "exactly one request/response interface per child"
                    )
                if not scalar_wire_profile:
                    raise SemanticError(
                        f"instance array '{declaration.name}' request/response "
                        "children may expose only scalar wire ports beside the "
                        "request/response interface"
                    )
                request_response = child_ir.request_responses[0]
                if (
                    request_response.max_outstanding <= 0
                    or request_response.ordering
                    is not RequestResponseOrdering.IN_ORDER
                ):
                    raise SemanticError(
                        f"instance array '{declaration.name}' supports only "
                        "positive max_outstanding with ordering in_order"
                    )
            if not scalar_wire_profile and not primitive_ready_valid_profile:
                raise SemanticError(
                    f"instance array '{declaration.name}' supports either "
                    "wire-only children or mixed scalar/primitive ready-valid "
                    "children; "
                    "mixed or other protocol ports are not supported"
                )
            if child_ir.csr_blocks:
                raise SemanticError(
                    f"instance array '{declaration.name}' currently does not support "
                    "CSR children"
                )
            storage_count = len(child_ir.fifos) + len(child_ir.memories) + len(child_ir.roms)
            scheduled_storage = any(fifo.scheduled for fifo in child_ir.fifos) or any(
                memory.scheduled for memory in child_ir.memories
            )
            has_user_state = bool(
                child_ir.registers
                or child_ir.next_assignments
                or child_ir.rules
            )
            if storage_count and has_user_state and not scheduled_storage:
                raise SemanticError(
                    f"instance array '{declaration.name}' does not support a "
                    "legacy globally controlled storage-owning child combined "
                    "with user registers, next-state assignments, or rules; "
                    "use scheduled storage through ResolvedTransition"
                )
            if primitive_ready_valid_profile and (child_ir.memories or child_ir.roms):
                raise SemanticError(
                    f"ready/valid instance array '{declaration.name}' currently "
                    "supports one FIFO but not synchronous memory or initialized "
                    "ROM children"
                )
            if primitive_ready_valid_profile and len(child_ir.fifos) > 1:
                raise SemanticError(
                    f"ready/valid instance array '{declaration.name}' currently "
                    "supports at most one FIFO resource"
                )
            if scalar_wire_profile and storage_count > 1:
                raise SemanticError(
                    f"instance array '{declaration.name}' currently supports exactly "
                    "one FIFO, synchronous memory, or initialized ROM resource"
                )
            if child_ir.is_multi_clock:
                raise SemanticError(
                    f"instance array '{declaration.name}' must share exactly one "
                    "synchronous clock/reset domain; CDC children are not supported"
                )
            if primitive_ready_valid_profile:
                if len(child_ir.clock_domains) != 1:
                    raise SemanticError(
                        f"ready/valid instance array '{declaration.name}' requires "
                        "exactly one synchronous clock/reset domain"
                    )
                if any(
                    connection.buffer_depth
                    or connection.adapter is not None
                    or connection.crossing is not None
                    for connection in child_ir.connections
                ):
                    raise SemanticError(
                        f"ready/valid instance array '{declaration.name}' does not "
                        "support child buffering, adapters, or crossings"
                    )
            if child_ir.instances or child_ir.elaborated_instances:
                _validate_nested_instance_array_child(
                    declaration.name,
                    child_ir,
                    hierarchy_cache=selected_hierarchy_cache,
                )
        # Array declarations are structurally unrolled into physical semantic
        # instances.  The specialization identity is shared, while each
        # physical path receives a distinct instance identity.  This keeps the
        # backend ABI deterministic and avoids reconstructing array indices
        # from generated RTL names.
        physical_names = (
            tuple(f"{declaration.name}[{index}]" for index in range(array_length))
            if array_length is not None
            else (declaration.name,)
        )
        child_irs[declaration.name] = child_ir
        for physical_name in physical_names:
            instance_names.add(physical_name)
            instance = ir_module.Instance(
                physical_name, declaration.module, specialization_tuple, None
            )
            instances.append(instance)
            child_irs[physical_name] = child_ir
        if child_ir.is_sequential:
            if not clock_domains or clock is None or reset is None:
                raise SemanticError(
                    f"sequential child '{declaration.name}' requires a parent clock/reset"
                )
            if child_ir.clock != clock or child_ir.reset != reset:
                raise SemanticError(
                    f"child '{declaration.name}' clock/reset must match parent "
                    f"('{clock}', '{reset}')"
                )
            if child_ir.clock_domains != clock_domains:
                raise SemanticError(
                    f"child '{declaration.name}' physical clock/reset contract "
                    "must exactly match its parent domain"
                )
        specialization_identity = hashlib.sha256(
            f"{declaration.module}|{tuple(sorted(resolved_arguments.items()))}".encode()
        ).hexdigest()[:24]
        for physical_name in physical_names:
            elaborated_instances.append(
                ir_module.ElaboratedInstance(
                    next(item for item in instances if item.name == physical_name),
                    child_ir.name,
                    clock if child_ir.is_sequential else None,
                    reset if child_ir.is_sequential else None,
                    instance_identity=hashlib.sha256(
                        f"{module.name}|{physical_name}|{declaration.module}|"
                        f"{tuple(sorted(resolved_arguments.items()))}".encode()
                    ).hexdigest()[:24],
                    semantic_path=(module.name, physical_name),
                    specialization_identity=specialization_identity,
                )
            )
            for port in child_ir.outputs:
                predeclared_type = module_context.instance_outputs.get(
                    (physical_name, port.name)
                )
                if predeclared_type is not None and predeclared_type != port.type:
                    raise SemanticError(
                        f"specialized child output signature changed during "
                        f"elaboration for '{physical_name}.{port.name}': "
                        f"predeclared {predeclared_type}, analyzed {port.type}"
                    )
                module_context.instance_outputs[(physical_name, port.name)] = port.type
        # Child port types are resolved through the child declaration's public
        # syntax when available; a synthetic aggregate exposes c.y to the
        # parent type checker without guessing generated RTL names.
        child_fields = tuple(StructField(port.name, port.type) for port in child_ir.outputs)
        if child_fields and array_length is None:
            value_symbols[declaration.name] = ir_module.LocalValue(
                declaration.name,
                StructType(f"__instance_{declaration.name}", child_fields),
                ir_expr.InputRef(declaration.name, StructType(f"__instance_{declaration.name}", child_fields)),
            )

    hierarchical_connection_declarations = list(module.connections)
    instance_declarations = {item.name: item for item in module.instances}
    for chain in module.connection_chains:
        if len(chain.endpoints) < 3:
            raise SemanticError("connection chain requires at least one intermediate instance")
        if any("[" in item for item in chain.endpoints):
            raise SemanticError(
                "instance arrays require explicit indexed connections; "
                "connection-chain array endpoints are not supported"
            )
        current_source = chain.endpoints[0]
        for instance_name in chain.endpoints[1:-1]:
            if "." in instance_name or "[" in instance_name:
                raise SemanticError(
                    "connection-chain intermediates must be bare physical instance names"
                )
            declaration = instance_declarations.get(instance_name)
            child = child_irs.get(instance_name)
            if declaration is None or child is None:
                raise SemanticError(
                    f"connection-chain intermediate '{instance_name}' is not an instance"
                )
            if declaration.array_length is not None:
                raise SemanticError(
                    f"connection-chain intermediate '{instance_name}' cannot be an instance array"
                )
            if child.module_signature is None:
                raise SemanticError(
                    f"connection-chain instance '{instance_name}' must conform to one named module interface"
                )
            protocol_inputs = tuple(
                port for port in child.ports
                if port.direction is ir_module.PortDirection.INPUT
                and port.protocol is not InterfaceProtocol.WIRE
            )
            protocol_outputs = tuple(
                port for port in child.ports
                if port.direction is ir_module.PortDirection.OUTPUT
                and port.protocol is not InterfaceProtocol.WIRE
            )
            if len(protocol_inputs) != 1 or len(protocol_outputs) != 1:
                raise SemanticError(
                    f"connection-chain instance '{instance_name}' requires exactly "
                    "one protocol input and one protocol output in its named interface"
                )
            hierarchical_connection_declarations.append(ast.ConnectionDecl(
                current_source,
                f"{instance_name}.{protocol_inputs[0].name}",
            ))
            current_source = f"{instance_name}.{protocol_outputs[0].name}"
        hierarchical_connection_declarations.append(ast.ConnectionDecl(
            current_source, chain.endpoints[-1]
        ))

    hierarchical_connection_declarations = [
        replace(
            declaration,
            source=concrete_hierarchical_endpoint(declaration.source),
            destination=concrete_hierarchical_endpoint(declaration.destination),
        )
        for declaration in hierarchical_connection_declarations
    ]

    array_rv_sources: set[tuple[str, str]] = set()
    array_rv_destinations: set[tuple[str, str]] = set()

    def array_physical_owner(owner: str) -> bool:
        match = re.fullmatch(
            r"(?P<array>[A-Za-z_][A-Za-z0-9_]*)\[[0-9]+\]", owner
        )
        return bool(
            match is not None
            and match.group("array") in module_context.instance_arrays
        )

    for declaration in hierarchical_connection_declarations:
        if "." not in declaration.source and "." not in declaration.destination:
            source_top = next(
                (
                    item for item in aggregate_protocol_endpoints
                    if item.name == declaration.source
                ),
                None,
            )
            destination_top = next(
                (
                    item for item in aggregate_protocol_endpoints
                    if item.name == declaration.destination
                ),
                None,
            )
            if source_top is None and destination_top is None:
                continue
            if source_top is None or destination_top is None:
                raise SemanticError(
                    "aggregate protocol pass-through requires two aggregate endpoints"
                )
            if source_top.protocol != destination_top.protocol:
                raise SemanticError(
                    "aggregate protocol pass-through requires the same protocol"
                )
            if (
                source_top.specialization_identity
                != destination_top.specialization_identity
            ):
                raise SemanticError(
                    "aggregate protocol pass-through specialization arguments "
                    "do not match"
                )
            if source_top.role == destination_top.role:
                raise SemanticError(
                    "aggregate protocol pass-through requires complementary roles"
                )
            if declaration.buffer_depth or declaration.request_buffer_depth \
                    or declaration.response_buffer_depth:
                raise SemanticError(
                    "aggregate protocol pass-through does not accept buffering"
                )
            if declaration.adapter is not None:
                raise SemanticError(
                    "aggregate protocol pass-through does not accept adapters"
                )
            source_members = {item.name: item for item in source_top.members}
            destination_members = {
                item.name: item for item in destination_top.members
            }
            if set(source_members) != set(destination_members):
                raise SemanticError(
                    "aggregate protocol pass-through member sets do not match"
                )
            aggregate_crossing = (
                ir_cdc.Crossing(
                    ir_cdc.CrossingKind(declaration.crossing.kind.value),
                    declaration.crossing.depth,
                )
                if declaration.crossing is not None
                else None
            )
            if aggregate_crossing is not None:
                if aggregate_crossing.kind is not ir_cdc.CrossingKind.ASYNC_FIFO:
                    raise SemanticError(
                        "aggregate protocol crossings currently support only async_fifo"
                    )
                depth = aggregate_crossing.depth
                if depth is None or depth < 4 or depth & (depth - 1):
                    raise SemanticError(
                        "aggregate async_fifo depth must be a power of two and at least 4"
                    )
                if len(source_members) != 1:
                    raise SemanticError(
                        "aggregate async_fifo crossing requires exactly one "
                        "ready/valid member"
                    )
            for member_name, member in source_members.items():
                other = destination_members[member_name]
                if (
                    member.protocol is not other.protocol
                    or member.payload_type != other.payload_type
                    or member.source_role != other.source_role
                    or member.sink_role != other.sink_role
                ):
                    raise SemanticError(
                        f"aggregate protocol member '{member_name}' does not match"
                    )
                if member.protocol not in {
                    InterfaceProtocol.WIRE,
                    InterfaceProtocol.READY_VALID,
                    InterfaceProtocol.CREDIT,
                }:
                    raise SemanticError(
                        f"aggregate protocol member '{member_name}' cannot yet "
                        "use top-level pass-through"
                    )
                if (
                    aggregate_crossing is not None
                    and member.protocol is not InterfaceProtocol.READY_VALID
                ):
                    raise SemanticError(
                        "aggregate async_fifo crossing requires exactly one "
                        "ready/valid member"
                    )
                source_port = symbols[f"{source_top.name}__{member_name}"]
                destination_port = symbols[
                    f"{destination_top.name}__{member_name}"
                ]
                if source_port.direction is not ir_module.PortDirection.INPUT:
                    raise SemanticError(
                        f"aggregate pass-through source '{source_top.name}' has "
                        f"the wrong role for member '{member_name}'"
                    )
                if destination_port.direction is not ir_module.PortDirection.OUTPUT:
                    raise SemanticError(
                        f"aggregate pass-through destination "
                        f"'{destination_top.name}' has the wrong role for member "
                        f"'{member_name}'"
                    )
                if (
                    aggregate_crossing is None
                    and source_port.domain != destination_port.domain
                ):
                    raise SemanticError(
                        "aggregate protocol pass-through endpoints must share "
                        "a clock domain"
                    )
                if (
                    aggregate_crossing is not None
                    and source_port.domain == destination_port.domain
                ):
                    raise SemanticError(
                        "aggregate async_fifo crossing requires different domains"
                    )
                connections.append(
                    ir_module.Connection(
                        source_port,
                        destination_port,
                        crossing=aggregate_crossing,
                    )
                )
            aggregate_protocol_connections.append(
                ir_module.AggregateProtocolConnection(
                    declaration.source,
                    declaration.destination,
                    source_top.protocol,
                    source_top.specialization_identity,
                    crossing=aggregate_crossing,
                )
            )
            continue
        source_parts = declaration.source.split(".")
        destination_parts = declaration.destination.split(".")
        source_child = child_irs.get(source_parts[0]) if len(source_parts) == 2 else None
        destination_child = child_irs.get(destination_parts[0]) if len(destination_parts) == 2 else None
        source_aggregate = (
            next((item for item in source_child.aggregate_protocol_endpoints if item.name == source_parts[1]), None)
            if source_child is not None and len(source_parts) == 2 else None
        )
        destination_aggregate = (
            next((item for item in destination_child.aggregate_protocol_endpoints if item.name == destination_parts[1]), None)
            if destination_child is not None and len(destination_parts) == 2 else None
        )
        top_aggregate = next(
            (item for item in aggregate_protocol_endpoints if item.name == source_parts[0]),
            None,
        ) if len(source_parts) == 1 else None
        if top_aggregate is not None and destination_aggregate is not None:
            if top_aggregate.protocol != destination_aggregate.protocol:
                raise SemanticError("top aggregate delegation requires the same protocol")
            if top_aggregate.specialization_identity != destination_aggregate.specialization_identity:
                raise SemanticError("top aggregate delegation specialization arguments do not match")
            if top_aggregate.role != destination_aggregate.role:
                raise SemanticError("top aggregate delegation requires the same role")
            if declaration.buffer_depth or declaration.adapter is not None or declaration.crossing is not None:
                raise SemanticError("top aggregate delegation does not accept buffering, adapters, or crossings")
            source_members = {member.name: member for member in top_aggregate.members}
            destination_members = {member.name: member for member in destination_aggregate.members}
            if set(source_members) != set(destination_members):
                raise SemanticError("top aggregate delegation member sets do not match")
            for name, member in source_members.items():
                other = destination_members[name]
                if (member.protocol, member.payload_type, member.source_role, member.sink_role) != (
                    other.protocol, other.payload_type, other.source_role, other.sink_role
                ):
                    raise SemanticError(f"top aggregate delegation member '{name}' does not match")
            aggregate_protocol_connections.append(
                ir_module.AggregateProtocolConnection(
                    declaration.source, declaration.destination,
                    top_aggregate.protocol, top_aggregate.specialization_identity,
                    delegation=True,
                )
            )
            continue
        if source_aggregate is not None or destination_aggregate is not None:
            if source_aggregate is None or destination_aggregate is None:
                raise SemanticError("aggregate protocol connections require two aggregate endpoints")
            if source_aggregate.protocol != destination_aggregate.protocol:
                raise SemanticError("aggregate protocol connections require the same protocol")
            if source_aggregate.specialization_identity != destination_aggregate.specialization_identity:
                raise SemanticError("aggregate protocol specialization arguments do not match")
            if declaration.buffer_depth or declaration.adapter is not None or declaration.crossing is not None:
                raise SemanticError("aggregate protocol connections do not accept buffering, adapters, or crossings")
            source_members = {member.name: member for member in source_aggregate.members}
            destination_members = {member.name: member for member in destination_aggregate.members}
            if set(source_members) != set(destination_members):
                raise SemanticError("aggregate protocol member sets do not match")
            for member_name, member in source_members.items():
                other = destination_members[member_name]
                if member.protocol is not other.protocol or member.payload_type != other.payload_type:
                    raise SemanticError(f"aggregate protocol member '{member_name}' does not match")
                if member.source_role == source_aggregate.role and member.sink_role == destination_aggregate.role:
                    source_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
                    destination_path = f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                elif member.source_role == destination_aggregate.role and member.sink_role == source_aggregate.role:
                    source_path = f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                    destination_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
                else:
                    raise SemanticError(f"aggregate protocol member '{member_name}' has incompatible roles")
                source_endpoint = endpoint(source_path, source=True)
                destination_endpoint = endpoint(destination_path, source=False)
                if source_endpoint.protocol is not destination_endpoint.protocol or source_endpoint.payload_type != destination_endpoint.payload_type:
                    raise SemanticError(f"aggregate protocol member '{member_name}' endpoint mismatch")
                if source_endpoint.domain != destination_endpoint.domain:
                    raise SemanticError("aggregate protocol endpoints must share a clock domain")
                hierarchical_connections.append(ir_module.HierarchicalConnection(source_endpoint, destination_endpoint))
            aggregate_protocol_connections.append(
                ir_module.AggregateProtocolConnection(
                    declaration.source, declaration.destination,
                    source_aggregate.protocol, source_aggregate.specialization_identity,
                )
            )
            continue
        source_rr = (
            next((item for item in source_child.request_responses if item.name == source_parts[1]), None)
            if source_child is not None else None
        )
        destination_rr = (
            next((item for item in destination_child.request_responses if item.name == destination_parts[1]), None)
            if destination_child is not None else None
        )
        if source_rr is not None or destination_rr is not None:
            if source_rr is None or destination_rr is None:
                raise SemanticError(
                    "request/response hierarchy connections require two "
                    "request/response endpoints"
                )
            if source_rr.request_type != destination_rr.request_type:
                raise SemanticError("request/response request payload types do not match")
            if source_rr.response_type != destination_rr.response_type:
                raise SemanticError("request/response response payload types do not match")
            if source_rr.max_outstanding != destination_rr.max_outstanding:
                raise SemanticError("request/response max_outstanding values do not match")
            if source_rr.ordering is not destination_rr.ordering:
                raise SemanticError("request/response ordering contracts do not match")
            if source_rr.max_outstanding <= 0 or source_rr.ordering is not RequestResponseOrdering.IN_ORDER:
                raise SemanticError(
                    "hierarchical request/response currently supports only "
                    "positive max_outstanding with ordering in_order"
                )
            if declaration.buffer_depth:
                raise SemanticError(
                    "generic 'buffer' is ambiguous on request/response connections; "
                    "use request_buffer and/or response_buffer"
                )
            rr_edges: dict[RequestResponseChannel, ir_module.HierarchicalConnection] = {}
            for channel in (RequestResponseChannel.REQUEST, RequestResponseChannel.RESPONSE):
                if channel is RequestResponseChannel.REQUEST:
                    source_path, destination_path = declaration.source, declaration.destination
                else:
                    # The response travels in the opposite physical direction
                    # while remaining part of the same logical connection.
                    source_path, destination_path = declaration.destination, declaration.source
                source_endpoint = request_response_endpoint(
                    source_path, source=True, channel=channel
                )
                destination_endpoint = request_response_endpoint(
                    destination_path, source=False, channel=channel
                )
                if source_endpoint.domain != destination_endpoint.domain:
                    raise SemanticError("hierarchical request/response endpoints must share a clock domain")
                if any(
                    edge.source.owner == source_endpoint.owner
                    and edge.source.name == source_endpoint.name
                    and edge.source.channel is channel
                    for edge in hierarchical_connections
                ):
                    raise SemanticError(
                        f"request/response endpoint '{source_endpoint.owner}.{source_endpoint.name}' "
                        f"is connected more than once on {channel.value}"
                    )
                edge = ir_module.HierarchicalConnection(
                        source_endpoint,
                        destination_endpoint,
                        request_buffer_depth=(
                            declaration.request_buffer_depth
                            if channel is RequestResponseChannel.REQUEST else 0
                        ),
                        response_buffer_depth=(
                            declaration.response_buffer_depth
                            if channel is RequestResponseChannel.RESPONSE else 0
                        ),
                    )
                hierarchical_connections.append(edge)
                rr_edges[channel] = edge
            request_response_connections.append(
                ir_module.RequestResponseConnection(
                    semantic_id=(
                        f"rr:{module.name}:{declaration.source}->{declaration.destination}"
                    ),
                    request=rr_edges[RequestResponseChannel.REQUEST],
                    response=rr_edges[RequestResponseChannel.RESPONSE],
                    request_type=source_rr.request_type,
                    response_type=source_rr.response_type,
                    max_outstanding=source_rr.max_outstanding,
                    ordering=source_rr.ordering,
                    requester=source_parts[0] if source_rr.role is RequestResponseRole.REQUESTER else destination_parts[0],
                    responder=destination_parts[0] if source_rr.role is RequestResponseRole.REQUESTER else source_parts[0],
                    clock_domain=rr_edges[RequestResponseChannel.REQUEST].source.domain,
                    reset_domain=reset,
                    reset_epoch_policy="synchronous_shared",
                    source_origin=next(
                        (
                            assignment.expression.origin
                            for assignment in source_child.assignments
                            if assignment.target.name == source_parts[1]
                            and assignment.expression.origin is not None
                        ),
                        None,
                    ),
                )
            )
            continue
        source = endpoint(declaration.source, source=True)
        destination = endpoint(declaration.destination, source=False)
        if source.protocol is not destination.protocol:
            raise SemanticError("hierarchical protocol connections require identical protocols")
        if source.payload_type != destination.payload_type:
            raise SemanticError("hierarchical protocol payload types do not match")
        if source.domain != destination.domain:
            raise SemanticError("hierarchical protocol endpoints must share a clock domain")
        array_edge = array_physical_owner(source.owner) or array_physical_owner(
            destination.owner
        )
        if array_edge and (
            declaration.buffer_depth
            or declaration.request_buffer_depth
            or declaration.response_buffer_depth
            or declaration.adapter is not None
            or declaration.crossing is not None
        ):
            raise SemanticError(
                "ready/valid instance-array connections must be direct and "
                "cannot use buffering, adapters, or crossings"
            )
        if declaration.adapter is not None:
            raise SemanticError("hierarchical protocol connections do not accept adapters")
        if array_edge:
            source_key = (source.owner, source.name)
            destination_key = (destination.owner, destination.name)
            if source_key in array_rv_sources:
                raise SemanticError(
                    f"ready/valid array endpoint '{source.owner}.{source.name}' "
                    "has multiple consumers"
                )
            if destination_key in array_rv_destinations:
                raise SemanticError(
                    f"ready/valid array endpoint "
                    f"'{destination.owner}.{destination.name}' has multiple drivers"
                )
            array_rv_sources.add(source_key)
            array_rv_destinations.add(destination_key)
        hierarchical_connections.append(
            ir_module.HierarchicalConnection(
                source, destination, buffer_depth=declaration.buffer_depth
            )
        )
        # A hierarchical ready/valid link drives both directions of the
        # physical interface.  Forward payload/valid belong to the
        # destination; backward ready belongs to the source.  Count that
        # source-side ready as an assignment so top-level completeness checks
        # agree with the typed connection rather than requiring a duplicate
        # source assignment.
        if source.owner == module.name and source.protocol is InterfaceProtocol.READY_VALID:
            assigned_outputs.add((source.name, None, ReadyValidSignal.READY))
        if destination.owner == module.name:
            assigned_outputs.update(
                {
                    (destination.name, None, ReadyValidSignal.PAYLOAD),
                    (destination.name, None, ReadyValidSignal.VALID),
                }
            )

    for array, length in module_context.instance_arrays.items():
        for index in range(length):
            owner = f"{array}[{index}]"
            child = child_irs[owner]
            if not child.ports or any(
                port.protocol is not InterfaceProtocol.READY_VALID
                for port in child.ports
            ):
                continue
            for port in child.inputs:
                if (owner, port.name) not in array_rv_destinations:
                    raise SemanticError(
                        f"ready/valid instance '{owner}' input '{port.name}' "
                        "has no compile-time indexed connection"
                    )
            for port in child.outputs:
                if (owner, port.name) not in array_rv_sources:
                    raise SemanticError(
                        f"ready/valid instance '{owner}' output '{port.name}' "
                        "has no compile-time indexed connection"
                    )

    hierarchical_assignments = list(module.assignments)
    for declaration in module.instances:
        hierarchical_assignments.extend(
            ast.Assignment(f"{declaration.name}.{binding.target}", binding.expression)
            for binding in declaration.bindings
        )
    for assignment in hierarchical_assignments:
        root = assignment.target.split(".", 1)[0]
        if root in resource_symbols:
            continue
        if root in instance_names:
            parts = assignment.target.split(".")
            if len(parts) != 2:
                raise SemanticError("instance binding must select one child port")
            child = child_irs[root]
            child_port = next((port for port in child.inputs if port.name == parts[1]), None)
            if child_port is None:
                raise SemanticError(f"instance '{root}' has no input port '{parts[1]}'")
            if child_port.protocol is not InterfaceProtocol.WIRE:
                raise SemanticError(
                    f"protocol endpoint '{assignment.target}' must use connect, "
                    "not an inline scalar binding"
                )
            bound = _check_typed_boundary(
                assignment.expression, value_symbols, child_port.type, module_context
            )
            if bound.type != child_port.type:
                raise SemanticError(
                    f"binding '{assignment.target}' has type {bound.type}, expected {child_port.type}"
                )
            if any(item.instance == root and item.port == child_port.name for item in instance_bindings):
                raise SemanticError(f"instance input '{assignment.target}' is assigned more than once")
            instance_bindings.append(ir_module.InstancePortBinding(root, child_port.name, bound))
            continue
        if "." not in assignment.target and assignment.target in {
            item.name for item in locals_
        }:
            continue
        channel: RequestResponseChannel | None = None
        target_text = assignment.target
        for prefix in sorted(module_context.aggregate_paths, key=len, reverse=True):
            if target_text == prefix or target_text.startswith(prefix + "."):
                target_text = module_context.aggregate_paths[prefix] + target_text[len(prefix):]
                break
        if root in request_response_symbols:
            target, channel, signal, target_type = _resolve_request_response_target(
                target_text, request_response_symbols
            )
        else:
            target, signal, target_type = _resolve_assignment_target(
                target_text, symbols
            )
        target_key = (target.name, channel, signal)
        if target_key in assigned_outputs:
            rendered = _render_assignment_target(target, signal, channel)
            raise SemanticError(f"output '{rendered}' is assigned more than once")

        expression_context = (
            replace(module_context, allow_runtime_instance_projection=True)
            if isinstance(target, ir_module.Port)
            and target.direction is ir_module.PortDirection.OUTPUT
            and target.protocol is InterfaceProtocol.WIRE
            and signal is None
            and channel is None
            else module_context
        )
        if isinstance(assignment.expression, ast.ExploreExpr):
            if _contains_explore(assignment.expression.expression):
                raise SemanticError("nested explore is not supported")
            if signal is not None or channel is not None:
                raise SemanticError("explore currently requires a wire output")
            operand = _check_expression(
                assignment.expression.expression,
                value_symbols,
                target_type,
                module_context,
            )
            operand = _expand_exploration_calls(
                operand, functions, module_context
            )
            objective = assignment.expression.objective
            objective_metric: ir_expr.CostMetric = (
                ir_expr.CostMetric.FMAX_EST
                if objective is not None and objective.metric.value == "fmax_est"
                else ir_expr.CostMetric(objective.metric.value)
                if objective is not None
                else ir_expr.CostMetric.LUT
            )
            if objective is not None and objective.direction == "maximize" and (
                objective_metric is not ir_expr.CostMetric.FMAX_EST
            ):
                raise SemanticError("maximize currently supports only fmax_est")
            allowed = tuple(
                TransformFamily(item.value) for item in assignment.expression.allowed
            )
            if TransformFamily.PIPELINE in allowed and not module_context.allow_delay:
                raise SemanticError("allow pipeline requires a module clock and reset")
            try:
                result = explore(
                    ExplorationRequest(
                        operand,
                        allowed,
                        tuple(
                            TransformFamily(item.value)
                            for item in assignment.expression.avoided
                        ),
                        constraints_from_syntax(assignment.expression.constraints),
                        objective_metric,
                        source_origin=assignment.expression.origin,
                        equivalences=tuple(equivalences),
                        formal_config=formal_config,
                        formal_verifier=formal_verifier,
                    )
                    ,
                    ExplorationContext(
                        target.name,
                        target_type,
                        module_context.allocate_delay,
                        candidate_site_owner,
                        "source_explore",
                    ),
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
            expression = result.selected.expression
            if exploration_results is not None:
                exploration_results.append(result)
            exploration_origin = _semantic_origin(
                assignment.expression, module_context
            )
            if exploration_origin is not None:
                expression = replace(expression, origin=exploration_origin)
        elif isinstance(assignment.expression, ast.ImplementationChoiceExpr):
            if signal is not None or channel is not None:
                raise SemanticError(
                    "implementation choices currently require a wire output"
                )
            expression_context = replace(
                module_context,
                allow_implementation_choice=True,
            )
        if isinstance(assignment.expression, ast.ArchitectureExpr):
            if signal is not None or channel is not None:
                raise SemanticError(
                    "architecture(auto) currently requires a wire output"
                )
            operand = _check_expression(
                assignment.expression.expression,
                value_symbols,
                target_type,
                module_context,
            )
            operand = _inline_semantic_locals(operand, tuple(locals_))
            operand = _expand_exploration_calls(
                operand, functions, module_context
            )
            exploration_origin = _semantic_origin(
                assignment.expression, module_context
            )
            if exploration_origin is not None:
                operand = replace(operand, origin=exploration_origin)
            constraints = tuple(
                ir_architectures.ArchitectureConstraint(
                    ir_architectures.ArchitectureMetric(constraint.metric.value),
                    constraint.maximum,
                )
                for constraint in assignment.expression.constraints
            )
            try:
                architecture = explore_architecture(
                    target.name,
                    operand,
                    target_type,
                    constraints,
                )
            except ArchitectureExplorationError as error:
                raise SemanticError(str(error)) from error
            architecture_explorations.append(architecture)
            expression = architecture.selected_candidate.expression
            if exploration_origin is not None:
                expression = replace(expression, origin=exploration_origin)
        elif (
            isinstance(assignment.expression, ast.PipelineExpr)
            and assignment.expression.stages is None
        ):
            if signal is not None or channel is not None:
                raise SemanticError(
                    "pipeline(auto) currently requires a wire output"
                )
            if not module_context.allow_delay:
                raise SemanticError("pipeline requires a module clock and reset")
            operand = _check_expression(
                assignment.expression.expression,
                value_symbols,
                target_type,
                module_context,
            )
            operand = _inline_semantic_locals(operand, tuple(locals_))
            operand = _expand_exploration_calls(
                operand, functions, module_context
            )
            exploration_origin = _semantic_origin(
                assignment.expression, module_context
            )
            if exploration_origin is not None:
                operand = replace(operand, origin=exploration_origin)
            constraints = tuple(
                ir_pipelines.PipelineConstraint(
                    ir_pipelines.PipelineMetric(
                        "throughput"
                        if constraint.metric is ast.PipelineMetric.INITIATION_INTERVAL
                        else constraint.metric.value
                    ),
                    ir_pipelines.PipelineRelation(constraint.relation.value),
                    constraint.value,
                )
                for constraint in assignment.expression.constraints
            )
            try:
                exploration = explore_pipeline(
                    target.name,
                    operand,
                    target_type,
                    constraints,
                    module_context.allocate_delay,
                )
            except PipelineExplorationError as error:
                raise SemanticError(str(error)) from error
            pipeline_explorations.append(exploration)
            expression = exploration.selected_candidate.expression
            if exploration_origin is not None:
                expression = replace(expression, origin=exploration_origin)
        elif not isinstance(assignment.expression, ast.ExploreExpr):
            expression = _check_expression(
                assignment.expression,
                value_symbols,
                target_type,
                expression_context,
            )
        if expression_context is not module_context:
            module_context.next_delay_instance = expression_context.next_delay_instance
        expression = _coerce_raw_target(expression, target_type)
        if expression.type != target_type:
            rendered = _render_assignment_target(target, signal, channel)
            raise SemanticError(
                f"cannot assign {expression.type} expression to "
                f"{target_type} output '{rendered}'",
                code="ZL-WIDTH-ASSIGNMENT",
                primary=expression.origin,
                fixes=("use an explicit exact-width conversion",),
            )
        assignments.append(ir_module.Assignment(target, expression, signal, channel))
        assigned_outputs.add(target_key)

    # A compile-time instance array denotes a fixed set of physical children,
    # not a broadcast binding.  Validate every scalar input here so both
    # backends consume the same complete typed wiring graph.
    for array, length in module_context.instance_arrays.items():
        for index in range(length):
            physical = f"{array}[{index}]"
            child = child_irs[physical]
            for port in child.inputs:
                if port.protocol is not InterfaceProtocol.WIRE:
                    continue
                if not any(
                    binding.instance == physical and binding.port == port.name
                    for binding in instance_bindings
                ):
                    raise SemanticError(
                        f"instance '{physical}' input '{port.name}' has no "
                        "compile-time indexed binding"
                    )

    _reject_instance_output_dependency_cycles(
        child_irs,
        tuple(instance_bindings),
        tuple(locals_),
    )

    for connection in connections:
        if (
            connection.buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            connection_outputs = _connection_output_keys(connection)
        else:
            direct_assignments = _direct_connection_assignments(connection)
            connection_outputs = tuple(
                (assignment.target.name, assignment.channel, assignment.signal)
                for assignment in direct_assignments
            )
        for key in connection_outputs:
            if key in assigned_outputs:
                target_name, channel, signal = key
                target = symbols[target_name]
                rendered = _render_assignment_target(
                    target, signal, channel
                )
                raise SemanticError(
                    f"connection and explicit assignment both drive '{rendered}'"
                )
            assigned_outputs.add(key)
        if (
            not connection.buffer_depth
            and connection.adapter is None
            and connection.crossing is None
        ):
            assignments.extend(direct_assignments)

    for assignment in assignments:
        if not isinstance(assignment.target, ir_module.Port):
            continue
        target_domain = assignment.target.domain
        source_domains = _expression_domains(
            assignment.expression, symbols, register_symbols
        )
        mismatched = {
            domain
            for domain in source_domains
            if domain is not None and domain != target_domain
        }
        if mismatched:
            source_domain = sorted(mismatched)[0]
            raise SemanticError(
                f"implicit clock-domain crossing in assignment to "
                f"'{assignment.target.name}' from '{source_domain}' to "
                f"'{target_domain}' is not allowed",
                code="ZL-DOMAIN-CROSSING",
                primary=assignment.expression.origin,
                fixes=("insert an explicit supported clock-domain crossing",),
            )
    for assignment in next_assignments:
        source_domains = _expression_domains(
            assignment.expression, symbols, register_symbols
        )
        mismatched = {
            domain
            for domain in source_domains
            if domain is not None and domain != assignment.target.domain
        }
        if mismatched:
            raise SemanticError(
                f"implicit clock-domain crossing in next state for "
                f"'{assignment.target.name}' is not allowed",
                code="ZL-DOMAIN-CROSSING",
                primary=assignment.expression.origin,
            )

    # Infer request/response ownership from the existing field assignments.
    # Requester and responder declarations are deliberately not a new syntax:
    # each owns one complete ready/valid half of the bidirectional interface.
    requester_fields = frozenset(
        {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
        }
    )
    responder_fields = frozenset(
        {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
        }
    )
    for index, interface in enumerate(request_responses):
        owned = frozenset(
            (assignment.channel, assignment.signal)
            for assignment in assignments
            if assignment.target is interface
            and assignment.channel is not None
            and assignment.signal is not None
        )
        if owned <= requester_fields:
            role = RequestResponseRole.REQUESTER
        elif owned <= responder_fields:
            role = RequestResponseRole.RESPONDER
        else:
            # Preserve the established diagnostic for a requester that tries
            # to drive an incoming field, while also rejecting mixed roles.
            raise SemanticError(
                f"cannot drive incoming request/response field on interface "
                f"'{interface.name}'; requester and responder ownership must "
                "be disjoint"
            )
        updated = replace(interface, role=role)
        request_responses[index] = updated
        request_response_symbols[interface.name] = updated

    # Assignment targets are semantic identities, not name-only references.
    # Ownership inference replaces each request/response declaration with its
    # role-qualified value, so retarget assignments to that same canonical
    # object before the module is published.  Otherwise canonical restoration
    # correctly finds the updated interface while the original semantic module
    # still contains stale default-requester targets.
    assignments = [
        replace(
            assignment,
            target=request_response_symbols[assignment.target.name],
        )
        if isinstance(assignment.target, ir_module.RequestResponseInterface)
        else assignment
        for assignment in assignments
    ]

    required_outputs: list[
        tuple[
            ir_module.Port | ir_module.RequestResponseInterface,
            RequestResponseChannel | None,
            InterfaceSignal | None,
        ]
    ] = [
        *((port, None, None) for port in outputs.values()),
    ]
    for port in protocol_interfaces.values():
        if port.protocol is InterfaceProtocol.READY_VALID:
            if port.direction is ir_module.PortDirection.INPUT:
                required_outputs.append((port, None, ReadyValidSignal.READY))
            else:
                required_outputs.extend(
                    (
                        (port, None, ReadyValidSignal.PAYLOAD),
                        (port, None, ReadyValidSignal.VALID),
                    )
                )
        elif port.protocol is InterfaceProtocol.PACKET:
            if port.direction is ir_module.PortDirection.INPUT:
                required_outputs.append((port, None, PacketSignal.READY))
            else:
                required_outputs.extend(
                    (
                        (port, None, PacketSignal.PAYLOAD),
                        (port, None, PacketSignal.VALID),
                        (port, None, PacketSignal.LAST),
                    )
                )
        elif port.protocol is InterfaceProtocol.CREDIT and (
            port.direction is ir_module.PortDirection.INPUT
        ):
            required_outputs.append((port, None, CreditSignal.RETURN))
        elif port.protocol is InterfaceProtocol.CREDIT:
            required_outputs.extend(
                (
                    (port, None, CreditSignal.PAYLOAD),
                    (port, None, CreditSignal.SEND),
                )
            )
        elif port.direction is ir_module.PortDirection.INPUT:
            required_outputs.extend(
                (
                    (port, None, VirtualChannelCreditSignal.RETURN),
                    (port, None, VirtualChannelCreditSignal.RETURN_VC),
                )
            )
        else:
            required_outputs.extend(
                (
                    (port, None, VirtualChannelCreditSignal.PAYLOAD),
                    (port, None, VirtualChannelCreditSignal.VC),
                    (port, None, VirtualChannelCreditSignal.SEND),
                )
            )
    for interface in request_responses:
        if interface.role is RequestResponseRole.REQUESTER:
            required_outputs.extend(
                (
                    (interface, RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
                    (interface, RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
                    (interface, RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
                )
            )
        else:
            required_outputs.extend(
                (
                    (interface, RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
                    (interface, RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD),
                    (interface, RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
                )
            )
    missing = [
        (target, channel, signal)
        for target, channel, signal in required_outputs
        if (target.name, channel, signal) not in assigned_outputs
    ]
    if missing:
        target, channel, signal = missing[0]
        raise SemanticError(
            f"output '{_render_assignment_target(target, signal, channel)}' "
            "has no assignment"
        )

    _reject_interface_dependency_cycles(tuple(assignments))

    contracts: list[ir_verification.Contract] = []
    contract_names: set[str] = set()
    contract_symbols: dict[str, _ValueSymbol] = {**value_symbols, **outputs}
    contract_context = _ExpressionContext(
        function_signatures,
        allow_delay=True,
        generic_functions=generic_functions,
        compile_time_constants=pure_context.compile_time_constants,
        static_callables=pure_context.static_callables,
        operator_declarations=module.operators,
        struct_declarations=module.structs,
        # Verification-only specialization is isolated from the production
        # callable catalog. A pure helper used only by an assertion must not
        # alter selected hardware identity or backend helper emission.
        generic_specializations=list(pure_context.generic_specializations),
        function_definitions=dict(pure_context.function_definitions),
        callable_definitions=dict(pure_context.callable_definitions),
        callable_use_counts=dict(pure_context.callable_use_counts),
        specializations_in_progress=set(pure_context.specializations_in_progress),
        specialization_budget_costs=dict(pure_context.specialization_budget_costs),
        compile_time_real_quantize_cache=(
            dict(pure_context.compile_time_real_quantize_cache)
        ),
        structs=struct_types,
        parameters=parameter_values,
        unresolved_parameters=unresolved_parameter_names,
        type_resolver=type_resolver,
        generic_dependency_identity=pure_context.generic_dependency_identity,
        compile_time_budget=_CompileTimeBudget(),
        formal_config=pure_context.formal_config,
        formal_verifier=pure_context.formal_verifier,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
        source_digests=source_digests,
        functional_binder_ordinals=dict(pure_context.functional_binder_ordinals),
        next_functional_binder_ordinal=[
            pure_context.next_functional_binder_ordinal[0]
        ],
    )
    domains_by_clock = {domain.clock: domain for domain in clock_domains}
    for declaration in module.contracts:
        if declaration.name in contract_names:
            raise SemanticError(f"duplicate contract '{declaration.name}'")
        contract_names.add(declaration.name)
        domain = domains_by_clock.get(declaration.clock)
        if domain is None:
            raise SemanticError(
                f"contract '{declaration.name}' references unknown clock "
                f"'{declaration.clock}'"
            )
        if declaration.reset != domain.reset:
            raise SemanticError(
                f"contract '{declaration.name}' must use reset '{domain.reset}' "
                f"for clock '{domain.clock}'"
            )
        expression = _check_expression(
            declaration.expression,
            contract_symbols,
            BitType(),
            contract_context,
        )
        if expression.type != BitType():
            raise SemanticError(
                f"contract '{declaration.name}' expression must be bit, got "
                f"{expression.type}"
            )
        _validate_contract_expression(expression, symbols, declaration.name)
        if declaration.kind.value == "assume":
            _validate_assumption_ownership(
                expression,
                symbols,
                request_response_symbols,
                declaration.name,
            )
        expression_domains = {
            item
            for item in _expression_domains(expression, symbols, register_symbols)
            if item is not None
        }
        mismatched_domains = expression_domains - {declaration.clock}
        if mismatched_domains:
            mismatched = sorted(mismatched_domains)[0]
            raise SemanticError(
                f"contract '{declaration.name}' clocked by '{declaration.clock}' "
                f"references signal in domain '{mismatched}'"
            )
        contracts.append(
            ir_verification.Contract(
                ir_verification.ContractKind(declaration.kind.value),
                declaration.name,
                declaration.clock,
                declaration.reset,
                expression,
            )
        )

    # First-class verification declarations form a source overlay.  They use
    # the ordinary expression type checker but never feed optimization, range
    # refinement, or production backend selection.
    # Goal identities name one semantic module specialization, not one source
    # snapshot.  In particular, adding an unrelated goal or changing comments
    # must not rename every existing goal.  The verification identity still
    # hashes the complete predicates and the selected hardware identity later.
    verification_namespace = stable_digest({
        "schema": "zlang-verification-module-specialization-v1",
        "module": module.name,
        "parameters": [
            {"name": item.name, "kind": item.kind, "default": str(item.default)}
            for item in module.parameters
        ],
        "value_bindings": [
            [name, value] for name, value in sorted(parameter_values.items())
        ],
        "type_bindings": [
            [name, str(value)]
            for name, value in sorted((specialization_type_bindings or {}).items())
        ],
        "constant_bindings": [
            [
                name,
                str(value.type),
                constant_runtime_value(value),
            ]
            for name, value in sorted(
                (specialization_constant_bindings or {}).items()
            )
        ],
        "callable_bindings": [
            [name, value.canonical_identity]
            for name, value in sorted(
                (specialization_callable_bindings or {}).items()
            )
        ],
    })

    def verification_id(*parts: object) -> str:
        return hashlib.sha256(
            repr((verification_namespace, *parts)).encode("utf-8")
        ).hexdigest()

    def verification_origin(
        declaration: object, construct: str
    ) -> SourceOrigin | None:
        span = getattr(declaration, "origin", None)
        return (
            SourceOrigin(
                span,
                construct,
                effective_source_unit,
                effective_source_digest,
            )
            if span is not None else None
        )

    def verification_domain(
        name: str, requested_clock: str | None
    ) -> ir_cdc.ClockDomain:
        if requested_clock is None:
            if len(clock_domains) != 1:
                raise SemanticError(
                    f"verification declaration '{name}' must name a clock: "
                    "clock inference requires exactly one clock/reset domain"
                )
            return clock_domains[0]
        selected = domains_by_clock.get(requested_clock)
        if selected is None:
            raise SemanticError(
                f"verification declaration '{name}' references unknown clock "
                f"'{requested_clock}'"
            )
        return selected

    def typed_verification_expression(
        declaration: object,
        expression_ast: ast.Expression,
        *,
        name: str,
        clock_name: str,
        ownership: str,
    ) -> ir_expr.Expression:
        expression = _check_expression(
            expression_ast, contract_symbols, BitType(), contract_context
        )
        if expression.type != BitType():
            raise SemanticError(
                f"verification clause '{name}' expression must be bit, got "
                f"{expression.type}"
            )
        expression = _inline_semantic_locals(expression, tuple(locals_))
        expression = _expand_analysis_calls(
            expression,
            contract_context,
            purpose=f"verification clause '{name}'",
        )
        _validate_verification_expression(
            expression,
            symbols,
            name,
            public_only=ownership == "ensure",
        )
        if ownership == "require":
            _validate_assumption_ownership(
                expression, symbols, request_response_symbols, name
            )
            try:
                if _is_constant_expression(expression) and not bool(
                    constant_runtime_value(expression)
                ):
                    raise SemanticError(
                        f"verification requirement '{name}' is compile-time false"
                    )
            except ConstantExpressionError:
                pass
        if ownership == "ensure" and not _observes_public_implementation_output(
            expression, symbols, request_response_symbols
        ):
            raise SemanticError(
                f"verification ensure '{name}' must observe at least one "
                "implementation-owned public output"
            )
        expression_domains = {
            item
            for item in _expression_domains(expression, symbols, register_symbols)
            if item is not None
        }
        mismatched_domains = expression_domains - {clock_name}
        if mismatched_domains:
            mismatched = sorted(mismatched_domains)[0]
            raise SemanticError(
                f"verification clause '{name}' clocked by '{clock_name}' "
                f"references signal in domain '{mismatched}'"
            )
        return expression

    # Use mutable builders internally, then freeze them in deterministic source
    # order.  The module-global scope is per inferred/explicit clock domain.
    verification_builders: dict[
        tuple[str, str],
        dict[str, object],
    ] = {}
    verification_order: list[tuple[str, str]] = []

    def scope_builder(
        scope_name: str,
        domain: ir_cdc.ClockDomain,
        origin: SourceOrigin | None,
    ) -> dict[str, object]:
        key = (scope_name, domain.clock)
        existing = verification_builders.get(key)
        if existing is not None:
            return existing
        scope_semantic_id = verification_id("scope", scope_name, domain.clock)
        result_builder: dict[str, object] = {
            "semantic_id": scope_semantic_id,
            "name": scope_name,
            "clock": domain.clock,
            "reset": domain.reset,
            "requirements": [],
            "goals": [],
            "names": set(),
            "origin": origin,
        }
        verification_builders[key] = result_builder
        verification_order.append(key)
        return result_builder

    def reserve_clause(builder: dict[str, object], name: str) -> None:
        names = builder["names"]
        assert isinstance(names, set)
        if name in names:
            raise SemanticError(
                f"duplicate verification clause '{name}' in scope "
                f"'{builder['name']}'"
            )
        names.add(name)

    # Legacy contracts normalize into module-global scopes while the original
    # records remain available for compatibility APIs and source rendering.
    for declaration, typed_contract in zip(module.contracts, contracts, strict=True):
        domain = domains_by_clock[typed_contract.clock]
        builder = scope_builder("$module", domain, None)
        reserve_clause(builder, typed_contract.name)
        if typed_contract.kind is ir_verification.ContractKind.ASSUME:
            requirement = ir_verification.VerificationRequirement(
                verification_id(
                    "scope", "$module", domain.clock,
                    "requirement", typed_contract.name,
                ),
                typed_contract.name,
                typed_contract.expression,
                verification_origin(declaration, f"assume {typed_contract.name}"),
            )
            requirements = builder["requirements"]
            assert isinstance(requirements, list)
            requirements.append(requirement)
        else:
            goal = ir_verification.VerificationGoal(
                verification_id(
                    "scope", "$module", domain.clock,
                    "assert", typed_contract.name,
                ),
                str(builder["semantic_id"]),
                ir_verification.VerificationGoalKind.ASSERT,
                typed_contract.name,
                typed_contract.expression,
                verification_origin(declaration, f"guarantee {typed_contract.name}"),
            )
            goals = builder["goals"]
            assert isinstance(goals, list)
            goals.append(goal)

    for declaration in module.verification_goals:
        domain = verification_domain(declaration.name, declaration.clock)
        builder = scope_builder("$module", domain, None)
        reserve_clause(builder, declaration.name)
        expression = typed_verification_expression(
            declaration,
            declaration.expression,
            name=declaration.name,
            clock_name=domain.clock,
            ownership="assert",
        )
        kind = ir_verification.VerificationGoalKind(declaration.kind.value)
        goal = ir_verification.VerificationGoal(
            verification_id(
                "scope", "$module", domain.clock,
                kind.value, declaration.name,
            ),
            str(builder["semantic_id"]),
            kind,
            declaration.name,
            expression,
            verification_origin(
                declaration, f"{kind.value} {declaration.name}"
            ),
        )
        goals = builder["goals"]
        assert isinstance(goals, list)
        goals.append(goal)

    explicit_scope_names: set[str] = set()
    for declaration in module.verification_scopes:
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
        domain = verification_domain(declaration.name, declaration.clock)
        builder = scope_builder(
            declaration.name,
            domain,
            verification_origin(declaration, f"contract {declaration.name}"),
        )
        for requirement_declaration in declaration.requirements:
            reserve_clause(builder, requirement_declaration.name)
            expression = typed_verification_expression(
                requirement_declaration,
                requirement_declaration.expression,
                name=requirement_declaration.name,
                clock_name=domain.clock,
                ownership="require",
            )
            requirements = builder["requirements"]
            assert isinstance(requirements, list)
            requirements.append(ir_verification.VerificationRequirement(
                verification_id(
                    "scope", declaration.name, domain.clock,
                    "requirement", requirement_declaration.name,
                ),
                requirement_declaration.name,
                expression,
                verification_origin(
                    requirement_declaration,
                    f"require {requirement_declaration.name}",
                ),
            ))
        for goal_declaration in declaration.goals:
            reserve_clause(builder, goal_declaration.name)
            kind = ir_verification.VerificationGoalKind(
                goal_declaration.kind.value
            )
            expression = typed_verification_expression(
                goal_declaration,
                goal_declaration.expression,
                name=goal_declaration.name,
                clock_name=domain.clock,
                ownership="ensure" if kind is ir_verification.VerificationGoalKind.ENSURE else "assert",
            )
            goals = builder["goals"]
            assert isinstance(goals, list)
            goals.append(ir_verification.VerificationGoal(
                verification_id(
                    "scope", declaration.name, domain.clock,
                    kind.value, goal_declaration.name,
                ),
                str(builder["semantic_id"]),
                kind,
                goal_declaration.name,
                expression,
                verification_origin(
                    goal_declaration,
                    f"{kind.value} {goal_declaration.name}",
                ),
            ))

    verification_scopes: list[ir_verification.VerificationScope] = []
    for key in verification_order:
        builder = verification_builders[key]
        goals = builder["goals"]
        requirements = builder["requirements"]
        assert isinstance(goals, list) and isinstance(requirements, list)
        verification_scopes.append(ir_verification.VerificationScope(
            str(builder["semantic_id"]),
            str(builder["name"]),
            str(builder["clock"]),
            str(builder["reset"]),
            tuple(requirements),
            tuple(goals),
            builder["origin"] if isinstance(builder["origin"], SourceOrigin) else None,
        ))

    for assignment in (*assignments, *next_assignments):
        _expression_latency(assignment.expression)
    for rule in rules:
        _expression_latency(rule.guard)
        for action in rule.actions:
            _expression_latency(action.expression)
    for fifo in fifos:
        for control in (fifo.data, fifo.push, fifo.pop):
            if control is None:
                continue
            latency = _expression_latency(control)
            if latency not in {None, 0}:
                raise SemanticError("FIFO control expressions cannot be staged")
    for group in resolved_transition.action_groups:
        for action in group.actions:
            for operand in action.operands:
                latency = _expression_latency(operand)
                if latency not in {None, 0}:
                    raise SemanticError("state action operands cannot be staged")
    for memory in memories:
        for control in (
            memory.read_address,
            memory.write_enable,
            memory.write_address,
            memory.write_data,
            memory.write_mask,
        ):
            if control is None:
                continue
            latency = _expression_latency(control)
            if latency not in {None, 0}:
                raise SemanticError("memory control expressions cannot be staged")
    for rom in roms:
        latency = _expression_latency(rom.read_address)
        if latency not in {None, 0}:
            raise SemanticError("ROM read-address expressions cannot be staged")

    (
        timing_contract,
        output_timings,
        instance_output_timings,
    ) = analyze_public_module_timing(
        module,
        ports=tuple(ports),
        assignments=tuple(assignments),
        locals_=tuple(locals_),
        clock_domains=clock_domains,
        child_irs=child_irs,
        elaborated_instances=tuple(elaborated_instances),
        instance_bindings=tuple(instance_bindings),
        request_responses=tuple(request_responses),
        aggregate_protocol_endpoints=tuple(aggregate_protocol_endpoints),
        csr_blocks=tuple(csr_blocks),
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
    )

    module_signature = _named_module_signature(
        module,
        resolver=type_resolver,
        specialization_type_bindings=specialization_type_bindings,
        actual_ports=source_scalar_ports,
        actual_clock_domains=clock_domains,
        actual_request_responses=tuple(request_responses),
        actual_aggregate_endpoints=tuple(aggregate_protocol_endpoints),
        actual_timing=timing_contract,
        source_unit=effective_source_unit,
        source_digest=effective_source_digest,
        source_digests=source_digests,
    )

    external_contract = None
    if module.external_model is not None:
        if module_signature is None or external_model_function is None:
            raise SemanticError(
                f"external module '{module.name}' requires a resolved named interface",
                code="ZL-EXTERN-SIGNATURE",
            )
        external_contract = ir_external.ExternalModuleContract(
            module.name,
            module_signature,
            external_model_function.callee_identity,
            source_origin=(
                SourceOrigin(
                    module.external_origin,
                    f"external module {module.name}",
                    effective_source_unit,
                    effective_source_digest,
                )
                if module.external_origin is not None else None
            ),
        )

    csr_access: ir_csr.CsrAccessInterface | None = None
    if csr_blocks:
        reserved = {"addr", "write", "wdata", "read", "rdata", "ready"}
        collision = next((port.name for port in ports if port.name in reserved), None)
        if collision is not None:
            raise SemanticError(
                f"CSR module port '{collision}' conflicts with the canonical CSR access ABI"
            )
        csr_access = ir_csr.CsrAccessInterface(
            semantic_id=f"csr-access:{csr_module_identity}"
        )
        ports.extend(
            ir_module.Port(
                ir_module.PortDirection.INPUT, name, type_,
                domain=clock,
            )
            for name, type_ in csr_access.input_types
        )
        ports.extend(
            ir_module.Port(
                ir_module.PortDirection.OUTPUT, name, type_,
                domain=clock,
            )
            for name, type_ in csr_access.output_types
        )
        for block in csr_blocks:
            for binding in block.state_bindings:
                ports.extend((
                    ir_module.Port(
                        ir_module.PortDirection.OUTPUT,
                        ir_csr.csr_state_port_name(binding),
                        binding.canonical_type,
                        domain=clock,
                    ),
                    ir_module.Port(
                        ir_module.PortDirection.OUTPUT,
                        ir_csr.csr_write_hit_port_name(binding),
                        ir_csr.BitType(),
                        domain=clock,
                    ),
                    ir_module.Port(
                        ir_module.PortDirection.OUTPUT,
                        ir_csr.csr_write_value_port_name(binding),
                        binding.canonical_type,
                        domain=clock,
                    ),
                ))

    module_specialization_bindings = (
        *(
            _constant_specialization_binding(
                parameter.name,
                specialization_constant_bindings[parameter.name],
                generic_dependency_identity,
            )
            for parameter in module.parameters
            if parameter.kind == "constant"
            and specialization_constant_bindings is not None
            and parameter.name in specialization_constant_bindings
        ),
        *(
            _callable_specialization_binding(
                parameter.name,
                specialization_callable_bindings[parameter.name],
                generic_dependency_identity,
            )
            for parameter in module.parameters
            if parameter.kind == "callable"
            and specialization_callable_bindings is not None
            and parameter.name in specialization_callable_bindings
        ),
    )

    result = ir_module.Module(
        name=module.name,
        ports=tuple(ports),
        assignments=tuple(assignments),
        locals=tuple(locals_),
        structs=struct_types,
        enums=enum_types,
        tagged_unions=tagged_union_types,
        functions=tuple(functions),
        clock=clock,
        reset=reset,
        registers=tuple(registers),
        next_assignments=tuple(next_assignments),
        request_responses=tuple(request_responses),
        connections=tuple(connections),
        csr_blocks=tuple(csr_blocks),
        csr_access=csr_access,
        rules=tuple(rules),
        rule_priorities=tuple(priorities),
        fifos=tuple(fifos),
        memories=tuple(memories),
        roms=tuple(roms),
        clock_domains=clock_domains,
        arbiters=tuple(arbiters),
        contracts=tuple(contracts),
        pipeline_explorations=tuple(pipeline_explorations),
        elastic_pipeline_regions=tuple(elastic_pipeline_regions),
        architecture_explorations=tuple(architecture_explorations),
        equivalences=tuple(equivalences),
        parameters=resolved_module_parameters,
        instances=tuple(instances),
        instance_bindings=tuple(instance_bindings),
        children=tuple(
            child_irs[item.semantic_path[-1]]
            for item in elaborated_instances
            if item.semantic_path
        ),
        elaborated_instances=tuple(elaborated_instances),
        protocol_endpoints=tuple(protocol_endpoints),
        hierarchical_connections=tuple(hierarchical_connections),
        request_response_connections=tuple(request_response_connections),
        protocol_schemas=tuple(protocol_schemas),
        aggregate_protocol_endpoints=tuple(aggregate_protocol_endpoints),
        aggregate_protocol_connections=tuple(aggregate_protocol_connections),
        library_imports=tuple(sorted(seen_imports)),
        library_dependencies=tuple(
            (item.logical_path, item.digest) for item in resolved_imports
        ),
        source_identity=module.source_identity,
        source_hash=module.source_hash,
        generic_specializations=tuple(sorted(
            pure_context.generic_specializations,
            key=lambda item: item.identity,
        )),
        resolved_transition=resolved_transition,
        timing_contract=timing_contract,
        output_timings=output_timings,
        instance_output_timings=instance_output_timings,
        root_module_identity=active_module_identity,
        dependency_closure=dependency_closure,
        module_signature=module_signature,
        callable_definitions=tuple(
            pure_context.callable_definitions[identity]
            for identity in sorted(pure_context.callable_definitions)
        ),
        external_contract=external_contract,
        specialization_bindings=module_specialization_bindings,
        verification_scopes=tuple(verification_scopes),
    )
    # Verification declarations are an optional source overlay.  Computing the
    # implementation-derived namespace walks the complete typed module, which
    # is intentionally necessary for stable goal identities but pure overhead
    # for the overwhelmingly common empty overlay.  Keep that work strictly
    # demand-driven without changing a single non-empty-overlay identity.
    if result.verification_scopes:
        verification_namespace = ir_verification.verification_module_identity(
            replace(result, verification_scopes=())
        )

        def finalized_verification_id(*parts: object) -> str:
            return hashlib.sha256(
                repr((verification_namespace, *parts)).encode("utf-8")
            ).hexdigest()

        finalized_scopes: list[ir_verification.VerificationScope] = []
        for scope in result.verification_scopes:
            scope_id = finalized_verification_id(
                "scope", scope.name, scope.clock
            )
            finalized_scopes.append(replace(
                scope,
                semantic_id=scope_id,
                requirements=tuple(
                    replace(
                        requirement,
                        semantic_id=finalized_verification_id(
                            "scope", scope.name, scope.clock,
                            "requirement", requirement.name,
                        ),
                    )
                    for requirement in scope.requirements
                ),
                goals=tuple(
                    replace(
                        goal,
                        semantic_id=finalized_verification_id(
                            "scope", scope.name, scope.clock,
                            goal.kind.value, goal.name,
                        ),
                        scope_id=scope_id,
                    )
                    for goal in scope.goals
                ),
            ))
        result = replace(result, verification_scopes=tuple(finalized_scopes))
    try:
        validate_hierarchical_connections(result, cache=selected_hierarchy_cache)
    except HierarchyError as error:
        raise SemanticError(str(error)) from error
    return result


def _validate_elastic_kernel_capture(
    expression: ir_expr.Expression,
    source_endpoint: str,
) -> None:
    """Enforce the frozen pure single-payload capture boundary.

    M31 may insert ``Pipeline`` nodes after this check.  Those nodes are the
    only state the elastic region is allowed to own; arbitrary source state or
    ready/valid control cannot be hidden in the selected scalar graph.
    """

    forbidden = (
        ir_expr.InputRef,
        ir_expr.RegisterRef,
        ir_expr.CreditRef,
        ir_expr.PacketRef,
        ir_expr.VirtualChannelCreditRef,
        ir_expr.RequestResponseRef,
        ir_expr.FifoRef,
        ir_expr.MemoryRef,
        ir_expr.RomRef,
        ir_expr.InstanceOutputRef,
        ir_expr.Delay,
    )
    for node in walk_expression(
        expression,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    ):
        if isinstance(node, ir_expr.ReadyValidRef):
            if (
                node.interface != source_endpoint
                or node.signal is not ReadyValidSignal.PAYLOAD
            ):
                raise SemanticError(
                    "elastic pipeline kernel may capture only the designated "
                    f"'{source_endpoint}.payload' value"
                )
            continue
        if isinstance(node, forbidden):
            raise SemanticError(
                "elastic pipeline kernel contains unsupported state/control "
                f"capture {type(node).__name__}"
            )


def _analyze_equivalences(declarations: tuple[ast.EquivDecl, ...]) -> list[ir_module.EquivalenceRule]:
    """Validate the deliberately small M27 rule language.

    Rules are structural declarations, not ordinary module expressions: their
    unbound names are pattern variables and only the safe M26 families are
    admitted.  This keeps arithmetic and effectful nodes out of the e-graph.
    """
    result: list[ir_module.EquivalenceRule] = []
    names: set[str] = set()
    allowed = {"or_zero", "xor_zero", "shift_zero", "mux_identity"}
    atom_re = re.compile(
        r"^(unsigned|signed|bits|bit|width|same_type|constant|power_of_two)"
        r"\(\s*([^()]*)\s*\)(?:\s*==\s*([0-9]+))?$"
    )
    identifier_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def vars_in(expr: ast.Expression) -> set[str]:
        if isinstance(expr, ast.NameExpr):
            return {expr.name}
        if isinstance(expr, ast.PatternConstantExpr):
            return {expr.witness}
        if isinstance(expr, ast.AddExpr):
            return vars_in(expr.left) | vars_in(expr.right)
        if isinstance(expr, ast.BinaryExpr):
            return vars_in(expr.left) | vars_in(expr.right)
        if isinstance(expr, ast.MuxExpr):
            return vars_in(expr.condition) | vars_in(expr.when_true) | vars_in(expr.when_false)
        if isinstance(expr, ast.ResizeExpr):
            return vars_in(expr.expression)
        if isinstance(expr, ast.NumberExpr):
            return set()
        raise SemanticError("equiv rules may contain only pure scalar value expressions")

    def pattern_kind(
        left: ast.Expression,
        right: ast.Expression,
    ) -> tuple[str, tuple[tuple[str, str], ...], str | None] | None:
        # Canonical orientation is intentionally accepted in either direction.
        pairs = ((left, right), (right, left))
        for a, b in pairs:
            if isinstance(b, ast.NameExpr) and isinstance(a, ast.BinaryExpr):
                if a.operator is ast.BinaryOperator.BIT_OR and isinstance(a.right, ast.PatternConstantExpr) and a.right.kind is ast.PatternConstantKind.ZERO and a.right.witness == b.name and isinstance(a.left, ast.NameExpr) and a.left.name == b.name:
                    return "or_zero", (("value", b.name),), a.operator.value
                if a.operator is ast.BinaryOperator.BIT_XOR and isinstance(a.right, ast.PatternConstantExpr) and a.right.kind is ast.PatternConstantKind.ZERO and a.right.witness == b.name and isinstance(a.left, ast.NameExpr) and a.left.name == b.name:
                    return "xor_zero", (("value", b.name),), a.operator.value
                if a.operator in {ast.BinaryOperator.SHIFT_LEFT, ast.BinaryOperator.SHIFT_RIGHT} and isinstance(a.right, ast.PatternConstantExpr) and a.right.kind is ast.PatternConstantKind.ZERO and a.right.witness == b.name and isinstance(a.left, ast.NameExpr) and a.left.name == b.name:
                    return "shift_zero", (("value", b.name),), a.operator.value
            if isinstance(a, ast.MuxExpr) and isinstance(a.when_true, ast.NameExpr) and isinstance(a.when_false, ast.NameExpr) and a.when_true.name == a.when_false.name and isinstance(b, ast.NameExpr) and b.name == a.when_true.name:
                if not isinstance(a.condition, ast.NameExpr):
                    return None
                return (
                    "mux_identity",
                    (("condition", a.condition.name), ("value", b.name)),
                    None,
                )
            if isinstance(a, ast.ResizeExpr) and isinstance(a.expression, ast.NameExpr) and isinstance(b, ast.NameExpr) and b.name == a.expression.name:
                return "resize_identity", (("value", b.name),), None
        return None

    def analyze_guard(
        text: str,
        variables: set[str],
    ) -> ir_module.EquivalenceGuardPredicate:
        match = atom_re.fullmatch(text.strip())
        if match is None:
            raise SemanticError(f"unsupported equiv guard '{text}'")
        kind = ir_module.EquivalenceGuardKind(match.group(1))
        raw_arguments = match.group(2).strip()
        arguments = tuple(
            item.strip() for item in raw_arguments.split(",") if item.strip()
        )
        expected_arity = 2 if kind is ir_module.EquivalenceGuardKind.SAME_TYPE else 1
        if len(arguments) != expected_arity or any(
            identifier_re.fullmatch(item) is None for item in arguments
        ):
            raise SemanticError(
                f"equiv guard '{text}' expects {expected_arity} bound identifier"
                f"{'s' if expected_arity != 1 else ''}"
            )
        unbound = tuple(item for item in arguments if item not in variables)
        if unbound:
            raise SemanticError(
                f"equiv guard '{text}' references unbound pattern variable "
                f"'{unbound[0]}'"
            )
        value = int(match.group(3)) if match.group(3) is not None else None
        if kind is ir_module.EquivalenceGuardKind.WIDTH:
            if value is None:
                raise SemanticError(
                    f"equiv guard '{text}' must compare width to an integer"
                )
        elif value is not None:
            raise SemanticError(
                f"equiv guard '{text}' does not accept an integer comparison"
            )
        return ir_module.EquivalenceGuardPredicate(kind, arguments, value)

    for declaration in declarations:
        if declaration.name in names:
            raise SemanticError(f"duplicate equiv declaration '{declaration.name}'")
        names.add(declaration.name)
        variables = vars_in(declaration.left) | vars_in(declaration.right)
        pattern = pattern_kind(declaration.left, declaration.right)
        if pattern is None or pattern[0] not in allowed:
            raise SemanticError(
                f"equiv '{declaration.name}' is not an approved pure, exact-width M26 rewrite; "
                "arithmetic identities are not accepted"
            )
        kind, bindings, operator = pattern
        if set(dict(bindings).values()) != variables:
            raise SemanticError(
                f"equiv '{declaration.name}' has a pattern constant or variable "
                "that is not bound by the approved rule shape"
            )
        guards = tuple(
            sorted(
                (
                    analyze_guard(predicate, variables)
                    for predicate in (
                        declaration.guard.predicates
                        if declaration.guard is not None
                        else ()
                    )
                ),
                key=lambda item: item.render(),
            )
        )
        result.append(
            ir_module.EquivalenceRule(
                declaration.name,
                kind,
                tuple(sorted(variables)),
                guards,
                bindings,
                operator,
            )
        )
    return result


def _constant_parameter_expression_text(
    expression: ast.Expression,
    context: _ExpressionContext,
) -> str | None:
    """Render the compile-time-only arithmetic subset for the canonical evaluator."""

    if isinstance(expression, ast.NumberExpr):
        return str(expression.value)
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.parameters:
            return str(context.parameters[expression.name])
        if expression.name in context.index_bindings:
            return str(context.index_bindings[expression.name])
        if expression.name in context.unresolved_parameters:
            raise SemanticError(
                f"unresolved compile-time parameter '{expression.name}' in ordinary expression"
            )
        return None
    if isinstance(expression, ast.UnaryExpr) and expression.operator is ast.BinaryOperator.SUBTRACT:
        operand = _constant_parameter_expression_text(expression.expression, context)
        return None if operand is None else f"(-({operand}))"
    if isinstance(expression, ast.AddExpr):
        left = _constant_parameter_expression_text(expression.left, context)
        right = _constant_parameter_expression_text(expression.right, context)
        return None if left is None or right is None else f"(({left})+({right}))"
    if isinstance(expression, ast.BinaryExpr) and expression.operator in {
        ast.BinaryOperator.SUBTRACT,
        ast.BinaryOperator.MULTIPLY,
        ast.BinaryOperator.DIVIDE,
        ast.BinaryOperator.SHIFT_LEFT,
    }:
        left = _constant_parameter_expression_text(expression.left, context)
        right = _constant_parameter_expression_text(expression.right, context)
        return (
            None if left is None or right is None
            else f"(({left}){expression.operator.value}({right}))"
        )
    if (
        isinstance(expression, ast.CallExpr)
        and not expression.specializations
        and expression.function in {
            "floor_log2", "ceil_log2", "index_width", "is_power_of_two"
        }
        and len(expression.arguments) == 1
    ):
        argument = _constant_parameter_expression_text(
            expression.arguments[0], context
        )
        return (
            None
            if argument is None
            else f"{expression.function}({argument})"
        )
    return None


def _fold_compile_time_parameter_expression(
    expression: ast.Expression,
    context: _ExpressionContext,
) -> ast.Expression:
    def contains_parameter(node: ast.Expression) -> bool:
        if isinstance(node, ast.NameExpr):
            return (
                node.name in context.parameters
                or node.name in context.index_bindings
                or node.name in context.unresolved_parameters
            )
        if isinstance(node, ast.UnaryExpr):
            return contains_parameter(node.expression)
        if isinstance(node, (ast.AddExpr, ast.BinaryExpr)):
            return contains_parameter(node.left) or contains_parameter(node.right)
        if isinstance(node, ast.CallExpr):
            return any(contains_parameter(argument) for argument in node.arguments)
        return False

    # A bare range binder already has the one uniform type derived from the
    # complete half-open range.  Replacing it with a minimum-width literal on
    # each iteration would make ``generate(i in 0..4) i`` spuriously change
    # element type at i=2.  Composite constant expressions are still folded
    # below; their ordinary typed operators retain the binder's range type.
    if (
        isinstance(expression, ast.NameExpr)
        and expression.name in context.index_bindings
    ):
        return expression

    # This path exists to substitute elaboration parameters in ordinary
    # expressions. Folding a literal-only hardware expression here would
    # erase the exact finite-width operator tree (for example ``3 + 0`` is u3,
    # not a newly inferred u2 literal) and would incorrectly turn general
    # unary minus into signed-literal syntax.
    if not contains_parameter(expression):
        return expression
    text = _constant_parameter_expression_text(expression, context)
    if text is None:
        return expression
    if context.type_resolver is None:
        raise SemanticError("compile-time parameter evaluation requires a type resolver")
    value = context.type_resolver._eval_constant_integer(
        text,
        description="ordinary expression",
        allow_zero=True,
        allow_negative=True,
    )
    if value >= 0:
        return ast.NumberExpr(value, origin=expression.origin)
    return ast.UnaryExpr(
        ast.BinaryOperator.SUBTRACT,
        ast.NumberExpr(-value, origin=expression.origin),
        origin=expression.origin,
    )


def _validate_nested_instance_array_child(
    array_name: str,
    child: ir_module.Module,
    *,
    hierarchy_cache: HierarchyTraversalCache | None = None,
) -> None:
    """Validate the bounded nested hierarchy admitted below an array element.

    Compile-time arrays are structural applications of an already typed child
    specialization.  A nested hierarchy is therefore safe only when every
    transitive component stays inside the existing closed scalar/ready-valid
    ABI: one synchronous domain, direct connections, and no protocol or
    storage semantics which would require a new parent-level scheduler.  The
    hierarchy index supplies the authoritative physical paths; no backend is
    allowed to reconstruct them from generated names.
    """

    try:
        hierarchy = build_hierarchy_index(child, cache=hierarchy_cache)
    except HierarchyError as error:
        raise SemanticError(
            f"instance array '{array_name}' has invalid nested hierarchy: {error}"
        ) from error

    root_domain = (child.clock, child.reset)
    for entry in hierarchy.entries:
        current = entry.module
        rendered_path = ".".join(entry.physical_path)
        if current.is_multi_clock:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "must share exactly one synchronous clock/reset domain; CDC "
                "children are not supported"
            )
        if current.is_sequential and (current.clock, current.reset) != root_domain:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "does not use the array element's clock/reset domain"
            )
        if current.request_responses or current.request_response_connections:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "does not support request/response hierarchy"
            )
        if (
            current.aggregate_protocol_endpoints
            or current.aggregate_protocol_connections
        ):
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "does not support aggregate protocol hierarchy"
            )
        if current.csr_blocks:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "does not support CSR state"
            )
        if current.memories or current.roms or current.fifos:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "does not support transitive storage resources"
            )
        unsupported_ports = tuple(
            port
            for port in current.ports
            if port.protocol not in {
                InterfaceProtocol.WIRE,
                InterfaceProtocol.READY_VALID,
            }
        )
        if unsupported_ports:
            raise SemanticError(
                f"instance array '{array_name}' nested child '{rendered_path}' "
                "supports only scalar wire and direct ready/valid ports"
            )
        for connection in current.connections:
            if (
                connection.buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                raise SemanticError(
                    f"instance array '{array_name}' nested child "
                    f"'{rendered_path}' requires a direct same-domain "
                    "connection without buffering, adapters, or crossings"
                )
        for connection in current.hierarchical_connections:
            if (
                connection.buffer_depth
                or connection.request_buffer_depth
                or connection.response_buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                raise SemanticError(
                    f"instance array '{array_name}' nested child "
                    f"'{rendered_path}' requires a direct same-domain "
                    "connection without buffering, adapters, or crossings"
                )


def _try_resolve_instance_array_index(
    index: int | ast.Expression,
    context: _ExpressionContext,
    *,
    array: str,
) -> int | None:
    """Resolve a compile-time instance selector, or return ``None``."""

    if isinstance(index, int):
        return index
    if isinstance(index, ast.NumberExpr):
        return index.value
    if isinstance(index, ast.NameExpr):
        if index.name in context.index_bindings:
            return context.index_bindings[index.name]
        if index.name in context.parameters:
            return context.parameters[index.name]
    text = _constant_parameter_expression_text(index, context)
    if text is not None and context.type_resolver is not None:
        return context.type_resolver._eval_constant_integer(
            text,
            description=f"instance array '{array}' index",
            allow_zero=True,
            allow_negative=True,
        )
    return None


def _resolve_instance_array_index(
    index: int | ast.Expression,
    context: _ExpressionContext,
    *,
    array: str,
) -> int:
    """Resolve an instance selector without creating a runtime hardware mux."""

    resolved = _try_resolve_instance_array_index(index, context, array=array)
    if resolved is not None:
        return resolved
    raise SemanticError(
        f"instance array '{array}' requires a compile-time index; "
        "runtime instance selection is not hardware generation"
    )


def _expand_immutable_locals(
    value: ir_expr.Expression,
    symbols: dict[str, _ValueSymbol],
    active: frozenset[str] = frozenset(),
) -> ir_expr.Expression:
    """Inline immutable locals at semantic boundaries that inspect structure."""

    if isinstance(value, ir_expr.InputRef):
        symbol = symbols.get(value.name)
        if isinstance(symbol, ir_module.LocalValue):
            if symbol.name in active:
                raise SemanticError(
                    f"cyclic immutable local '{symbol.name}' during semantic expansion"
                )
            return _expand_immutable_locals(
                symbol.expression, symbols, active | {symbol.name}
            )
        return value
    if isinstance(value, ir_expr.Switch):
        return replace(
            value,
            selector=_expand_immutable_locals(value.selector, symbols, active),
            cases=tuple(
                replace(
                    case,
                    expression=_expand_immutable_locals(
                        case.expression, symbols, active
                    ),
                )
                for case in value.cases
            ),
            default=_expand_immutable_locals(value.default, symbols, active),
        )
    if isinstance(value, ir_expr.StructConstruct):
        return replace(
            value,
            fields=tuple(
                (name, _expand_immutable_locals(item, symbols, active))
                for name, item in value.fields
            ),
        )
    if not isinstance(value, ir_expr.TracedExpression):
        return value
    updates: dict[str, object] = {}
    for item in fields(value):
        if item.name == "origin" or not item.init:
            continue
        current = getattr(value, item.name)
        if isinstance(current, ir_expr.TracedExpression):
            updates[item.name] = _expand_immutable_locals(
                current, symbols, active
            )
        elif isinstance(current, tuple):
            updates[item.name] = tuple(
                _expand_immutable_locals(element, symbols, active)
                if isinstance(element, ir_expr.TracedExpression) else element
                for element in current
            )
    return replace(value, **updates) if updates else value


def _unsigned_type_range(type_: HardwareType) -> ir_expr.ValueRange | None:
    if isinstance(type_, (UIntType, BitsType)):
        return ir_expr.ValueRange(0, (1 << type_.width) - 1, "static_type")
    return None


def _static_value_range(
    expression: ir_expr.Expression,
    refinements: dict[str, ir_expr.ValueRange] | None = None,
) -> ir_expr.ValueRange | None:
    """Compute a cheap conservative interval for an unsigned value expression."""

    if isinstance(expression, ir_expr.Constant):
        if isinstance(expression.type, (UIntType, BitsType)):
            return ir_expr.ValueRange(expression.value, expression.value, "constant")
        return None
    if isinstance(expression, (ir_expr.InputRef, ir_expr.RegisterRef, ir_expr.ParameterRef)):
        if refinements is not None and expression.name in refinements:
            return refinements[expression.name]
        return _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.Truncate):
        return (
            ir_expr.ValueRange(0, (1 << expression.type.width) - 1, "truncate")
            if isinstance(expression.type, (UIntType, BitsType)) else None
        )
    if isinstance(expression, ir_expr.Extend):
        operand = _static_value_range(expression.expression, refinements)
        return (
            ir_expr.ValueRange(operand.minimum, operand.maximum, "extend")
            if operand is not None and isinstance(expression.type, (UIntType, BitsType))
            else None
        )
    if isinstance(expression, (ir_expr.Slice, ir_expr.Concat, ir_expr.Bitcast)):
        return _unsigned_type_range(expression.type)
    if isinstance(expression, (ir_expr.Pack, ir_expr.Unpack)):
        return _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.FunctionalTableLookup):
        return expression.value_range or _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.VectorIndex):
        source = expression.expression
        # A statically selected element of an explicitly retained vector has
        # the element's own interval.  Otherwise its exact value is unknown,
        # but the canonical element type is still a sound conservative proof.
        elements = (
            source.elements
            if isinstance(source, (ir_expr.Generate, ir_expr.Map))
            else None
        )
        if elements is not None:
            minimum, maximum = ir_expr.compile_time_range(expression.index)
            selected_ranges = tuple(
                _static_value_range(elements[index], refinements)
                for index in range(minimum, maximum + 1)
            )
            if selected_ranges and all(item is not None for item in selected_ranges):
                concrete = tuple(
                    item for item in selected_ranges if item is not None
                )
                return ir_expr.ValueRange(
                    min(item.minimum for item in concrete),
                    max(item.maximum for item in concrete),
                    "constant_table",
                )
        return _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.RuntimeIndex):
        source = expression.expression
        elements = (
            source.elements
            if isinstance(source, (ir_expr.Generate, ir_expr.Map))
            else None
        )
        if elements is not None:
            ranges = tuple(
                _static_value_range(element, refinements) for element in elements
            )
            if ranges and all(item is not None for item in ranges):
                concrete = tuple(item for item in ranges if item is not None)
                return ir_expr.ValueRange(
                    min(item.minimum for item in concrete),
                    max(item.maximum for item in concrete),
                    "constant_table",
                )
        return _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.Add):
        left = _static_value_range(expression.left, refinements)
        right = _static_value_range(expression.right, refinements)
        if left is not None and right is not None and isinstance(expression.type, UIntType):
            return ir_expr.ValueRange(
                left.minimum + right.minimum,
                left.maximum + right.maximum,
                "arithmetic",
            )
        return None
    if isinstance(expression, ir_expr.Binary):
        left = _static_value_range(expression.left, refinements)
        right = _static_value_range(expression.right, refinements)
        if left is None or right is None or not isinstance(expression.type, (UIntType, BitsType)):
            return None
        if expression.operator is ir_expr.BinaryOperator.SUBTRACT:
            if left.minimum >= right.maximum:
                return ir_expr.ValueRange(
                    left.minimum - right.maximum,
                    left.maximum - right.minimum,
                    "arithmetic",
                )
            return _unsigned_type_range(expression.type)
        if expression.operator is ir_expr.BinaryOperator.MULTIPLY:
            return ir_expr.ValueRange(
                left.minimum * right.minimum,
                left.maximum * right.maximum,
                "arithmetic",
            )
        if expression.operator is ir_expr.BinaryOperator.SHIFT_RIGHT:
            if right.minimum == right.maximum:
                return ir_expr.ValueRange(
                    left.minimum >> right.minimum,
                    left.maximum >> right.minimum,
                    "arithmetic",
                )
        if expression.operator is ir_expr.BinaryOperator.SHIFT_LEFT:
            if right.minimum == right.maximum:
                shifted_maximum = left.maximum << right.minimum
                type_maximum = (1 << expression.type.width) - 1
                if shifted_maximum <= type_maximum:
                    return ir_expr.ValueRange(
                        left.minimum << right.minimum,
                        shifted_maximum,
                        "arithmetic",
                    )
        return _unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.Mux):
        true_range = _static_value_range(expression.when_true, refinements)
        false_range = _static_value_range(expression.when_false, refinements)
        if true_range is not None and false_range is not None:
            return ir_expr.ValueRange(
                min(true_range.minimum, false_range.minimum),
                max(true_range.maximum, false_range.maximum),
                "union",
            )
        return None
    if isinstance(expression, ir_expr.Switch):
        ranges = [
            *(
                _static_value_range(case.expression, refinements)
                for case in expression.cases
            ),
            _static_value_range(expression.default, refinements),
        ]
        if all(item is not None for item in ranges):
            concrete = [item for item in ranges if item is not None]
            return ir_expr.ValueRange(
                min(item.minimum for item in concrete),
                max(item.maximum for item in concrete),
                "union",
            )
    return None


def _guard_range_refinements(
    expression: ir_expr.Expression,
) -> dict[str, ir_expr.ValueRange]:
    """Extract sound unsigned intervals from a bounded rule guard.

    Only conjunction and a comparison against an exact unsigned constant are
    recognized.  Unsupported boolean structure contributes no fact; in
    particular disjunction and negation are never approximated.
    """

    def reference_name(value: ir_expr.Expression) -> str | None:
        if isinstance(
            value, (ir_expr.InputRef, ir_expr.RegisterRef, ir_expr.ParameterRef)
        ) and isinstance(value.type, (UIntType, BitsType)):
            return value.name
        return None

    def exact_constant(value: ir_expr.Expression) -> int | None:
        if isinstance(value, ir_expr.Constant) and isinstance(
            value.type, (UIntType, BitsType)
        ):
            return value.value
        return None

    def intersect(
        left: dict[str, ir_expr.ValueRange],
        right: dict[str, ir_expr.ValueRange],
    ) -> dict[str, ir_expr.ValueRange]:
        result = dict(left)
        for name, candidate in right.items():
            previous = result.get(name)
            if previous is None:
                result[name] = candidate
                continue
            minimum = max(previous.minimum, candidate.minimum)
            maximum = min(previous.maximum, candidate.maximum)
            if minimum <= maximum:
                result[name] = ir_expr.ValueRange(
                    minimum, maximum, "guard_conjunction"
                )
        return result

    if not isinstance(expression, ir_expr.Binary):
        return {}
    if expression.operator is ir_expr.BinaryOperator.BIT_AND:
        return intersect(
            _guard_range_refinements(expression.left),
            _guard_range_refinements(expression.right),
        )

    operator = expression.operator
    name = reference_name(expression.left)
    constant = exact_constant(expression.right)
    if name is None or constant is None:
        reverse = {
            ir_expr.BinaryOperator.LESS: ir_expr.BinaryOperator.GREATER,
            ir_expr.BinaryOperator.LESS_EQUAL: ir_expr.BinaryOperator.GREATER_EQUAL,
            ir_expr.BinaryOperator.GREATER: ir_expr.BinaryOperator.LESS,
            ir_expr.BinaryOperator.GREATER_EQUAL: ir_expr.BinaryOperator.LESS_EQUAL,
            ir_expr.BinaryOperator.EQUAL: ir_expr.BinaryOperator.EQUAL,
        }
        name = reference_name(expression.right)
        constant = exact_constant(expression.left)
        operator = reverse.get(operator)
    if name is None or constant is None or operator is None:
        return {}

    referenced = (
        expression.left if reference_name(expression.left) == name else expression.right
    )
    base = _unsigned_type_range(referenced.type)
    if base is None:
        return {}
    minimum, maximum = base.minimum, base.maximum
    if operator is ir_expr.BinaryOperator.LESS:
        maximum = min(maximum, constant - 1)
    elif operator is ir_expr.BinaryOperator.LESS_EQUAL:
        maximum = min(maximum, constant)
    elif operator is ir_expr.BinaryOperator.GREATER:
        minimum = max(minimum, constant + 1)
    elif operator is ir_expr.BinaryOperator.GREATER_EQUAL:
        minimum = max(minimum, constant)
    elif operator is ir_expr.BinaryOperator.EQUAL:
        minimum = max(minimum, constant)
        maximum = min(maximum, constant)
    else:
        return {}
    if minimum > maximum:
        return {}
    return {name: ir_expr.ValueRange(minimum, maximum, "rule_guard")}


def _check_expression(
    expression: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
) -> ir_expr.Expression:
    _budget_step(context)
    source_expression = expression
    if isinstance(expression, ast.CompileTimeIfExpr):
        selected = (
            expression.when_true
            if _compile_time_condition(expression.condition, inputs, context)
            else expression.when_false
        )
        if selected is None:
            raise SemanticError("compile-time if requires an else branch in an expression")
        # The branch is deliberately selected before any ordinary expression
        # typing, so an invalid non-selected specialization is never visited.
        return _check_expression(selected, inputs, expected, context)
    expression = _fold_compile_time_parameter_expression(expression, context)
    result = _check_expression_untraced(expression, inputs, expected, context)
    if context.allow_fixed_target_coercion:
        result = _coerce_fixed_target(result, expected)
    if (
        isinstance(source_expression, ast.NameExpr)
        and source_expression.name in context.parameters
        and source_expression.origin is not None
    ):
        origin = SourceOrigin(
            source_expression.origin,
            f"module parameter {source_expression.name}={context.parameters[source_expression.name]}",
            context.source_unit,
            context.source_digest,
        )
    else:
        origin = _semantic_origin(source_expression, context)
    return replace(result, origin=origin) if origin is not None else result


def _is_raw_representation_type(type_: HardwareType) -> bool:
    """Return whether a type is one of the two explicit raw-bit boundaries."""

    return isinstance(type_, BitsType) or (
        isinstance(type_, VecType) and isinstance(type_.element_type, BitType)
    )


def _make_bitcast(
    expression: ir_expr.Expression,
    target: HardwareType,
    *,
    description: str = "bitcast",
) -> ir_expr.Expression:
    """Create one exact-width non-enum representation reinterpretation."""

    try:
        source_width = ir_packing.packed_width(expression.type)
    except ir_packing.PackingError as error:
        raise SemanticError(
            f"{description} source must be recursively bit-packable and non-enum, "
            f"got {expression.type}: {error}"
        ) from error
    try:
        target_width = ir_packing.packed_width(target)
    except ir_packing.PackingError as error:
        raise SemanticError(
            f"{description} target must be recursively bit-packable and non-enum, "
            f"got {target}: {error}"
        ) from error
    if source_width != target_width:
        raise SemanticError(
            f"{description} requires equal packed widths, got "
            f"{expression.type} ({source_width}) and {target} ({target_width})"
        )
    if expression.type == target:
        return expression
    # Exact-width representation casts compose.  Retaining only the outer
    # requested target keeps canonical identity independent of redundant
    # source-level pack/unpack/bitcast spelling.
    if isinstance(expression, ir_expr.Bitcast):
        original = expression.expression
        if original.type == target:
            return original
        return ir_expr.Bitcast(original, target, origin=expression.origin)
    return ir_expr.Bitcast(expression, target, origin=expression.origin)


def _coerce_raw_target(
    expression: ir_expr.Expression,
    expected: HardwareType | None,
) -> ir_expr.Expression:
    """Apply the deliberately narrow implicit raw-boundary conversion."""

    if expected is None or expression.type == expected:
        return expression
    if not (
        _is_raw_representation_type(expression.type)
        or _is_raw_representation_type(expected)
    ):
        return expression
    try:
        return _make_bitcast(expression, expected, description="implicit raw bitcast")
    except SemanticError:
        # Preserve the ordinary exact-type diagnostic for a width/non-packable
        # mismatch; only a legal exact representation boundary is inserted.
        return expression


def _can_implicitly_bitcast_types(
    source: HardwareType,
    target: HardwareType,
) -> bool:
    if not (
        _is_raw_representation_type(source)
        or _is_raw_representation_type(target)
    ):
        return False
    try:
        return ir_packing.packed_width(source) == ir_packing.packed_width(target)
    except ir_packing.PackingError:
        return False


def _check_typed_boundary(
    expression: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType,
    context: _ExpressionContext,
) -> ir_expr.Expression:
    """Type an explicitly declared assignment/storage boundary."""

    try:
        typed = _check_expression(expression, inputs, expected, context)
    except SemanticError as original:
        # A direct compile-time collection producer may cross an otherwise
        # legal exact-width raw boundary.  Type its logical elements without
        # forcing a scalar expected type, then insert the same canonical
        # Bitcast as the explicit spelling.  Context is not propagated into
        # the producer's elements and no width/scale conversion is inferred.
        if (
            isinstance(expression, (ast.GenerateExpr, ast.MapExpr, ast.VectorLiteralExpr))
            and _is_raw_representation_type(expected)
        ):
            try:
                generated = _check_expression(expression, inputs, None, context)
                typed = _coerce_raw_target(generated, expected)
            except SemanticError:
                raise original
            if typed.type != expected:
                raise original
            return typed
        # A flat bit-vector target may use an exact-width integral literal as
        # its raw representation.  First type that literal as bits<W>; all
        # non-literal/vector-producing expressions retain normal contextual
        # typing and diagnostics.
        if not (
            isinstance(expected, VecType)
            and isinstance(expected.element_type, BitType)
            and isinstance(expression, ast.NumberExpr)
        ):
            raise
        try:
            typed = _check_expression(
                expression, inputs, BitsType(expected.width), context
            )
        except SemanticError:
            raise original
    return _coerce_raw_target(typed, expected)


def _check_integer_literal(
    value: int,
    expected: HardwareType | None,
    *,
    signed_syntax: bool,
) -> ir_expr.Constant:
    """Type one exact integral literal without host-width or wrap semantics.

    A leading source ``-`` is part of literal typing only when its operand is a
    numeric literal.  Other unary-minus expressions retain the existing typed
    arithmetic operation.  Context-free negative literals therefore receive
    the minimum exact two's-complement type, while a typed boundary may widen
    them to an exact signed integer or fixed-point destination.
    """

    if isinstance(expected, EnumType):
        raise SemanticError(
            f"numeric literal {value} cannot initialize enum '{expected.name}'; "
            "use a qualified member"
        )
    if signed_syntax and expected is not None and not isinstance(
        expected, (SIntType, FixedType)
    ):
        raise SemanticError(
            f"negative integer literal {value} cannot initialize unsigned/raw "
            f"type {expected}"
        )
    if expected is None:
        type_: HardwareType = (
            SIntType(minimum_signed_width(value))
            if signed_syntax
            else UIntType(minimum_unsigned_width(value))
        )
    else:
        type_ = expected
    raw_value = (
        value << type_.fraction
        if isinstance(type_, (FixedType, UFixedType))
        else value
    )
    if not _constant_fits(raw_value, type_):
        suggestion = (
            "; use quantize(...) for explicit overflow handling"
            if isinstance(type_, (FixedType, UFixedType))
            else ""
        )
        raise SemanticError(f"constant {value} does not fit {type_}{suggestion}")
    return ir_expr.Constant(raw_value, type_)


def _is_constant_expression(expression: ir_expr.Expression) -> bool:
    """Recognize reset constants through the shared exact constant evaluator."""

    try:
        constant_runtime_value(expression)
    except ConstantExpressionError:
        return False
    return True


def _has_runtime_value_dependency(expression: ir_expr.Expression) -> bool:
    """Conservatively detect runtime data without expanding callable bodies.

    Retained calls make expanding a large pure datapath solely to discover that
    it depends on an input both expensive and unnecessary.  A runtime argument
    already proves that a local is not a compile-time constant; its scalar
    range is likewise unknown.  Constant-only calls still take the existing
    bounded expansion path, preserving compile-time-local compatibility.
    """

    runtime_leaves = (
        ir_expr.InputRef,
        ir_expr.RegisterRef,
        ir_expr.ReadyValidRef,
        ir_expr.CreditRef,
        ir_expr.PacketRef,
        ir_expr.VirtualChannelCreditRef,
        ir_expr.RequestResponseRef,
        ir_expr.FifoRef,
        ir_expr.MemoryRef,
        ir_expr.RomRef,
        ir_expr.InstanceOutputRef,
    )
    seen: set[int] = set()

    def visit(value: object) -> bool:
        if isinstance(value, runtime_leaves):
            return True
        if isinstance(value, ir_expr.Expression):
            identity = id(value)
            if identity in seen:
                return False
            seen.add(identity)
        if isinstance(value, tuple):
            return any(visit(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return any(
                visit(getattr(value, item.name))
                for item in fields(value)
                if item.name not in {"origin", "type"}
            )
        return False

    return visit(expression)


def _local_constant_and_range(
    value: ir_expr.Expression,
    context: _ExpressionContext,
    *,
    name: str,
) -> tuple[bool, ir_expr.ValueRange | None]:
    """Classify one local while keeping obviously-runtime call graphs compact."""

    if _has_runtime_value_dependency(value):
        return False, _static_value_range(value)
    analysis = _expand_analysis_calls(
        value, context, purpose=f"local '{name}'"
    )
    return _is_constant_expression(analysis), _static_value_range(analysis)


def _coerce_fixed_target(
    expression: ir_expr.Expression,
    expected: HardwareType | None,
) -> ir_expr.Expression:
    """Apply a fixed destination's storage policy without changing its scale.

    Arithmetic remains full precision.  This conversion is therefore inserted
    only at an explicitly typed boundary and never selects a rounding rule for
    the user.
    """

    if expected is None or expression.type == expected:
        return expression
    if not isinstance(expected, (FixedType, UFixedType)):
        return expression
    if type(expression.type) is not type(expected):
        return expression
    if expression.type.fraction > expected.fraction:
        return expression
    if (
        expression.type.fraction < expected.fraction
        and expression.type.width - expression.type.fraction
        > expected.width - expected.fraction
    ):
        return expression
    overflow = _fixed_overflow_for_type(expected)
    return ir_expr.FixedConvert(
        expression,
        ir_expr.FixedRounding.TOWARD_ZERO,
        overflow,
        ir_expr.FixedConversionKind.RESCALE,
        expected,
    )


def _check_expression_untraced(
    expression: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
) -> ir_expr.Expression:
    if (
        isinstance(expression, ast.FieldExpr)
        and isinstance(expression.expression, ast.NameExpr)
        and context.type_resolver is not None
    ):
        owner = expression.expression.name
        enum_type = context.type_resolver.enum_type(owner)
        if enum_type is not None:
            try:
                code = enum_type.member_code(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"enum '{enum_type.name}' has no member '{expression.field}'"
                ) from error
            if expected is not None and expected != enum_type:
                raise SemanticError(
                    f"enum member {enum_type.name}.{expression.field} has type "
                    f"{enum_type}, expected exact {expected}"
                )
            return ir_expr.Constant(code, enum_type)
        union_type = context.type_resolver.tagged_union_type(owner)
        if union_type is not None:
            variant = union_type.variant(expression.field)
            if variant is None:
                raise SemanticError(
                    f"tagged union '{union_type.name}' has no variant "
                    f"'{expression.field}'"
                )
            if variant.fields:
                raise SemanticError(
                    f"tagged-union constructor {union_type.name}.{variant.name} "
                    "requires a named field body"
                )
            if expected is not None and expected != union_type:
                raise SemanticError(
                    f"constructor {union_type.name}.{variant.name} has type "
                    f"{union_type}, expected exact {expected}"
                )
            return ir_expr.UnionConstruct(
                variant.name,
                (),
                union_type,
                origin=expression.origin,
            )
    path_parts: list[str] = []
    cursor = expression
    while isinstance(cursor, ast.FieldExpr):
        path_parts.append(cursor.field)
        cursor = cursor.expression
    if isinstance(cursor, ast.NameExpr):
        full_path = ".".join((cursor.name, *reversed(path_parts)))
        for prefix in sorted(context.aggregate_paths, key=len, reverse=True):
            if full_path == prefix or full_path.startswith(prefix + "."):
                synthetic = context.aggregate_paths[prefix]
                remainder = full_path[len(prefix):].lstrip(".")
                rewritten: ast.Expression = ast.NameExpr(synthetic)
                for field_name in filter(None, remainder.split(".")):
                    rewritten = ast.FieldExpr(rewritten, field_name)
                return _check_expression_untraced(rewritten, inputs, expected, context)
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.union_binders:
            projection = context.union_binders[expression.name]
            if expected is not None and projection.type != expected:
                raise SemanticError(
                    f"tagged-union binder '{expression.name}' has type "
                    f"{projection.type}, expected exact {expected}"
                )
            return projection
        if expression.name in context.index_bindings:
            value = context.index_bindings[expression.name]
            type_ = expected or context.index_types.get(
                expression.name, UIntType(max(1, value.bit_length()))
            )
            if not isinstance(type_, (UIntType, BitsType)):
                raise SemanticError(
                    f"compile-time range index '{expression.name}' cannot produce {type_}"
                )
            return ir_expr.Constant(value, type_)
        if expression.name in context.parameters:
            value = context.parameters[expression.name]
            type_ = expected or UIntType(max(1, value.bit_length()))
            if not isinstance(type_, (UIntType, BitsType)):
                raise SemanticError(
                    f"compile-time parameter '{expression.name}' cannot produce {type_}"
                )
            if not _constant_fits(value, type_):
                raise SemanticError(
                    f"compile-time parameter '{expression.name}' value {value} does not fit {type_}"
                )
            return ir_expr.Constant(value, type_)
        if expression.name in context.compile_time_constants:
            value = context.compile_time_constants[expression.name]
            if expected is not None and value.type != expected:
                raise SemanticError(
                    f"compile-time constant parameter '{expression.name}' has "
                    f"type {value.type}, expected exact {expected}"
                )
            return value
        symbol = inputs.get(expression.name)
        if symbol is None:
            raise SemanticError(f"unknown input '{expression.name}'")
        if isinstance(symbol, ir_expr.Expression):
            return symbol
        if isinstance(symbol, (_FifoSymbol, _MemorySymbol, _RomSymbol)):
            raise SemanticError(
                f"storage resource '{symbol.name}' is not a value; select a field"
            )
        if isinstance(symbol, ir_module.FunctionParameter):
            return ir_expr.ParameterRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.Register):
            return ir_expr.RegisterRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.LocalValue):
            if symbol.compile_time and isinstance(symbol.expression, ir_expr.Constant):
                return ir_expr.Constant(symbol.expression.value, expected or symbol.type)
            return ir_expr.InputRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.RequestResponseInterface):
            raise SemanticError(
                f"request/response interface '{symbol.name}' is not a value; "
                "select request or response and then a protocol field"
            )
        if symbol.protocol is InterfaceProtocol.READY_VALID:
            raise SemanticError(
                f"ready/valid interface '{symbol.name}' is not a value; "
                "select payload, valid, ready, or transfer"
            )
        if symbol.protocol is InterfaceProtocol.CREDIT:
            raise SemanticError(
                f"credit interface '{symbol.name}' is not a value; select "
                "payload, send, return, transfer, or credits"
            )
        if symbol.protocol is InterfaceProtocol.PACKET:
            raise SemanticError(
                f"packet interface '{symbol.name}' is not a value; select "
                "payload, valid, ready, last, or transfer"
            )
        if symbol.protocol is InterfaceProtocol.VC_CREDIT:
            raise SemanticError(
                f"virtual-channel credit interface '{symbol.name}' is not a "
                "value; select a protocol field"
            )
        return ir_expr.InputRef(symbol.name, symbol.type)

    if isinstance(expression, ast.PatternConstantExpr):
        if expression.kind is ast.PatternConstantKind.ZERO:
            raise SemanticError(
                "zero<x> is available only in equiv declarations; "
                "use zeros<W> for an ordinary exact-width bit constant"
            )
        if context.type_resolver is None:
            raise SemanticError(
                f"{expression.kind.value}<{expression.witness}> requires a type resolver"
            )
        width = context.type_resolver._eval_width(expression.witness)
        type_ = BitsType(width)
        value = 0 if expression.kind is ast.PatternConstantKind.ZEROS else (1 << width) - 1
        if expected is not None and expected != type_ and not _can_implicitly_bitcast_types(
            type_, expected
        ):
            raise SemanticError(
                f"{expression.kind.value}<{expression.witness}> produces {type_}, "
                f"expected exact {expected}"
            )
        return ir_expr.Constant(value, type_)

    if isinstance(expression, ast.NumberExpr):
        return _check_integer_literal(
            expression.value,
            expected,
            signed_syntax=False,
        )

    if isinstance(expression, ast.CharLiteralExpr):
        type_ = UIntType(8)
        if (
            expected is not None
            and expected != type_
            and not _can_implicitly_bitcast_types(type_, expected)
        ):
            raise SemanticError(
                f"character literal has exact type {type_}, expected exact {expected}"
            )
        return ir_expr.Constant(expression.value, type_)

    if isinstance(expression, ast.StringLiteralExpr):
        if not expression.values:
            raise SemanticError(
                "empty string literal is not supported because vectors must have "
                "positive length"
            )
        type_ = VecType(len(expression.values), UIntType(8))
        if (
            expected is not None
            and expected != type_
            and not _can_implicitly_bitcast_types(type_, expected)
        ):
            raise SemanticError(
                f"string literal has exact type {type_}, expected exact {expected}"
            )
        return ir_expr.Generate(
            "vector_literal",
            0,
            len(expression.values),
            tuple(
                ir_expr.Constant(value, UIntType(8))
                for value in expression.values
            ),
            type_,
        )

    if isinstance(expression, ast.TupleLiteralExpr):
        if expected is not None and not isinstance(expected, TupleType):
            raise SemanticError(
                f"tuple literal cannot initialize non-tuple type {expected}"
            )
        if isinstance(expected, TupleType) and (
            len(expected.elements) != len(expression.elements)
        ):
            raise SemanticError(
                f"tuple literal has {len(expression.elements)} elements, expected "
                f"{len(expected.elements)} for {expected}"
            )
        typed_elements: list[ir_expr.Expression] = []
        for index, element in enumerate(expression.elements):
            component_expected = (
                expected.elements[index]
                if isinstance(expected, TupleType) else None
            )
            typed_elements.append(
                _check_typed_boundary(
                    element, inputs, component_expected, context
                )
                if component_expected is not None
                else _check_expression(element, inputs, None, context)
            )
        tuple_type = expected or TupleType(
            tuple(item.type for item in typed_elements)
        )
        return ir_expr.TupleConstruct(tuple(typed_elements), tuple_type)

    if isinstance(expression, ast.RationalExpr):
        if not isinstance(expected, (FixedType, UFixedType)):
            raise SemanticError(
                "decimal literal requires a contextual fixed-point type or quantize(...)"
            )
        scaled = expression.numerator << expected.fraction
        raw, remainder = divmod(scaled, expression.denominator)
        if remainder:
            raise SemanticError(
                f"literal {expression.numerator}/{expression.denominator} is not exactly "
                f"representable as {expected}; use quantize(..., rounding_mode)"
            )
        if not _constant_fits(raw, expected):
            raise SemanticError(
                f"literal {expression.numerator}/{expression.denominator} is outside "
                f"the range of {expected}; use quantize(...) for explicit overflow handling"
            )
        return ir_expr.Constant(raw, expected)

    if isinstance(expression, ast.TaggedUnionConstructExpr):
        if context.type_resolver is None:
            raise SemanticError("tagged-union constructor requires a type resolver")
        union_type = context.type_resolver.tagged_union_type(expression.union_name)
        if union_type is None:
            raise SemanticError(
                f"unknown tagged union '{expression.union_name}' in constructor"
            )
        variant = union_type.variant(expression.variant)
        if variant is None:
            raise SemanticError(
                f"tagged union '{union_type.name}' has no variant "
                f"'{expression.variant}'"
            )
        if expected is not None and expected != union_type:
            raise SemanticError(
                f"constructor {union_type.name}.{variant.name} has type "
                f"{union_type}, expected exact {expected}"
            )
        provided_names = tuple(item.name for item in expression.fields)
        duplicate = next(
            (name for name in provided_names if provided_names.count(name) > 1),
            None,
        )
        if duplicate is not None:
            raise SemanticError(
                f"duplicate constructor field '{duplicate}' for "
                f"{union_type.name}.{variant.name}"
            )
        expected_names = tuple(item.name for item in variant.fields)
        missing = tuple(name for name in expected_names if name not in provided_names)
        extra = tuple(name for name in provided_names if name not in expected_names)
        if missing or extra:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unknown " + ", ".join(extra))
            raise SemanticError(
                f"constructor {union_type.name}.{variant.name} fields are invalid: "
                + "; ".join(details)
            )
        syntax_by_name = {item.name: item for item in expression.fields}
        typed_fields: list[tuple[str, ir_expr.Expression]] = []
        for field_ in variant.fields:
            source = syntax_by_name[field_.name]
            value_syntax = source.expression or ast.NameExpr(field_.name)
            value = _check_typed_boundary(
                value_syntax, inputs, field_.type, context
            )
            typed_fields.append((field_.name, value))
        return ir_expr.UnionConstruct(
            variant.name,
            tuple(typed_fields),
            union_type,
            origin=expression.origin,
        )

    if isinstance(expression, ast.TaggedUnionMatchExpr):
        selector = _check_expression(expression.selector, inputs, None, context)
        if not isinstance(selector.type, TaggedUnionType):
            raise SemanticError(
                f"match selector must be a tagged union, got {selector.type}"
            )
        union_type = selector.type
        seen: set[str] = set()
        typed_branches: list[ir_expr.Expression] = []
        keys: list[int] = []
        result_type = expected
        for arm in expression.arms:
            if arm.union_name != union_type.name:
                raise SemanticError(
                    f"match arm {arm.union_name}.{arm.variant} does not belong to "
                    f"tagged union '{union_type.name}'"
                )
            variant = union_type.variant(arm.variant)
            if variant is None:
                raise SemanticError(
                    f"tagged union '{union_type.name}' has no variant '{arm.variant}'"
                )
            if arm.variant in seen:
                raise SemanticError(
                    f"duplicate match variant {union_type.name}.{arm.variant}"
                )
            seen.add(arm.variant)
            expected_binders = tuple(field.name for field in variant.fields)
            if arm.binders != expected_binders:
                raise SemanticError(
                    f"match arm {union_type.name}.{variant.name} binders must be "
                    f"{expected_binders}, got {arm.binders}"
                )
            shadow = next(
                (name for name in arm.binders if name in inputs or name in context.union_binders),
                None,
            )
            if shadow is not None:
                raise SemanticError(
                    f"tagged-union match binder '{shadow}' shadows an existing symbol"
                )
            binders = dict(context.union_binders)
            for field_ in variant.fields:
                binders[field_.name] = ir_expr.UnionField(
                    selector,
                    variant.name,
                    field_.name,
                    field_.type,
                    origin=arm.origin,
                )
            arm_context = replace(context, union_binders=binders)
            branch = (
                _check_typed_boundary(
                    arm.expression, inputs, result_type, arm_context
                )
                if result_type is not None
                else _check_expression(arm.expression, inputs, None, arm_context)
            )
            if result_type is None:
                result_type = branch.type
            elif branch.type != result_type:
                raise SemanticError(
                    f"match arm {union_type.name}.{variant.name} has type "
                    f"{branch.type}, expected exact {result_type}"
                )
            typed_branches.append(branch)
            keys.append(union_type.tag(variant.name))
        missing = tuple(
            variant.name for variant in union_type.variants
            if variant.name not in seen
        )
        if missing:
            raise SemanticError(
                f"missing tagged-union match variant(s) for '{union_type.name}': "
                + ", ".join(missing)
            )
        assert result_type is not None and typed_branches
        tag = ir_expr.UnionTag(
            selector,
            BitsType(union_type.tag_width),
            origin=expression.origin,
        )
        return ir_expr.Switch(
            tag,
            tuple(
                ir_expr.SwitchCase(key, branch)
                for key, branch in zip(keys, typed_branches, strict=True)
            ),
            typed_branches[0],
            result_type,
            origin=expression.origin,
        )

    if isinstance(expression, ast.VectorLiteralExpr):
        if not expression.elements:
            raise SemanticError(
                "empty vector literal has no element type; use a non-empty literal"
            )
        expected_element: HardwareType | None = None
        if expected is not None:
            if not isinstance(expected, VecType):
                raise SemanticError(
                    f"vector literal cannot initialize non-vector {expected}"
                )
            if expected.length != len(expression.elements):
                raise SemanticError(
                    f"vector literal has {len(expression.elements)} elements, "
                    f"expected {expected.length}"
                )
            expected_element = expected.element_type
        elements: list[ir_expr.Expression] = []
        element_type = expected_element
        for index, syntax in enumerate(expression.elements):
            element = (
                _check_typed_boundary(syntax, inputs, element_type, context)
                if element_type is not None
                else _check_expression(syntax, inputs, None, context)
            )
            if element_type is None:
                element_type = element.type
            if element.type != element_type:
                raise SemanticError(
                    f"vector literal element {index} has type {element.type}, "
                    f"expected exact {element_type}"
                )
            elements.append(element)
        assert element_type is not None
        type_ = VecType(len(elements), element_type)
        return ir_expr.Generate(
            "vector_literal", 0, len(elements), tuple(elements), type_
        )

    if isinstance(expression, ast.StructUpdateExpr):
        base = _check_expression(expression.expression, inputs, expected, context)
        if not isinstance(base.type, StructType):
            raise SemanticError(
                f"immutable 'with' update requires a nominal struct, got {base.type}"
            )
        if expected is not None and expected != base.type:
            raise SemanticError(
                f"struct update has type {base.type}, expected exact {expected}"
            )
        replacements: dict[str, ast.Expression] = {}
        for field in expression.fields:
            if field.name in replacements:
                raise SemanticError(
                    f"duplicate field '{field.name}' in struct update"
                )
            replacements[field.name] = (
                field.expression
                if field.expression is not None
                else ast.NameExpr(field.name, origin=expression.origin)
            )
        declared = {field.name: field for field in base.type.fields}
        unknown = sorted(set(replacements) - set(declared))
        if unknown:
            raise SemanticError(
                f"struct '{base.type.name}' has no field(s): {', '.join(unknown)}"
            )
        fields: list[tuple[str, ir_expr.Expression]] = []
        for field in base.type.fields:
            value = (
                _check_typed_boundary(
                    replacements[field.name], inputs, field.type, context
                )
                if field.name in replacements
                else ir_expr.FieldAccess(base, field.name, field.type)
            )
            if value.type != field.type:
                raise SemanticError(
                    f"field '{field.name}' has type {value.type}, expected {field.type}"
                )
            fields.append((field.name, value))
        return ir_expr.StructConstruct(base.type.name, tuple(fields), base.type)

    if isinstance(expression, ast.GenerateExpr):
        return _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            expected,
            context,
            ir_expr.Generate,
        )

    if isinstance(expression, ast.MapExpr):
        return _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            expected,
            context,
            ir_expr.Map,
        )

    if isinstance(expression, ast.IndexedSumExpr):
        collection = _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            None,
            context,
            ir_expr.Generate,
        )
        origin = _semantic_origin(expression, context)
        if origin is not None:
            collection = replace(collection, origin=origin)
        return _check_reduction(
            ir_expr.ReductionOperator.ADD,
            collection,
            context,
        )

    if isinstance(expression, ast.CollectionSumExpr):
        collection = _check_expression(
            expression.collection, inputs, None, context
        )
        return _check_reduction(
            ir_expr.ReductionOperator.ADD,
            collection,
            context,
        )

    if isinstance(expression, ast.ReduceExpr):
        collection = _check_expression(
            expression.collection, inputs, None, context
        )
        operator = ir_expr.ReductionOperator(expression.operator.value)
        if not isinstance(collection.type, VecType):
            if operator not in {
                ir_expr.ReductionOperator.BIT_AND,
                ir_expr.ReductionOperator.BIT_OR,
                ir_expr.ReductionOperator.BIT_XOR,
            }:
                raise SemanticError(
                    f"reduce({operator.value}, ...) requires a vector collection, "
                    f"got {collection.type}"
                )
            if not isinstance(
                collection.type, (BitType, BitsType, UIntType, SIntType)
            ):
                raise SemanticError(
                    f"scalar reduce({operator.value}, ...) requires bit, bits, "
                    f"unsigned, or signed integer input, got {collection.type}"
                )
            raw_vector = _make_bitcast(
                collection,
                VecType(collection.type.width, BitType()),
                description=f"scalar reduce({operator.value}, ...)",
            )
            return _check_reduction(operator, raw_vector, context)
        return _check_reduction(
            operator,
            collection,
            context,
        )

    if isinstance(expression, ast.DotExpr):
        return _check_dot(expression, inputs, context, expected)

    if isinstance(expression, ast.QuantizeExpr):
        return _check_quantize(expression, inputs, expected, context)

    if isinstance(expression, ast.AddExpr):
        left, right = _check_operand_pair(
            expression.left, expression.right, inputs, context
        )
        if isinstance(left.type, EnumType) or isinstance(right.type, EnumType):
            raise SemanticError(
                "arithmetic is not defined for enum values"
            )
        if isinstance(left.type, StructType) or isinstance(right.type, StructType):
            return _resolve_operator(
                "+",
                (left, right),
                context,
                call_origin=_semantic_origin(expression, context),
            )
        try:
            result_type = addition_rule(left.type, right.type).result_type
        except NumericTypeError as error:
            if error.reason is NumericTypeErrorReason.FRACTION_MISMATCH:
                raise SemanticError(
                    "fixed-point addition requires identical fractional widths; "
                    "use explicit quantize/rescale before the operator"
                ) from error
            if error.reason is NumericTypeErrorReason.FAMILY_MISMATCH:
                raise SemanticError(
                    f"cannot add operands with different type families "
                    f"{left.type} and {right.type}"
                ) from error
            raise SemanticError(f"addition is not defined for {left.type}") from error
        if isinstance(left, ir_expr.Constant) and isinstance(right, ir_expr.Constant):
            return ir_expr.Constant(left.value + right.value, result_type)
        return ir_expr.Add(left, right, result_type)

    if isinstance(expression, ast.BinaryExpr):
        return _check_binary(expression, inputs, context)

    if isinstance(expression, ast.UnaryExpr):
        if (
            expression.operator is ast.BinaryOperator.SUBTRACT
            and isinstance(expression.expression, ast.NumberExpr)
        ):
            return _check_integer_literal(
                -expression.expression.value,
                expected,
                signed_syntax=True,
            )
        operand = _check_expression(expression.expression, inputs, None, context)
        if expression.operator is ast.BinaryOperator.LOGIC_NOT:
            if not isinstance(operand.type, BitType):
                raise SemanticError(
                    f"logical not requires bit, got {operand.type}"
                )
            return _build_binary(
                ir_expr.BinaryOperator.EQUAL,
                operand,
                ir_expr.Constant(0, BitType()),
            )
        if expression.operator is ast.BinaryOperator.BIT_NOT:
            if not isinstance(
                operand.type, (BitType, BitsType, UIntType, SIntType)
            ):
                raise SemanticError(
                    f"bitwise complement requires bit, bits, unsigned, or "
                    f"signed integer input, got {operand.type}"
                )
            ones = (
                -1
                if isinstance(operand.type, SIntType)
                else (1 << operand.type.width) - 1
            )
            return _build_binary(
                ir_expr.BinaryOperator.BIT_XOR,
                operand,
                ir_expr.Constant(ones, operand.type),
            )
        if isinstance(operand.type, StructType):
            return _resolve_operator(
                "-",
                (operand,),
                context,
                call_origin=_semantic_origin(expression, context),
            )
        if not isinstance(
            operand.type, (UIntType, SIntType, FixedType, UFixedType)
        ):
            raise SemanticError(f"unary minus is not defined for {operand.type}")
        zero = ir_expr.Constant(0, operand.type)
        return _build_binary(ir_expr.BinaryOperator.SUBTRACT, zero, operand)

    if isinstance(expression, ast.DelayExpr):
        if not context.allow_delay:
            raise SemanticError("delay requires a module clock and reset")
        operand = _check_expression(expression.expression, inputs, expected, context)
        if not isinstance(
            operand.type,
            (
                BitType,
                UIntType,
                SIntType,
                BitsType,
                FixedType,
                UFixedType,
                TupleType,
            ),
        ):
            raise SemanticError(
                f"delay reset value is not defined for {operand.type}"
            )
        return ir_expr.Delay(
            expression.cycles,
            operand,
            context.allocate_delay(),
            operand.type,
        )

    if isinstance(expression, ast.ExploreExpr):
        if _contains_explore(expression.expression):
            raise SemanticError("nested explore is not supported")
        if any(
            item.value in {"pipeline", "dsp", "reduction", "adapter"}
            for item in expression.allowed
        ):
            raise SemanticError(
                "architecture-changing explore is allowed only as a complete "
                "wire-output assignment"
            )
        operand = _check_expression(expression.expression, inputs, expected, context)
        objective = expression.objective
        metric = (
            ir_expr.CostMetric.FMAX_EST
            if objective is not None and objective.metric.value == "fmax_est"
            else ir_expr.CostMetric(objective.metric.value)
            if objective is not None
            else ir_expr.CostMetric.LUT
        )
        if objective is not None and objective.direction == "maximize" and (
            metric is not ir_expr.CostMetric.FMAX_EST
        ):
            raise SemanticError("maximize currently supports only fmax_est")
        try:
            result = explore(ExplorationRequest(
                operand,
                tuple(TransformFamily(item.value) for item in expression.allowed),
                tuple(TransformFamily(item.value) for item in expression.avoided),
                constraints_from_syntax(expression.constraints),
                metric,
                formal_config=context.formal_config,
                formal_verifier=context.formal_verifier,
            ))
        except ValueError as error:
            raise SemanticError(str(error)) from error
        if context.exploration_results is not None:
            context.exploration_results.append(replace(
                result,
                site_owner=context.candidate_site_owner,
                site_kind="expression_explore",
            ))
        return result.selected.expression

    if isinstance(expression, ast.PipelineExpr):
        if expression.stages is None:
            raise SemanticError(
                "pipeline(auto) is allowed only as a complete wire-output assignment"
            )
        if expression.constraints:
            raise SemanticError(
                "fixed pipeline stages do not accept automatic constraints"
            )
        if not context.allow_delay:
            raise SemanticError("pipeline requires a module clock and reset")
        operand = _check_expression(expression.expression, inputs, expected, context)
        if not isinstance(
            operand.type,
            (
                BitType,
                UIntType,
                SIntType,
                BitsType,
                FixedType,
                UFixedType,
                TupleType,
            ),
        ):
            raise SemanticError(
                f"pipeline reset value is not defined for {operand.type}"
            )
        return ir_expr.Pipeline(
            expression.stages,
            operand,
            context.allocate_delay(),
            operand.type,
        )

    if isinstance(expression, ast.ArchitectureExpr):
        raise SemanticError(
            "architecture(auto) is allowed only as a complete wire-output assignment"
        )

    if isinstance(expression, ast.ImplementationChoiceExpr):
        if not context.allow_implementation_choice:
            raise SemanticError(
                "implementation choice is allowed only as a complete wire-output "
                "assignment"
            )
        return _check_implementation_choice(
            expression,
            inputs,
            expected,
            context,
        )

    if isinstance(expression, ast.CallExpr):
        if expression.function == "repeat":
            if len(expression.arguments) != 1:
                raise SemanticError("repeat expects one value argument")
            if len(expression.specializations) > 1:
                raise SemanticError("repeat accepts at most one positional length")
            explicit_length: int | None = None
            if expression.specializations:
                argument = expression.specializations[0]
                if argument.name is not None or not isinstance(argument.value, int):
                    raise SemanticError(
                        "repeat length must be one positional compile-time integer"
                    )
                explicit_length = argument.value
                if explicit_length <= 0:
                    raise SemanticError("repeat length must be positive")
            if expected is not None and not isinstance(expected, VecType):
                raise SemanticError(f"repeat cannot initialize non-vector {expected}")
            contextual_length = expected.length if isinstance(expected, VecType) else None
            if explicit_length is None and contextual_length is None:
                raise SemanticError(
                    "repeat requires either repeat<N>(value) or a declared vec<N,T> target"
                )
            if (
                explicit_length is not None
                and contextual_length is not None
                and explicit_length != contextual_length
            ):
                raise SemanticError(
                    f"repeat length {explicit_length} does not match target length "
                    f"{contextual_length}"
                )
            length = explicit_length if explicit_length is not None else contextual_length
            assert length is not None
            element_expected = expected.element_type if isinstance(expected, VecType) else None
            element = (
                _check_typed_boundary(
                    expression.arguments[0], inputs, element_expected, context
                )
                if element_expected is not None
                else _check_expression(expression.arguments[0], inputs, None, context)
            )
            type_ = VecType(length, element.type)
            return ir_expr.Generate(
                "repeat", 0, length, tuple(element for _ in range(length)), type_
            )
        if expression.function == "enum_encode":
            if expression.specializations:
                raise SemanticError(
                    "enum_encode does not accept specialization arguments"
                )
            if len(expression.arguments) != 1:
                raise SemanticError("enum_encode expects one argument")
            operand = _check_expression(
                expression.arguments[0], inputs, None, context
            )
            if not isinstance(operand.type, EnumType):
                raise SemanticError(
                    f"enum_encode requires a nominal enum input, got {operand.type}"
                )
            result_type = BitsType(operand.type.width)
            if expected is not None and expected != result_type:
                raise SemanticError(
                    f"enum_encode produces {result_type}, expected exact {expected}"
                )
            return ir_expr.EnumEncode(operand, result_type)
        if expression.function in {"enum_valid", "enum_decode"}:
            if len(expression.specializations) != 1:
                raise SemanticError(
                    f"{expression.function} requires one explicit enum type"
                )
            specialization = expression.specializations[0]
            if specialization.name is not None or not isinstance(
                specialization.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
            ):
                raise SemanticError(
                    f"{expression.function} requires one positional enum type"
                )
            if context.type_resolver is None:
                raise SemanticError(
                    f"{expression.function} requires a type resolver"
                )
            enum_type = context.type_resolver.resolve(specialization.value)
            if not isinstance(enum_type, EnumType):
                raise SemanticError(
                    f"{expression.function} target must be a nominal enum, got {enum_type}"
                )
            expected_arity = 1 if expression.function == "enum_valid" else 2
            if len(expression.arguments) != expected_arity:
                raise SemanticError(
                    f"{expression.function} expects {expected_arity} argument"
                    f"{'s' if expected_arity != 1 else ''}"
                )
            raw_type = BitsType(enum_type.width)
            raw = _check_expression(
                expression.arguments[0], inputs, raw_type, context
            )
            if raw.type != raw_type:
                raise SemanticError(
                    f"{expression.function}<{enum_type.name}> requires exact "
                    f"{raw_type} input, got {raw.type}"
                )
            if expression.function == "enum_valid":
                if expected is not None and expected != BitType():
                    raise SemanticError(
                        f"enum_valid produces bit, expected exact {expected}"
                    )
                return ir_expr.EnumValid(raw, enum_type, BitType())
            fallback = _check_expression(
                expression.arguments[1], inputs, enum_type, context
            )
            if fallback.type != enum_type:
                raise SemanticError(
                    f"enum_decode fallback has type {fallback.type}, expected exact {enum_type}"
                )
            if expected is not None and expected != enum_type:
                raise SemanticError(
                    f"enum_decode produces {enum_type}, expected exact {expected}"
                )
            return ir_expr.EnumDecode(raw, fallback, enum_type)
        if expression.function == "parity":
            if expression.specializations:
                raise SemanticError("parity does not accept specialization arguments")
            if len(expression.arguments) != 1:
                raise SemanticError("parity expects one argument")
            collection = _check_expression(
                expression.arguments[0], inputs, None, context
            )
            if isinstance(collection.type, VecType):
                if not isinstance(collection.type.element_type, BitType):
                    raise SemanticError(
                        f"parity vector input must be vec<N,bit>, got {collection.type}"
                    )
                return _check_reduction(
                    ir_expr.ReductionOperator.BIT_XOR, collection, context
                )
            if not isinstance(
                collection.type, (BitType, BitsType, UIntType, SIntType)
            ):
                raise SemanticError(
                    f"parity requires bit, bits, unsigned, signed, or vec<N,bit> "
                    f"input, got {collection.type}"
                )
            raw_vector = _make_bitcast(
                collection,
                VecType(collection.type.width, BitType()),
                description="parity",
            )
            return _check_reduction(
                ir_expr.ReductionOperator.BIT_XOR, raw_vector, context
            )
        if expression.function in _REAL_INTRINSICS:
            if isinstance(expected, (FixedType, UFixedType)):
                raise SemanticError(
                    f"intrinsic '{expression.function}' returns a compile-time real value; "
                    "use explicit quantize(...) for fixed-point hardware"
                )
            value = _compile_time_real_value(expression, inputs, context)
            exact = value.exact_integer()
            if exact is None:
                raise SemanticError(
                    f"intrinsic '{expression.function}' produced a non-integral compile-time real value; "
                    "use explicit quantize(...) for fixed-point hardware"
                )
            result_type: HardwareType = expected or (
                SIntType(minimum_signed_width(exact))
                if exact < 0
                else UIntType(minimum_unsigned_width(exact))
            )
            if not isinstance(result_type, (BitType, UIntType, SIntType, BitsType)):
                raise SemanticError(
                    f"intrinsic '{expression.function}' exact integer result cannot produce {result_type}"
                )
            if not _constant_fits(exact, result_type):
                raise SemanticError(
                    f"intrinsic '{expression.function}' result {exact} does not fit {result_type}"
                )
            return ir_expr.Constant(exact, result_type)
        if expression.function in {
            "length", "floor_log2", "ceil_log2", "index_width",
            "is_power_of_two",
        }:
            if len(expression.arguments) != 1:
                raise SemanticError(
                    f"intrinsic '{expression.function}' expects one argument"
                )
            value = _compile_time_integer_value(
                expression, inputs, context
            )
            result_type: HardwareType = (
                BitType()
                if expression.function == "is_power_of_two"
                else expected or UIntType(max(1, value.bit_length()))
            )
            if not isinstance(result_type, (BitType, UIntType, BitsType)):
                raise SemanticError(
                    f"intrinsic '{expression.function}' result must be integral"
                )
            if isinstance(result_type, (UIntType, BitsType)) and not _constant_fits(value, result_type):
                raise SemanticError(
                    f"intrinsic '{expression.function}' result {value} does not fit {result_type}"
                )
            return ir_expr.Constant(value, result_type)
        fixed_intrinsics = {
            "fixed_truncate_wrap": (ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP),
            "fixed_truncate_saturate": (ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.SATURATE),
            "fixed_round_even_wrap": (ir_expr.FixedRounding.NEAREST_EVEN, ir_expr.FixedOverflow.WRAP),
            "fixed_round_even_saturate": (ir_expr.FixedRounding.NEAREST_EVEN, ir_expr.FixedOverflow.SATURATE),
        }
        if expression.function == "fixed_to_raw":
            if len(expression.arguments) != 1:
                raise SemanticError("fixed_to_raw expects one argument")
            operand = _check_expression(expression.arguments[0], inputs, None, context)
            if not isinstance(operand.type, (FixedType, UFixedType)):
                raise SemanticError(f"fixed_to_raw requires fixed-point input, got {operand.type}")
            result_type = (SIntType if isinstance(operand.type, FixedType) else UIntType)(operand.type.width)
            if (
                expected is not None
                and expected != result_type
                and not _can_implicitly_bitcast_types(result_type, expected)
            ):
                raise SemanticError(f"fixed_to_raw produces {result_type}, expected {expected}")
            return ir_expr.FixedConvert(
                operand, ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP,
                ir_expr.FixedConversionKind.TO_RAW, result_type,
            )
        if expression.function == "fixed_raw":
            if len(expression.arguments) != 1 or not isinstance(expected, (FixedType, UFixedType)):
                raise SemanticError("fixed_raw requires one argument and a fixed-point assignment context")
            raw_type = SIntType(expected.width) if isinstance(expected, FixedType) else UIntType(expected.width)
            argument = expression.arguments[0]
            if (
                isinstance(argument, ast.UnaryExpr)
                and argument.operator is ast.BinaryOperator.SUBTRACT
                and isinstance(argument.expression, ast.NumberExpr)
            ):
                raise SemanticError(
                    "fixed_raw requires a non-negative integral raw bit pattern"
                )
            if isinstance(argument, ast.NumberExpr):
                if not 0 <= argument.value < (1 << expected.width):
                    raise SemanticError(
                        f"fixed_raw pattern {argument.value} does not fit {expected.width} bits"
                    )
                operand = ir_expr.Constant(_normalize(argument.value, raw_type), raw_type)
            elif isinstance(argument, ast.RationalExpr):
                raise SemanticError("fixed_raw requires an integral raw bit pattern")
            else:
                operand = _check_expression(argument, inputs, None, context)
            if operand.type != raw_type:
                raise SemanticError(f"fixed_raw for {expected} requires {raw_type}, got {operand.type}")
            return ir_expr.FixedConvert(
                operand, ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP,
                ir_expr.FixedConversionKind.FROM_RAW, expected,
            )
        if expression.function in fixed_intrinsics:
            if len(expression.arguments) != 1 or not isinstance(expected, (FixedType, UFixedType)):
                raise SemanticError(
                    f"{expression.function} requires one argument and a fixed-point assignment context"
                )
            operand = _check_expression(expression.arguments[0], inputs, None, context)
            compatible = (
                isinstance(expected, FixedType) and isinstance(operand.type, (FixedType, SIntType))
            ) or (
                isinstance(expected, UFixedType) and isinstance(operand.type, (UFixedType, UIntType))
            )
            if not compatible:
                raise SemanticError(
                    f"{expression.function} cannot convert {operand.type} to {expected}"
                )
            rounding, overflow = fixed_intrinsics[expression.function]
            return ir_expr.FixedConvert(
                operand, rounding, overflow, ir_expr.FixedConversionKind.RESCALE, expected,
            )
        call_origin = _semantic_origin(expression, context)
        signature = _lookup_function_signature(
            context,
            expression.function,
            call_origin=call_origin,
        )
        generic = context.generic_functions.get(expression.function)
        if generic is not None:
            arguments = tuple(
                _check_expression(argument, inputs, None, context)
                for argument in expression.arguments
            )
            arguments = _contextualize_generic_integer_arguments(
                generic,
                expression.arguments,
                arguments,
                expression.specializations,
                context,
            )
            return _specialize_callable(
                generic,
                arguments,
                expression.specializations,
                context,
                call_origin=_semantic_origin(expression, context),
                specialization_symbols=inputs,
            )
        static_callable = context.static_callables.get(expression.function)
        if static_callable is not None:
            if expression.specializations:
                raise SemanticError(
                    f"callable parameter '{expression.function}' is already "
                    "statically specialized"
                )
            if len(expression.arguments) != len(static_callable.parameter_types):
                raise SemanticError(
                    f"callable parameter '{expression.function}' expects "
                    f"{len(static_callable.parameter_types)} arguments, got "
                    f"{len(expression.arguments)}"
                )
            arguments = tuple(
                _check_expression(argument, inputs, expected_type, context)
                for argument, expected_type in zip(
                    expression.arguments,
                    static_callable.parameter_types,
                    strict=True,
                )
            )
            for index, (argument, expected_type) in enumerate(
                zip(arguments, static_callable.parameter_types, strict=True)
            ):
                if argument.type != expected_type:
                    raise SemanticError(
                        f"argument {index} to callable parameter "
                        f"'{expression.function}' has type {argument.type}, "
                        f"expected exact {expected_type}"
                    )
            reference = static_callable.reference
            concrete_definition = context.callable_definitions.get(
                static_callable.callee_identity
            )
            if concrete_definition is not None:
                actual_parameters = tuple(
                    parameter.type for parameter in concrete_definition.parameters
                )
                if (
                    actual_parameters != static_callable.parameter_types
                    or concrete_definition.return_type
                    != static_callable.return_type
                ):
                    raise SemanticError(
                        f"callable parameter '{expression.function}' concrete "
                        "definition does not match its exact signature"
                    )
                _record_callable_use(context, concrete_definition.callee_identity)
                result = ir_expr.Call(
                    concrete_definition.name,
                    arguments,
                    concrete_definition.return_type,
                    concrete_definition.callee_identity,
                    origin=_semantic_origin(expression, context),
                )
            elif context.generic_functions.get(reference.name) is not None:
                # A generic callable actual is fully specialized and published
                # when the binding is accepted.  Re-specializing here could
                # change identity under a child module's dependency context.
                raise SemanticError(
                    f"callable parameter '{expression.function}' has no "
                    "published concrete definition"
                )
            else:
                target_signature = _lookup_function_signature(
                    context,
                    reference.name,
                    call_origin=call_origin,
                )
                if target_signature is None:
                    raise SemanticError(
                        f"statically selected function '{reference.name}' is unavailable"
                    )
                result = ir_expr.Call(
                    reference.name,
                    arguments,
                    target_signature.return_type,
                    stable_callee_identity(
                        reference.name,
                        target_signature.parameters,
                        target_signature.return_type,
                    ),
                )
            if result.type != static_callable.return_type:
                raise SemanticError(
                    f"callable parameter '{expression.function}' returns "
                    f"{result.type}, expected exact {static_callable.return_type}"
                )
            if result.callee_identity != static_callable.callee_identity:
                raise SemanticError(
                    f"callable parameter '{expression.function}' resolved to "
                    "a different concrete callee identity"
                )
            return result
        if signature is None:
            raise SemanticError(f"unknown function '{expression.function}'")
        if len(expression.arguments) != len(signature.parameters):
            raise SemanticError(
                f"function '{expression.function}' expects "
                f"{len(signature.parameters)} arguments, got "
                f"{len(expression.arguments)}"
            )
        arguments: list[ir_expr.Expression] = []
        for argument_syntax, parameter in zip(
            expression.arguments, signature.parameters, strict=True
        ):
            argument = _check_expression(
                argument_syntax, inputs, parameter.type, context
            )
            if argument.type != parameter.type:
                raise SemanticError(
                    f"argument '{parameter.name}' to function "
                    f"'{expression.function}' has type {argument.type}, "
                    f"expected {parameter.type}"
                )
            arguments.append(argument)
        return ir_expr.Call(expression.function, tuple(arguments), signature.return_type)

    if isinstance(expression, ast.StructConstructExpr):
        # Resolve field punning before any generic/concrete struct path.  The
        # resulting NameExpr deliberately goes through the normal lexical
        # lookup below, so shorthand has exactly the same diagnostics and
        # typing as an explicit ``field = field`` initializer.
        if any(field.expression is None for field in expression.fields):
            expression = replace(
                expression,
                fields=tuple(
                    replace(
                        field,
                        expression=ast.NameExpr(field.name, origin=expression.origin),
                    )
                    if field.expression is None else field
                    for field in expression.fields
                ),
            )
        # A parameterized struct specialization is commonly known from the
        # destination type even though it is intentionally absent from
        # ``resolve_all()`` (there is no finite set of generic
        # specializations to enumerate).  Use that exact contextual type
        # before consulting the non-generic declaration table.  This keeps
        # construction backend-independent and, importantly, avoids picking
        # an unrelated first specialization of the same generic struct.
        struct = (
            expected
            if isinstance(expected, StructType)
            and (
                expected.name == expression.struct_name
                or expected.name.startswith(expression.struct_name + "<")
            )
            else None
        )
        if struct is None:
            struct = next((item for item in getattr(context, "structs", ()) if item.name == expression.struct_name), None)
        if struct is None:
            struct = next((item for item in getattr(context, "structs", ()) if item.name.startswith(expression.struct_name + "<")), None)
        if struct is None:
            declaration = next(
                (
                    item for item in context.struct_declarations
                    if item.name == expression.struct_name
                ),
                None,
            )
            if declaration is not None and declaration.parameters:
                provided = {field.name: field.expression for field in expression.fields}
                declared_fields = {field.name: field for field in declaration.fields}
                if set(provided) != set(declared_fields):
                    missing = sorted(set(declared_fields) - set(provided))
                    extra = sorted(set(provided) - set(declared_fields))
                    raise SemanticError(
                        f"struct '{declaration.name}' fields mismatch; missing={missing}, extra={extra}"
                    )
                raw_values = {
                    name: _check_expression(value, inputs, None, context)
                    for name, value in provided.items()
                }
                parameters = {item.name: item for item in declaration.parameters}
                type_bindings: dict[str, HardwareType] = {}
                value_bindings: dict[str, int] = {}
                assert context.type_resolver is not None
                for field in declaration.fields:
                    _bind_generic_type(
                        field.type_name, raw_values[field.name].type, parameters,
                        type_bindings, value_bindings, context.type_resolver,
                    )
                unresolved = [
                    item.name for item in declaration.parameters
                    if item.name not in type_bindings and item.name not in value_bindings
                ]
                if unresolved:
                    raise SemanticError(
                        f"cannot infer struct '{declaration.name}' parameters: {', '.join(unresolved)}"
                    )
                arguments = ",".join(
                    str(type_bindings[item.name]) if item.kind == "type"
                    else str(value_bindings[item.name])
                    for item in declaration.parameters
                )
                struct = context.type_resolver.resolve(
                    ast.TypeName(f"{declaration.name}<{arguments}>")
                )
        # Struct declarations are attached by analyze for this expression context.
        if struct is None:
            raise SemanticError(f"unknown struct '{expression.struct_name}'")
        provided = {field.name: field.expression for field in expression.fields}
        expected_fields = {field.name: field for field in struct.fields}
        if set(provided) != set(expected_fields):
            missing = sorted(set(expected_fields) - set(provided))
            extra = sorted(set(provided) - set(expected_fields))
            raise SemanticError(f"struct '{struct.name}' fields mismatch; missing={missing}, extra={extra}")
        values = []
        for field in struct.fields:
            value = _check_typed_boundary(
                provided[field.name], inputs, field.type, context
            )
            if value.type != field.type:
                raise SemanticError(f"field '{field.name}' has type {value.type}, expected {field.type}")
            values.append((field.name, value))
        return ir_expr.StructConstruct(struct.name, tuple(values), struct)

    if isinstance(expression, ast.FieldExpr):
        if (
            isinstance(expression.expression, ast.FieldExpr)
            and isinstance(expression.expression.expression, ast.NameExpr)
        ):
            interface_name = expression.expression.expression.name
            symbol = inputs.get(interface_name)
            if isinstance(symbol, ir_module.RequestResponseInterface):
                try:
                    channel = RequestResponseChannel(
                        expression.expression.field
                    )
                except ValueError as error:
                    raise SemanticError(
                        f"request/response interface '{symbol.name}' has no "
                        f"channel '{expression.expression.field}'"
                    ) from error
                try:
                    signal = ReadyValidSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"request/response channel '{symbol.name}.{channel.value}' "
                        f"has no field '{expression.field}'"
                    ) from error
                if signal is ReadyValidSignal.PAYLOAD:
                    type_ = (
                        symbol.request_type
                        if channel is RequestResponseChannel.REQUEST
                        else symbol.response_type
                    )
                else:
                    type_ = BitType()
                return ir_expr.RequestResponseRef(
                    symbol.name, channel, signal, type_
                )
        if isinstance(expression.expression, ast.NameExpr):
            symbol = inputs.get(expression.expression.name)
            if isinstance(symbol, _FifoSymbol):
                try:
                    signal = ir_storage.FifoSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"FIFO '{symbol.name}' has no field '{expression.field}'"
                    ) from error
                if signal in {
                    ir_storage.FifoSignal.DATA,
                    ir_storage.FifoSignal.PUSH,
                    ir_storage.FifoSignal.POP,
                }:
                    raise SemanticError(
                        f"FIFO field '{symbol.name}.{signal.value}' is write-only"
                    )
                if signal is ir_storage.FifoSignal.FRONT:
                    type_ = symbol.element_type
                elif signal is ir_storage.FifoSignal.COUNT:
                    type_ = UIntType(symbol.count_width)
                else:
                    type_ = BitType()
                return ir_expr.FifoRef(symbol.name, signal, type_)
            if isinstance(symbol, _MemorySymbol):
                try:
                    signal = ir_storage.MemorySignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"memory '{symbol.name}' has no field '{expression.field}'"
                    ) from error
                if signal is not ir_storage.MemorySignal.READ_DATA:
                    raise SemanticError(
                        f"memory field '{symbol.name}.{signal.value}' is write-only"
                    )
                return ir_expr.MemoryRef(
                    symbol.name, signal, symbol.element_type
                )
            if isinstance(symbol, _RomSymbol):
                try:
                    signal = ir_storage.RomSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"ROM '{symbol.name}' has no field '{expression.field}'"
                    ) from error
                if signal is not ir_storage.RomSignal.READ_DATA:
                    raise SemanticError(
                        f"ROM field '{symbol.name}.{signal.value}' is write-only"
                    )
                return ir_expr.RomRef(symbol.name, signal, symbol.element_type)
            if isinstance(symbol, ir_module.RequestResponseInterface):
                raise SemanticError(
                    f"request/response channel '{symbol.name}.{expression.field}' "
                    "is not a value; select payload, valid, ready, or transfer"
                )
            if isinstance(symbol, ir_module.Port):
                if symbol.protocol is InterfaceProtocol.READY_VALID:
                    try:
                        signal = ReadyValidSignal(expression.field)
                    except ValueError as error:
                        raise SemanticError(
                            f"ready/valid interface '{symbol.name}' has no field "
                            f"'{expression.field}'"
                        ) from error
                    type_ = (
                        symbol.type
                        if signal is ReadyValidSignal.PAYLOAD
                        else BitType()
                    )
                    return ir_expr.ReadyValidRef(symbol.name, signal, type_)
                if symbol.protocol is InterfaceProtocol.CREDIT:
                    try:
                        signal = CreditSignal(expression.field)
                    except ValueError as error:
                        raise SemanticError(
                            f"credit interface '{symbol.name}' has no field "
                            f"'{expression.field}'"
                        ) from error
                    if (
                        signal is CreditSignal.CREDITS
                        and symbol.direction is ir_module.PortDirection.INPUT
                    ):
                        raise SemanticError(
                            f"credit receiver interface '{symbol.name}' does not "
                            "own a sender credit count"
                        )
                    if signal is CreditSignal.PAYLOAD:
                        type_ = symbol.type
                    elif signal is CreditSignal.CREDITS:
                        if symbol.capacity is None:
                            raise SemanticError(
                                f"credit interface '{symbol.name}' has no capacity"
                            )
                        type_ = UIntType(max(1, symbol.capacity.bit_length()))
                    else:
                        type_ = BitType()
                    return ir_expr.CreditRef(symbol.name, signal, type_)
                if symbol.protocol is InterfaceProtocol.PACKET:
                    try:
                        signal = PacketSignal(expression.field)
                    except ValueError as error:
                        raise SemanticError(
                            f"packet interface '{symbol.name}' has no field "
                            f"'{expression.field}'"
                        ) from error
                    type_ = (
                        symbol.type
                        if signal is PacketSignal.PAYLOAD
                        else BitType()
                    )
                    return ir_expr.PacketRef(symbol.name, signal, type_)
                if symbol.protocol is InterfaceProtocol.VC_CREDIT:
                    try:
                        signal = VirtualChannelCreditSignal(expression.field)
                    except ValueError as error:
                        raise SemanticError(
                            f"virtual-channel credit interface '{symbol.name}' "
                            f"has no field '{expression.field}'"
                        ) from error
                    if symbol.virtual_channels is None or symbol.capacity is None:
                        raise SemanticError(
                            f"virtual-channel credit interface '{symbol.name}' "
                            "has incomplete bounds"
                        )
                    if (
                        signal is VirtualChannelCreditSignal.CREDITS
                        and symbol.direction is ir_module.PortDirection.INPUT
                    ):
                        raise SemanticError(
                            f"virtual-channel credit receiver '{symbol.name}' "
                            "does not own sender credit counts"
                        )
                    vc_type = UIntType(
                        max(1, (symbol.virtual_channels - 1).bit_length())
                    )
                    if signal is VirtualChannelCreditSignal.PAYLOAD:
                        type_ = symbol.type
                    elif signal in {
                        VirtualChannelCreditSignal.VC,
                        VirtualChannelCreditSignal.RETURN_VC,
                    }:
                        type_ = vc_type
                    elif signal is VirtualChannelCreditSignal.CREDITS:
                        type_ = VecType(
                            symbol.virtual_channels,
                            UIntType(max(1, symbol.capacity.bit_length())),
                        )
                    else:
                        type_ = BitType()
                    return ir_expr.VirtualChannelCreditRef(
                        symbol.name, signal, type_
                    )
        if (
            isinstance(expression.expression, ast.IndexExpr)
            and isinstance(expression.expression.expression, ast.NameExpr)
            and expression.expression.expression.name in context.instance_arrays
        ):
            array = expression.expression.expression.name
            length = context.instance_arrays[array]
            index_syntax = expression.expression.index
            index = _try_resolve_instance_array_index(
                index_syntax, context, array=array
            )
            if index is not None:
                if index < 0 or index >= length:
                    raise SemanticError(
                        f"instance array '{array}' index {index} is out of range "
                        f"0..{length - 1}"
                    )
                physical = f"{array}[{index}]"
                instance_type = context.instance_outputs.get(
                    (physical, expression.field)
                )
                if instance_type is None:
                    raise SemanticError(
                        f"instance '{physical}' has no output port "
                        f"'{expression.field}'"
                    )
                return ir_expr.InstanceOutputRef(
                    physical, expression.field, instance_type
                )

            physical_names = tuple(f"{array}[{item}]" for item in range(length))
            output_types = tuple(
                context.instance_outputs.get((physical, expression.field))
                for physical in physical_names
            )
            if any(type_ is None for type_ in output_types):
                raise SemanticError(
                    f"instance array '{array}' has no common output port "
                    f"'{expression.field}'"
                )
            instance_type = output_types[0]
            assert instance_type is not None
            if any(type_ != instance_type for type_ in output_types[1:]):
                raise SemanticError(
                    f"instance array '{array}' output '{expression.field}' "
                    "does not have one exact type across physical children"
                )
            protocols = tuple(
                context.instance_output_protocols.get(
                    (physical, expression.field), InterfaceProtocol.WIRE
                )
                for physical in physical_names
            )
            if any(protocol is not InterfaceProtocol.WIRE for protocol in protocols):
                raise SemanticError(
                    f"runtime instance selection supports only scalar wire outputs; "
                    f"'{array}[...].{expression.field}' is a protocol endpoint"
                )
            if not context.allow_runtime_instance_projection:
                raise SemanticError(
                    "runtime instance-array output selection is supported only "
                    "while driving a module wire output; use an explicit "
                    "generate plus runtime index for an internal value"
                )
            if not ir_packing.is_bit_packable(instance_type):
                raise SemanticError(
                    f"runtime instance selection requires a bit-packable non-enum "
                    f"wire output, got {instance_type}"
                )
            assert not isinstance(index_syntax, int)
            typed_index = _check_expression(index_syntax, inputs, None, context)
            typed_index = _expand_immutable_locals(typed_index, inputs)
            typed_index = _expand_analysis_calls(
                typed_index, context, purpose="runtime instance-array selector"
            )
            if isinstance(typed_index, ir_expr.Constant):
                # Compile-time folding can discover a constant after the
                # syntax-level resolver. Preserve the direct physical ref.
                if typed_index.value < 0 or typed_index.value >= length:
                    raise SemanticError(
                        f"instance array '{array}' index {typed_index.value} is "
                        f"out of range 0..{length - 1}"
                    )
                return ir_expr.InstanceOutputRef(
                    f"{array}[{typed_index.value}]",
                    expression.field,
                    instance_type,
                )
            if not isinstance(typed_index.type, (UIntType, BitsType)):
                raise SemanticError(
                    "runtime instance-array selector must be an unsigned integral "
                    f"expression; got {typed_index.type}"
                )
            value_range = _static_value_range(
                typed_index, context.range_refinements
            )
            if value_range is None:
                raise SemanticError(
                    "runtime instance-array selector has no statically provable "
                    f"unsigned range; got {typed_index.type}, required 0..{length - 1}"
                )
            if value_range.minimum < 0 or value_range.maximum >= length:
                raise SemanticError(
                    f"runtime instance-array selector range {value_range.minimum}.."
                    f"{value_range.maximum} is not provably within array length "
                    f"{length} (required 0..{length - 1})"
                )
            generated = ir_expr.Generate(
                "i",
                0,
                length,
                tuple(
                    ir_expr.InstanceOutputRef(
                        physical, expression.field, instance_type
                    )
                    for physical in physical_names
                ),
                VecType(length, instance_type),
            )
            return ir_expr.RuntimeIndex(
                generated,
                typed_index,
                length,
                value_range,
                instance_type,
            )
        if isinstance(expression.expression, ast.NameExpr):
            if expression.expression.name in context.instance_arrays:
                raise SemanticError(
                    f"instance array '{expression.expression.name}' requires "
                    "a compile-time index before selecting an output port"
                )
            instance_type = context.instance_outputs.get(
                (expression.expression.name, expression.field)
            )
            if instance_type is not None:
                return ir_expr.InstanceOutputRef(
                    expression.expression.name, expression.field, instance_type
                )
        aggregate = _check_expression(expression.expression, inputs, None, context)
        if not isinstance(aggregate.type, StructType):
            raise SemanticError(
                f"field access requires a struct, got {aggregate.type}"
            )
        field = aggregate.type.field(expression.field)
        if field is None:
            raise SemanticError(
                f"struct '{aggregate.type.name}' has no field '{expression.field}'"
            )
        return ir_expr.FieldAccess(aggregate, field.name, field.type)

    if isinstance(expression, ast.IndexExpr):
        collection = _check_expression(expression.expression, inputs, None, context)
        if isinstance(collection.type, BitsType):
            if isinstance(expression.index, int):
                index = expression.index
            elif (
                isinstance(expression.index, ast.NameExpr)
                and expression.index.name in context.index_bindings
            ):
                index = context.index_bindings[expression.index.name]
            else:
                typed_index = _check_expression(
                    expression.index, inputs, None, context
                )
                typed_index = _expand_immutable_locals(typed_index, inputs)
                typed_index = _expand_analysis_calls(
                    typed_index, context, purpose="packed bit index"
                )
                if not isinstance(typed_index, ir_expr.Constant):
                    raise SemanticError(
                        "packed bit indexing requires a compile-time-proven "
                        "selector; runtime packed-bit selection is not supported"
                    )
                index = typed_index.value
            if index < 0 or index >= collection.type.width:
                raise SemanticError(
                    f"packed bit index {index} is out of range for "
                    f"{collection.type} (required 0..{collection.type.width - 1})"
                )
            result_type = BitType()
            if (
                expected is not None
                and expected != result_type
                and not _can_implicitly_bitcast_types(result_type, expected)
            ):
                raise SemanticError(
                    f"packed bit index produces bit, expected exact {expected}"
                )
            selected = ir_expr.Slice(
                collection, index, index, BitsType(1)
            )
            return _make_bitcast(
                selected,
                result_type,
                description="packed bit index",
            )
        vector = collection
        if isinstance(vector.type, TupleType):
            if not isinstance(expression.index, int):
                raise SemanticError(
                    "tuple projection requires a zero-based integer literal index"
                )
            index = expression.index
            if index < 0 or index >= len(vector.type.elements):
                raise SemanticError(
                    f"tuple index {index} is out of range for {vector.type}"
                )
            return ir_expr.TupleProject(
                vector, index, vector.type.elements[index]
            )
        if not isinstance(vector.type, VecType):
            raise SemanticError(f"indexing requires a vector, got {vector.type}")
        if isinstance(expression.index, int):
            index = expression.index
        elif (
            isinstance(expression.index, ast.NameExpr)
            and expression.index.name in context.index_bindings
        ):
            index = context.index_bindings[expression.index.name]
        else:
            typed_index = _check_expression(expression.index, inputs, None, context)
            typed_index = _expand_immutable_locals(typed_index, inputs)
            typed_index = _expand_analysis_calls(
                typed_index, context, purpose="runtime vector index"
            )
            if isinstance(typed_index, ir_expr.Constant):
                index = typed_index.value
            else:
                if not isinstance(typed_index.type, (UIntType, BitsType)):
                    raise SemanticError(
                        "runtime vector index must be an unsigned integral expression; "
                        f"got {typed_index.type}"
                    )
                value_range = _static_value_range(
                    typed_index, context.range_refinements
                )
                if value_range is None:
                    raise SemanticError(
                        "runtime vector index has no statically provable unsigned range; "
                        f"got {typed_index.type}, required 0..{vector.type.length - 1}"
                    )
                if value_range.minimum < 0 or value_range.maximum >= vector.type.length:
                    raise SemanticError(
                        f"runtime index range {value_range.minimum}..{value_range.maximum} "
                        f"is not provably within vector length {vector.type.length} "
                        f"(required 0..{vector.type.length - 1}, type {typed_index.type})"
                    )
                return ir_expr.RuntimeIndex(
                    vector,
                    typed_index,
                    vector.type.length,
                    value_range,
                    vector.type.element_type,
                )
        if index < 0 or index >= vector.type.length:
            raise SemanticError(
                f"vector index {index} is out of range for {vector.type}"
            )
        return ir_expr.VectorIndex(vector, index, vector.type.element_type)

    if isinstance(expression, ast.VectorRangeExpr):
        vector = _check_expression(expression.expression, inputs, None, context)
        if not isinstance(vector.type, VecType):
            raise SemanticError(
                f"vector range requires a vector, got {vector.type}; "
                "packed scalar values use inclusive [MSB:LSB] slicing"
            )
        start = _resolve_range_bound(
            expression.start, context, "vector-range start", inputs
        )
        stop = _resolve_range_bound(
            expression.stop, context, "vector-range stop", inputs
        )
        if stop <= start:
            raise SemanticError(
                f"vector range {start}..{stop} is empty; ranges are half-open"
            )
        if start < 0 or stop > vector.type.length:
            raise SemanticError(
                f"vector range {start}..{stop} is out of range for {vector.type} "
                f"(required 0..{vector.type.length})"
            )
        result_type = VecType(stop - start, vector.type.element_type)
        if expected is not None and expected != result_type:
            raise SemanticError(
                f"vector range {start}..{stop} produces {result_type}, "
                f"expected exact {expected}"
            )
        # Use the same retained IR as an explicit vector literal containing
        # the static projections.  The concise range therefore changes only
        # spelling, not semantic/canonical/backend identity.
        return ir_expr.Generate(
            "vector_literal",
            0,
            stop - start,
            tuple(
                ir_expr.VectorIndex(
                    vector, index, vector.type.element_type
                )
                for index in range(start, stop)
            ),
            result_type,
        )

    if isinstance(expression, ast.SliceExpr):
        operand = _check_expression(expression.expression, inputs, None, context)
        if isinstance(operand.type, (EnumType, StructType, TupleType, VecType)) or not ir_packing.is_bit_packable(
            operand.type
        ):
            raise SemanticError(
                f"bit slicing requires a packed scalar value, got {operand.type}"
            )
        if context.type_resolver is None:
            raise SemanticError("bit-slice bounds require a type resolver")
        try:
            msb = context.type_resolver._eval_constant_integer(
                str(expression.msb),
                description="bit-slice MSB",
                allow_zero=True,
                allow_negative=True,
            )
        except SemanticError as error:
            if "unresolved compile-time parameter" not in str(error):
                raise
            raise SemanticError(
                f"unresolved constant '{expression.msb}' in bit-slice MSB: {error}"
            ) from error
        try:
            lsb = context.type_resolver._eval_constant_integer(
                str(expression.lsb),
                description="bit-slice LSB",
                allow_zero=True,
                allow_negative=True,
            )
        except SemanticError as error:
            if "unresolved compile-time parameter" not in str(error):
                raise
            raise SemanticError(
                f"unresolved constant '{expression.lsb}' in bit-slice LSB: {error}"
            ) from error
        if lsb < 0:
            raise SemanticError("bit-slice LSB must not be negative")
        if msb < lsb:
            raise SemanticError(
                f"bit slice [{msb}:{lsb}] is reversed; MSB must be at least LSB"
            )
        if msb >= operand.type.width:
            raise SemanticError(
                f"bit slice [{msb}:{lsb}] is out of range for {operand.type} "
                f"(width {operand.type.width})"
            )
        result_type = BitsType(ir_packing.slice_width(msb, lsb))
        if (
            expected is not None
            and expected != result_type
            and not _can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(
                f"bit slice [{msb}:{lsb}] produces {result_type}, expected exact {expected}"
            )
        return ir_expr.Slice(operand, msb, lsb, result_type)

    if isinstance(expression, ast.ConcatExpr):
        if len(expression.arguments) < 2:
            raise SemanticError("concat requires at least two operands")
        operands = tuple(
            _check_expression(argument, inputs, None, context)
            for argument in expression.arguments
        )
        vector_operands = tuple(
            operand for operand in operands if isinstance(operand.type, VecType)
        )
        if vector_operands:
            if len(vector_operands) != len(operands):
                raise SemanticError(
                    "concat cannot mix vector and non-vector operands; use "
                    "bitcast<bits<W>>(vector) for explicit raw-bit assembly"
                )
            first = vector_operands[0].type
            assert isinstance(first, VecType)
            for index, operand in enumerate(vector_operands[1:], start=2):
                assert isinstance(operand.type, VecType)
                if operand.type.element_type != first.element_type:
                    raise SemanticError(
                        "vector concat requires exact common element type; "
                        f"operand 1 has {first.element_type}, operand {index} "
                        f"has {operand.type.element_type}"
                    )
            result_type = VecType(
                sum(operand.type.length for operand in vector_operands),
                first.element_type,
            )
            if (
                expected is not None
                and expected != result_type
                and not _can_implicitly_bitcast_types(result_type, expected)
            ):
                raise SemanticError(
                    f"vector concat produces {result_type}, expected exact {expected}"
                )
            return ir_expr.VectorConcat(operands, result_type)
        widths: list[int] = []
        for index, operand in enumerate(operands):
            try:
                widths.append(ir_packing.packed_width(operand.type))
            except ir_packing.PackingError as error:
                raise SemanticError(
                    f"concat operand {index + 1} has non-bit-packable type "
                    f"{operand.type}: {error}"
                ) from error
        result_type = BitsType(sum(widths))
        if (
            expected is not None
            and expected != result_type
            and not _can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(
                f"concat produces {result_type}, expected exact {expected}",
                code="ZL-WIDTH-CONCAT",
                primary=_semantic_origin(expression, context),
                notes=tuple(
                    (
                        f"operand {index + 1}: exact type {operand.type}, "
                        f"packed width {width}"
                    )
                    for index, (operand, width) in enumerate(
                        zip(operands, widths, strict=True)
                    )
                )
                + (f"total packed width: {sum(widths)}",),
                fixes=(
                    "resize an operand explicitly before concat, or use an "
                    "equal-width bitcast only for a representation change",
                ),
            )
        return ir_expr.Concat(operands, result_type)

    if isinstance(expression, ast.BitcastExpr):
        if context.type_resolver is None:
            raise SemanticError("bitcast target requires a type resolver")
        target_type = context.type_resolver.resolve(expression.target_type)
        operand = _check_expression(expression.expression, inputs, None, context)
        result = _make_bitcast(
            operand, target_type, description=f"bitcast<{target_type}>"
        )
        if (
            expected is not None
            and expected != target_type
            and not _can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"bitcast<{target_type}> produces {target_type}, "
                f"expected exact {expected}"
            )
        return result

    if isinstance(expression, ast.ReshapeExpr):
        operand = _check_expression(expression.expression, inputs, None, context)
        if expression.target_type is None:
            target_type = expected
            if not isinstance(target_type, VecType):
                raise SemanticError(
                    "contextual reshape requires an unambiguous vector target; "
                    "use reshape<vec<...>>(value)"
                )
        else:
            if context.type_resolver is None:
                raise SemanticError("reshape target requires a type resolver")
            target_type = context.type_resolver.resolve(expression.target_type)
        if not isinstance(operand.type, VecType):
            raise SemanticError(
                f"reshape requires a vector source, got {operand.type}"
            )
        if not isinstance(target_type, VecType):
            raise SemanticError(
                f"reshape target must be a vector, got {target_type}"
            )
        source_count, source_leaf = vector_leaf_shape(operand.type)
        target_count, target_leaf = vector_leaf_shape(target_type)
        if source_count != target_count:
            raise SemanticError(
                "reshape requires equal leaf count, got "
                f"{source_count} in {operand.type} and {target_count} in {target_type}"
            )
        if source_leaf != target_leaf:
            raise SemanticError(
                "reshape requires exact common leaf type, got "
                f"{source_leaf} and {target_leaf}"
            )
        if (
            expected is not None
            and expected != target_type
            and not _can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"reshape produces {target_type}, expected exact {expected}"
            )
        if operand.type == target_type:
            return operand
        return ir_expr.Reshape(operand, target_type)

    if isinstance(expression, ast.PackExpr):
        operand = _check_expression(expression.expression, inputs, None, context)
        try:
            width = ir_packing.packed_width(operand.type)
        except ir_packing.PackingError as error:
            raise SemanticError(
                f"pack requires a recursively bit-packable non-enum value, got "
                f"{operand.type}: {error}"
            ) from error
        result_type = BitsType(width)
        if (
            expected is not None
            and expected != result_type
            and not _can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(
                f"pack produces {result_type}, expected exact {expected}"
            )
        return _make_bitcast(operand, result_type, description="pack")

    if isinstance(expression, ast.UnpackExpr):
        if context.type_resolver is None:
            raise SemanticError("unpack target requires a type resolver")
        target_type = context.type_resolver.resolve(expression.target_type)
        try:
            width = ir_packing.packed_width(target_type)
        except ir_packing.PackingError as error:
            raise SemanticError(
                f"unpack target must be recursively bit-packable and non-enum, "
                f"got {target_type}: {error}"
            ) from error
        source_type = BitsType(width)
        operand = _check_expression(
            expression.expression, inputs, source_type, context
        )
        if operand.type != source_type:
            raise SemanticError(
                f"unpack<{target_type}> requires exact {source_type} source, "
                f"got {operand.type}"
            )
        if (
            expected is not None
            and expected != target_type
            and not _can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"unpack<{target_type}> produces {target_type}, expected exact {expected}"
            )
        return _make_bitcast(
            operand, target_type, description=f"unpack<{target_type}>"
        )

    if isinstance(expression, ast.ResizeExpr):
        operand = _check_expression(expression.expression, inputs, None, context)
        width = expression.width
        contextual = width is None
        if contextual:
            if expected is None:
                raise SemanticError(
                    f"contextual {expression.kind.value}(...) requires an explicit "
                    "typed assignment, storage, argument, or return boundary"
                )
            if isinstance(expected, (EnumType, StructType, TupleType, VecType)):
                raise SemanticError(
                    f"contextual {expression.kind.value}(...) requires a scalar "
                    f"integer/fixed/raw target, got {expected}"
                )
            width = expected.width
        if isinstance(width, str):
            if width not in context.parameters:
                raise SemanticError(
                    f"resize width '{width}' is not a constant module parameter"
                )
            width = context.parameters[width]
        if isinstance(operand.type, EnumType):
            raise SemanticError(
                f"cannot resize enum '{operand.type.name}'; implicit enum "
                "resizing is forbidden"
            )
        if isinstance(operand.type, BitType):
            raise SemanticError(
                f"{expression.kind.value} is not defined for bit; use bits<1> "
                "for a resizable vector"
            )
        if expression.kind is ast.ResizeKind.EXTEND:
            if width < operand.type.width:
                raise SemanticError(
                    f"cannot extend {operand.type} to width {width}; "
                    "use truncate for a narrower result"
                )
            result_type = _resized_type(operand.type, width)
            if contextual and result_type != expected:
                raise SemanticError(
                    f"contextual extend of {operand.type} produces {result_type}, "
                    f"but the typed boundary requires exact {expected}"
                )
            if isinstance(operand, ir_expr.Constant):
                return ir_expr.Constant(operand.value, result_type)
            return ir_expr.Extend(operand, result_type)
        if width > operand.type.width:
            raise SemanticError(
                f"cannot truncate {operand.type} to width {width}; "
                "use extend for a wider result"
            )
        result_type = _resized_type(operand.type, width)
        if contextual and result_type != expected:
            raise SemanticError(
                f"contextual truncate of {operand.type} produces {result_type}, "
                f"but the typed boundary requires exact {expected}"
            )
        if isinstance(operand, ir_expr.Constant):
            return ir_expr.Constant(_normalize(operand.value, result_type), result_type)
        return ir_expr.Truncate(operand, result_type)

    if isinstance(expression, ast.MuxExpr):
        condition = _check_expression(
            expression.condition, inputs, BitType(), context
        )
        if not isinstance(condition.type, BitType):
            raise SemanticError(f"mux condition must be bit, got {condition.type}")
        branches, result_type = _check_alternatives(
            (expression.when_true, expression.when_false),
            inputs,
            expected,
            "mux branch",
            context,
        )
        if isinstance(condition, ir_expr.Constant):
            return branches[0] if condition.value else branches[1]
        return ir_expr.Mux(condition, branches[0], branches[1], result_type)

    if isinstance(expression, ast.SwitchExpr):
        selector = _check_expression(expression.selector, inputs, None, context)
        if isinstance(selector.type, EnumType):
            if expression.default is not None:
                raise SemanticError(
                    f"exhaustive enum switch on '{selector.type.name}' must not "
                    "contain an else arm"
                )
            if context.type_resolver is None:
                raise SemanticError("enum switch requires a type resolver")
            keys: set[int] = set()
            seen_members: set[str] = set()
            keyed_arms: list[tuple[int, ast.Expression]] = []
            for arm in expression.arms:
                if not isinstance(arm.key, ast.EnumMemberRef):
                    raise SemanticError(
                        f"enum switch key for '{selector.type.name}' must be "
                        "a qualified member"
                    )
                key_type = context.type_resolver.enum_type(arm.key.enum_name)
                if key_type is None:
                    raise SemanticError(
                        f"unknown enum type '{arm.key.enum_name}' in switch label"
                    )
                if key_type != selector.type:
                    raise SemanticError(
                        f"switch label {arm.key.enum_name}.{arm.key.member} has "
                        f"enum type {key_type}, expected exact {selector.type}"
                    )
                try:
                    code = key_type.member_code(arm.key.member)
                except ValueError as error:
                    raise SemanticError(
                        f"enum '{key_type.name}' has no member '{arm.key.member}'"
                    ) from error
                if arm.key.member in seen_members:
                    raise SemanticError(
                        f"duplicate enum switch member "
                        f"{key_type.name}.{arm.key.member}"
                    )
                seen_members.add(arm.key.member)
                keys.add(code)
                keyed_arms.append((code, arm.expression))
            missing = tuple(
                member for member in selector.type.members
                if member not in seen_members
            )
            if missing:
                raise SemanticError(
                    f"missing enum member in switch on '{selector.type.name}': "
                    + ", ".join(missing)
                )
            branch_syntax = tuple(branch for _, branch in keyed_arms)
            branches, result_type = _check_alternatives(
                branch_syntax, inputs, expected, "switch branch", context
            )
            if isinstance(selector, ir_expr.Constant):
                for (code, _), branch in zip(
                    keyed_arms, branches, strict=True
                ):
                    if code == selector.value:
                        return branch
                raise SemanticError(
                    f"enum constant {selector.value} is outside "
                    f"'{selector.type.name}'"
                )
            cases = tuple(
                ir_expr.SwitchCase(ordinal, branch)
                for (ordinal, _), branch in zip(
                    keyed_arms, branches, strict=True
                )
            )
            # Invalid spare bit patterns are outside the nominal enum domain.
            # Keep the existing total Switch IR/backend contract by selecting
            # a deterministic unreachable default after exact coverage proof.
            return ir_expr.Switch(selector, cases, branches[0], result_type)

        if not isinstance(selector.type, (UIntType, BitsType)):
            raise SemanticError(
                f"switch selector must be unsigned or bits, or a nominal enum; "
                f"got {selector.type}"
            )
        if expression.default is None:
            raise SemanticError("numeric switch requires an else arm")
        keys: set[int] = set()
        for arm in expression.arms:
            if not isinstance(arm.key, int):
                raise SemanticError(
                    "numeric switch requires numeric case labels"
                )
            if arm.key in keys:
                raise SemanticError(f"duplicate switch case {arm.key}")
            if arm.key >= (1 << selector.type.width):
                raise SemanticError(
                    f"switch case {arm.key} does not fit {selector.type} selector"
                )
            keys.add(arm.key)
        branch_syntax = tuple(arm.expression for arm in expression.arms) + (
            expression.default,
        )
        branches, result_type = _check_alternatives(
            branch_syntax, inputs, expected, "switch branch", context
        )
        if isinstance(selector, ir_expr.Constant):
            for arm, branch in zip(expression.arms, branches[:-1], strict=True):
                if arm.key == selector.value:
                    return branch
            return branches[-1]
        cases = tuple(
            ir_expr.SwitchCase(arm.key, branch)
            for arm, branch in zip(expression.arms, branches[:-1], strict=True)
        )
        return ir_expr.Switch(selector, cases, branches[-1], result_type)

    raise SemanticError(f"unsupported expression {expression!r}")


def _check_indexed_vector(
    index: str,
    start: int | str,
    stop: int | str,
    body: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
    constructor: type[ir_expr.Generate] | type[ir_expr.Map],
) -> ir_expr.Generate | ir_expr.Map | ir_expr.FunctionalRegion:
    start = _resolve_range_bound(start, context, "start", inputs)
    stop = _resolve_range_bound(stop, context, "stop", inputs)
    if stop <= start:
        raise SemanticError(
            f"functional range {start}..{stop} is empty; ranges are half-open"
        )
    length = stop - start
    if length > _FUNCTIONAL_RANGE_LIMIT:
        raise SemanticError(
            f"functional range {start}..{stop} expands to {length} elements; "
            f"the compile-time generation limit is {_FUNCTIONAL_RANGE_LIMIT}"
        )
    budget = context.compile_time_budget
    if budget is not None:
        budget.generated_elements += length
        if budget.generated_elements > _TOTAL_GENERATED_LIMIT:
            raise SemanticError(
                f"compile-time generation exceeds {_TOTAL_GENERATED_LIMIT} elements"
            )
    if index in inputs or index in context.index_bindings or index in context.parameters:
        raise SemanticError(f"functional range index '{index}' shadows an existing name")

    expected_element: HardwareType | None = None
    if expected is not None:
        if not isinstance(expected, VecType):
            raise SemanticError(
                f"generated vector cannot be context-sized as scalar {expected}"
            )
        if expected.length != length:
            raise SemanticError(
                f"functional range {start}..{stop} creates {length} elements, "
                f"expected {expected.length}"
            )
        expected_element = expected.element_type

    body_key = id(body)
    binder_ordinal = context.functional_binder_ordinals.get(body_key)
    if binder_ordinal is None:
        binder_ordinal = context.next_functional_binder_ordinal[0]
        context.next_functional_binder_ordinal[0] += 1
        context.functional_binder_ordinals[body_key] = binder_ordinal
    binder_nesting = (*context.functional_binder_nesting, binder_ordinal)

    elements: list[ir_expr.Expression] = []
    element_type = expected_element
    index_type = UIntType(max(1, (stop - 1).bit_length()))
    for value in range(start, stop):
        body_context = replace(
            context,
            allow_delay=False,
            allow_implementation_choice=False,
            index_bindings={**context.index_bindings, index: value},
            index_types={**context.index_types, index: index_type},
            functional_binder_nesting=binder_nesting,
        )
        element = _check_expression(body, inputs, element_type, body_context)
        if element_type is None:
            element_type = element.type
        if element.type != element_type:
            raise SemanticError(
                f"functional element at {index}={value} has type {element.type}, "
                f"expected {element_type}"
            )
        elements.append(element)
    assert element_type is not None
    type_ = VecType(length, element_type)
    if length >= _FUNCTIONAL_REGION_THRESHOLD:
        kind = (
            FunctionalRegionKind.GENERATE
            if constructor is ir_expr.Generate
            else FunctionalRegionKind.MAP
        )
        body_origin = _semantic_origin(body, context)
        if context.functional_binder_callable_identity is None:
            # Preserve the established identity of module-level functional
            # regions byte-for-byte.  Their ordinal registry remains shared
            # by the selected module analysis as before.
            binder_identity_payload = {
                "schema": "zlang-functional-binder-v2",
                "resolution_stack": context.resolution_stack,
                "semantic_nesting": context.functional_binder_nesting,
                "declaration_ordinal": binder_ordinal,
                "kind": kind.value,
                "start": start,
                "stop": stop,
            }
        else:
            binder_identity_payload = {
                "schema": "zlang-callable-functional-binder-v1",
                "callable_identity": context.functional_binder_callable_identity,
                "semantic_nesting": context.functional_binder_nesting,
                "declaration_ordinal": binder_ordinal,
                "kind": kind.value,
                "start": start,
                "stop": stop,
            }
        binder_identity = stable_digest(binder_identity_payload)
        binder = CompileTimeBinderRef(
            binder_identity,
            index,
            start,
            stop,
            body_origin,
        )
        definitions = (
            *context.function_definitions.values(),
            *context.callable_definitions.values(),
        )
        compacted = compact_functional_elements(
            kind,
            binder,
            # Compiler lowering erases immutable locals before either backend.
            # Expose their concrete typed expressions before anti-unification
            # so a compact region cannot retain a dangling local InputRef.
            # Stateful/protocol-derived locals then correctly fail the pure
            # region gate and remain an ordinary bounded Generate/Map.
            tuple(
                _expand_immutable_locals(element, inputs)
                for element in elements
            ),
            type_,
            definitions,
        )
        if compacted is not None:
            region, inlined_identities = compacted
            for identity in inlined_identities:
                _release_callable_use(context, identity)
            return region
    return constructor(index, start, stop, tuple(elements), type_)


def _check_reduction(
    operator: ir_expr.ReductionOperator,
    collection: ir_expr.Expression,
    context: _ExpressionContext,
) -> ir_expr.Reduce:
    if not isinstance(collection.type, VecType):
        raise SemanticError(
            f"reduce({operator.value}, ...) requires a vector collection, "
            f"got {collection.type}"
        )
    element_type = collection.type.element_type
    if isinstance(element_type, StructType):
        if operator is not ir_expr.ReductionOperator.ADD:
            raise SemanticError(
                "nominal aggregate reduction currently supports only additive sum"
            )
        if isinstance(collection, ir_expr.FunctionalRegion):

            def resolve_combine(
                left_type: HardwareType,
                right_type: HardwareType,
            ) -> ExactReductionCombine:
                left = ir_expr.FunctionalCaptureRef(
                    stable_digest(("exact-reduce-left", str(left_type))),
                    "exact_reduce_left",
                    left_type,
                )
                right = ir_expr.FunctionalCaptureRef(
                    stable_digest(("exact-reduce-right", str(right_type))),
                    "exact_reduce_right",
                    right_type,
                )
                try:
                    combined = _resolve_operator("+", (left, right), context)
                except SemanticError as error:
                    raise SemanticError(
                        "nominal aggregate sum cannot resolve an exact balanced-tree "
                        f"operator '+' for {left_type} and {right_type}: {error}"
                    ) from error
                if not isinstance(combined, ir_expr.Call):
                    raise SemanticError(
                        "nominal aggregate sum requires a retained typed callable "
                        f"for operator '+' on {left_type} and {right_type}"
                    )
                if combined.callee_identity is None:
                    raise SemanticError(
                        "nominal aggregate sum callable has no semantic identity"
                    )
                return ExactReductionCombine(
                    combined.type,
                    combined.function,
                    combined.callee_identity,
                )

            plan = build_exact_reduction_plan(
                element_type,
                collection.type.length,
                resolve_combine,
            )
            return ir_expr.Reduce(
                operator,
                collection,
                plan.root_type,
                plan=plan,
            )
        expanded = _balanced_nominal_addition(
            collection_elements(collection),
            context,
        )
        return ir_expr.Reduce(
            operator,
            collection,
            expanded.type,
            expanded=expanded,
        )
    else:
        try:
            type_ = reduction_result_type(
                operator,
                element_type,
                collection.type.length,
            )
        except FunctionalLoweringError as error:
            if operator is ir_expr.ReductionOperator.ADD:
                raise SemanticError(
                    "addition reduction requires one integer signedness family: "
                    f"{error}"
                ) from error
            raise SemanticError(str(error)) from error
        assert isinstance(
            type_,
            (BitType, UIntType, SIntType, BitsType, FixedType, UFixedType),
        )
    return ir_expr.Reduce(operator, collection, type_)


def _balanced_nominal_addition(
    elements: tuple[ir_expr.Expression, ...],
    context: _ExpressionContext,
) -> ir_expr.Expression:
    """Resolve the frozen source-order balanced tree through exact `operator +`."""

    if not elements:
        raise SemanticError("cannot reduce an empty nominal collection")
    if len(elements) == 1:
        return elements[0]
    middle = len(elements) // 2
    left = _balanced_nominal_addition(elements[:middle], context)
    right = _balanced_nominal_addition(elements[middle:], context)
    try:
        return _resolve_operator("+", (left, right), context)
    except SemanticError as error:
        raise SemanticError(
            "nominal aggregate sum cannot resolve an exact balanced-tree "
            f"operator '+' for {left.type} and {right.type}: {error}"
        ) from error


def _check_dot(
    expression: ast.DotExpr,
    inputs: dict[str, _ValueSymbol],
    context: _ExpressionContext,
    expected: HardwareType | None,
) -> ir_expr.Expression:
    origin = _semantic_origin(expression, context)
    left = _check_expression(expression.left, inputs, None, context)
    right = _check_expression(expression.right, inputs, None, context)
    if not isinstance(left.type, VecType) or not isinstance(right.type, VecType):
        raise SemanticError(
            f"dot requires two vectors, got {left.type} and {right.type}"
        )
    if left.type.length != right.type.length:
        raise SemanticError(
            "dot vector lengths must match, got "
            f"{left.type.length} and {right.type.length}"
        )
    products: list[ir_expr.Expression] = []
    for index in range(left.type.length):
        left_element = ir_expr.VectorIndex(
            left,
            index,
            left.type.element_type,
            origin=origin,
        )
        right_element = ir_expr.VectorIndex(
            right,
            index,
            right.type.element_type,
            origin=origin,
        )
        if isinstance(left_element.type, StructType) or isinstance(
            right_element.type, StructType
        ):
            product = _resolve_operator(
                "*", (left_element, right_element), context,
                call_origin=origin,
            )
        else:
            product = _build_binary(
                ir_expr.BinaryOperator.MULTIPLY,
                left_element,
                right_element,
            )
        products.append(replace(product, origin=origin))
    product_tuple = tuple(products)
    product_type = product_tuple[0].type
    dot = ir_expr.Dot(
        left,
        right,
        product_tuple,
        VecType(left.type.length, product_type),
        origin=origin,
    )
    reduction = _check_reduction(ir_expr.ReductionOperator.ADD, dot, context)
    if expression.rounding is None:
        return reduction
    if not isinstance(expected, (FixedType, UFixedType)):
        raise SemanticError(
            "dot(a,b,rounding) requires a contextual fixed-point target"
        )
    if type(reduction.type) is not type(expected):
        raise SemanticError(
            f"rounded dot cannot convert {reduction.type} to {expected}; "
            "signedness must match"
        )
    if expected.fraction >= reduction.type.fraction:
        raise SemanticError(
            "dot rounding is only valid when the contextual target discards "
            "fractional bits; use dot(a,b) for an exact result"
        )
    return ir_expr.FixedConvert(
        reduction,
        _fixed_rounding(expression.rounding),
        _fixed_overflow_for_type(expected),
        ir_expr.FixedConversionKind.RESCALE,
        expected,
    )


def _fixed_rounding(mode: ast.FixedRoundingMode) -> ir_expr.FixedRounding:
    return {
        ast.FixedRoundingMode.TOWARD_ZERO: ir_expr.FixedRounding.TOWARD_ZERO,
        ast.FixedRoundingMode.FLOOR: ir_expr.FixedRounding.FLOOR,
        ast.FixedRoundingMode.AWAY_ZERO: ir_expr.FixedRounding.AWAY_ZERO,
        ast.FixedRoundingMode.NEAREST_EVEN: ir_expr.FixedRounding.NEAREST_EVEN,
    }[mode]


def _fixed_overflow_for_type(
    type_: FixedType | UFixedType,
) -> ir_expr.FixedOverflow:
    return (
        ir_expr.FixedOverflow.SATURATE
        if type_.overflow is FixedOverflowPolicy.SATURATE
        else ir_expr.FixedOverflow.WRAP
    )


def _check_quantize(
    expression: ast.QuantizeExpr,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
) -> ir_expr.Expression:
    if expression.target_type is None:
        target = expected
        if not isinstance(target, (FixedType, UFixedType)):
            raise SemanticError(
                "contextual quantize requires an unambiguous fixed-point target"
            )
    else:
        if context.type_resolver is None:
            raise SemanticError("explicit quantize target cannot be resolved here")
        target = context.type_resolver.resolve(expression.target_type)
        if not isinstance(target, (FixedType, UFixedType)):
            raise SemanticError(f"quantize target must be fixed-point, got {target}")

    rounding = _fixed_rounding(expression.rounding)
    overflow = (
        ir_expr.FixedOverflow(expression.overflow.value)
        if expression.overflow is not None
        else _fixed_overflow_for_type(target)
    )

    if _contains_compile_time_real_intrinsic(expression.expression):
        try:
            real_value = _compile_time_real_value(
                expression.expression, inputs, context
            )
            cache_key = (
                real_value.identity,
                target,
                rounding.value,
                overflow.value,
            )
            cached = context.compile_time_real_quantize_cache.get(cache_key)
            if cached is not None:
                _budget_step(context, cached.logical_operations)
                raw = cached.raw
            else:
                logical_operations = 0

                def charge_quantization(operations: int) -> None:
                    nonlocal logical_operations
                    logical_operations += operations
                    _budget_step(context, operations)

                raw, precision = ct_real.quantize_to_raw(
                    real_value,
                    width=target.width,
                    fraction=target.fraction,
                    signed=isinstance(target, FixedType),
                    rounding=rounding,
                    overflow=overflow,
                    budget_step=charge_quantization,
                )
                context.compile_time_real_quantize_cache[cache_key] = (
                    _CompileTimeRealQuantization(
                        raw,
                        precision,
                        logical_operations,
                    )
                )
        except ct_real.CompileTimeRealError as error:
            raise SemanticError(str(error)) from error
        return ir_expr.Constant(raw, target)

    if isinstance(expression.expression, ast.RationalExpr):
        numerator = expression.expression.numerator
        source_type: HardwareType
        if numerator < 0:
            source_type = SIntType(minimum_signed_width(numerator))
        else:
            source_type = UIntType(minimum_unsigned_width(numerator))
        source = ir_expr.Constant(numerator, source_type)
        return ir_expr.FixedConvert(
            source,
            rounding,
            overflow,
            ir_expr.FixedConversionKind.RESCALE,
            target,
            expression.expression.denominator,
        )

    source = _check_expression(expression.expression, inputs, None, context)
    if not isinstance(
        source.type, (FixedType, UFixedType, SIntType, UIntType)
    ):
        raise SemanticError(
            f"quantize requires a fixed-point or integer source, got {source.type}"
        )
    return ir_expr.FixedConvert(
        source,
        rounding,
        overflow,
        ir_expr.FixedConversionKind.RESCALE,
        target,
    )


def _semantic_origin(
    expression: ast.Expression,
    context: _ExpressionContext | None = None,
) -> SourceOrigin | None:
    if expression.origin is None:
        return None
    if isinstance(expression, ast.NameExpr):
        construct = f"name {expression.name}"
    elif isinstance(expression, ast.NumberExpr):
        construct = f"literal {expression.value}"
    elif isinstance(expression, ast.CharLiteralExpr):
        construct = f"character literal 0x{expression.value:02x}"
    elif isinstance(expression, ast.StringLiteralExpr):
        construct = f"string literal {len(expression.values)} bytes"
    elif isinstance(expression, ast.TupleLiteralExpr):
        construct = f"tuple literal arity {len(expression.elements)}"
    elif (
        isinstance(expression, ast.UnaryExpr)
        and expression.operator is ast.BinaryOperator.SUBTRACT
        and isinstance(expression.expression, ast.NumberExpr)
    ):
        construct = f"literal {-expression.expression.value}"
    elif isinstance(expression, ast.PatternConstantExpr):
        construct = f"{expression.kind.value}<{expression.witness}>"
    elif isinstance(expression, ast.AddExpr):
        construct = "operator +"
    elif isinstance(expression, ast.BinaryExpr):
        construct = f"operator {expression.operator.value}"
    elif isinstance(expression, ast.ResizeExpr):
        construct = (
            expression.kind.value
            if expression.width is None
            else f"{expression.kind.value}<{expression.width}>"
        )
    elif isinstance(expression, ast.MuxExpr):
        construct = "mux"
    elif isinstance(expression, ast.SwitchExpr):
        construct = "switch"
    elif isinstance(expression, ast.CallExpr):
        construct = f"call {expression.function}"
    elif isinstance(expression, ast.VectorLiteralExpr):
        construct = "vector literal"
    elif isinstance(expression, ast.StructUpdateExpr):
        construct = "struct update"
    elif isinstance(expression, ast.FieldExpr):
        enum_type = (
            context.type_resolver.enum_type(expression.expression.name)
            if context is not None
            and context.type_resolver is not None
            and isinstance(expression.expression, ast.NameExpr)
            else None
        )
        construct = (
            f"enum member {enum_type.name}.{expression.field}"
            if enum_type is not None else f"field .{expression.field}"
        )
    elif isinstance(expression, ast.IndexExpr):
        if isinstance(expression.index, int):
            index_text = str(expression.index)
        elif isinstance(expression.index, ast.NameExpr):
            index_text = expression.index.name
        elif isinstance(expression.index, ast.NumberExpr):
            index_text = str(expression.index.value)
        else:
            index_text = type(expression.index).__name__
        construct = f"index [{index_text}]"
    elif isinstance(expression, ast.SliceExpr):
        construct = f"slice [{expression.msb}:{expression.lsb}]"
    elif isinstance(expression, ast.VectorRangeExpr):
        construct = f"vector range [{expression.start}..{expression.stop}]"
    elif isinstance(expression, ast.ConcatExpr):
        construct = "concat"
    elif isinstance(expression, ast.BitcastExpr):
        construct = "bitcast"
    elif isinstance(expression, ast.ReshapeExpr):
        construct = "reshape"
    elif isinstance(expression, ast.PackExpr):
        construct = "pack"
    elif isinstance(expression, ast.UnpackExpr):
        construct = f"unpack<{_render_type_syntax(expression.target_type)}>"
    elif isinstance(expression, ast.GenerateExpr):
        construct = (
            f"generate({expression.index} in "
            f"{expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.MapExpr):
        construct = (
            f"map({expression.index} in {expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.ReduceExpr):
        construct = f"reduce({expression.operator.value}, ...)"
    elif isinstance(expression, ast.IndexedSumExpr):
        construct = (
            f"sum({expression.index} in {expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.CollectionSumExpr):
        construct = "sum(collection)"
    elif isinstance(expression, ast.DotExpr):
        construct = "dot"
    elif isinstance(expression, ast.DelayExpr):
        construct = f"delay<{expression.cycles}>"
    elif isinstance(expression, ast.PipelineExpr):
        depth = "auto" if expression.stages is None else expression.stages
        construct = f"pipeline({depth})"
    elif isinstance(expression, ast.ArchitectureExpr):
        construct = "architecture(auto)"
    elif isinstance(expression, ast.ImplementationChoiceExpr):
        construct = "choice"
    elif isinstance(expression, ast.ExploreExpr):
        construct = "explore"
    else:  # pragma: no cover - the expression union is exhaustively handled.
        construct = type(expression).__name__
    return SourceOrigin(
        expression.origin,
        construct,
        context.source_unit if context is not None else None,
        context.source_digest if context is not None else None,
    )


def _contains_explore(value: object) -> bool:
    if isinstance(value, ast.ExploreExpr):
        return True
    if is_dataclass(value):
        return any(
            _contains_explore(getattr(value, item.name))
            for item in fields(value)
            if item.name != "origin"
        )
    if isinstance(value, tuple):
        return any(_contains_explore(item) for item in value)
    return False


def _check_implementation_choice(
    syntax: ast.ImplementationChoiceExpr,
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    context: _ExpressionContext,
) -> ir_expr.ImplementationChoice:
    if len(syntax.alternatives) < 2:
        raise SemanticError("implementation choice requires at least two alternatives")
    kinds = [alternative.kind for alternative in syntax.alternatives]
    if len(kinds) != len(set(kinds)):
        duplicate = next(kind for kind in kinds if kinds.count(kind) > 1)
        raise SemanticError(
            f"implementation choice repeats '{duplicate.value}' alternative"
        )
    if syntax.selected is not None and syntax.selected not in kinds:
        raise SemanticError(
            f"selected implementation '{syntax.selected.value}' has no alternative"
        )
    if syntax.cost_policy is not None:
        metrics = [constraint.metric for constraint in syntax.cost_policy.constraints]
        if len(metrics) != len(set(metrics)):
            duplicate = next(metric for metric in metrics if metrics.count(metric) > 1)
            raise SemanticError(
                f"cost policy repeats '{duplicate.value}' constraint"
            )

    alternatives: list[ir_expr.ImplementationAlternative] = []
    computations: list[ir_expr.Expression] = []
    result_type = expected
    for alternative_syntax in syntax.alternatives:
        expression = _check_expression(
            alternative_syntax.expression,
            inputs,
            result_type,
            context,
        )
        if result_type is None:
            result_type = expression.type
        if expression.type != result_type:
            raise SemanticError(
                f"implementation '{alternative_syntax.kind.value}' has type "
                f"{expression.type}, expected {result_type}"
            )
        computation, multiply, addend = _implementation_mac_shape(
            expression,
            alternative_syntax.kind.value,
        )
        _require_pure_implementation_value(
            computation,
            alternative_syntax.kind.value,
        )
        latency = _expression_latency(expression) or 0
        kind = ir_expr.ImplementationKind(alternative_syntax.kind.value)
        resource = (
            ir_expr.ImplementationResource.DSP
            if kind is ir_expr.ImplementationKind.DSP_MAC
            else ir_expr.ImplementationResource.LOGIC
        )
        applicability = ir_expr.ImplementationApplicability(
            operation="multiply_add",
            conditions=(
                "integer_scalar_operands",
                "full_precision_multiply",
                "single_addend",
                "initiation_interval_1",
            ),
            multiplier_left_type=multiply.left.type,
            multiplier_right_type=multiply.right.type,
            addend_type=addend.type,
            result_type=expression.type,
            resource_hint=resource,
        )
        semantics = ir_expr.ImplementationSemantics(
            result_type=expression.type,
            latency=latency,
            initiation_interval=1,
            protocol_events=(),
        )
        alternatives.append(
            ir_expr.ImplementationAlternative(
                kind,
                expression,
                applicability,
                semantics,
            )
        )
        computations.append(computation)

    assert result_type is not None
    if any(computation != computations[0] for computation in computations[1:]):
        raise SemanticError(
            "implementation alternatives are not mathematically equivalent; "
            "Milestone 19 requires identical typed multiply-add computations"
        )
    latencies = {
        alternative.kind.value: alternative.semantics.latency
        for alternative in alternatives
    }
    if len(set(latencies.values())) != 1:
        rendered = ", ".join(
            f"{kind}={latency}" for kind, latency in sorted(latencies.items())
        )
        raise SemanticError(
            "implementation choice latency mismatch: "
            f"{rendered}; alternatives are mathematical but not cycle-accurate "
            "equivalents",
            code="ZL-TIMING-MISMATCH",
        )
    return ir_expr.ImplementationChoice(
        (
            ir_expr.ImplementationKind(syntax.selected.value)
            if syntax.selected is not None
            else None
        ),
        tuple(alternatives),
        (
            ir_expr.ImplementationEquivalence.MATHEMATICAL,
            ir_expr.ImplementationEquivalence.CYCLE_ACCURATE,
        ),
        result_type,
        (
            ir_expr.CostPolicy(
                ir_expr.CostMetric(syntax.cost_policy.goal.value),
                tuple(
                    ir_expr.CostConstraint(
                        ir_expr.CostMetric(constraint.metric.value),
                        constraint.maximum,
                    )
                    for constraint in syntax.cost_policy.constraints
                ),
                (
                    ir_expr.SynthesisFeedback(syntax.cost_policy.feedback.value)
                    if syntax.cost_policy.feedback is not None
                    else None
                ),
            )
            if syntax.cost_policy is not None
            else None
        ),
    )


def _implementation_mac_shape(
    expression: ir_expr.Expression,
    kind: str,
) -> tuple[ir_expr.Expression, ir_expr.Binary, ir_expr.Expression]:
    computation = (
        expression.expression
        if isinstance(expression, ir_expr.Pipeline)
        else expression
    )
    if not isinstance(computation, ir_expr.Add):
        raise SemanticError(
            f"implementation '{kind}' requires one multiply-add expression"
        )
    products = tuple(
        (operand, other)
        for operand, other in (
            (computation.left, computation.right),
            (computation.right, computation.left),
        )
        if isinstance(operand, ir_expr.Binary)
        and operand.operator is ir_expr.BinaryOperator.MULTIPLY
    )
    if len(products) != 1:
        raise SemanticError(
            f"implementation '{kind}' requires exactly one full-precision "
            "multiply and one addend"
        )
    multiply, addend = products[0]
    scalar_integer = (UIntType, SIntType)
    if not all(
        isinstance(type_, scalar_integer)
        for type_ in (
            multiply.left.type,
            multiply.right.type,
            addend.type,
            computation.type,
        )
    ):
        raise SemanticError(
            f"implementation '{kind}' requires scalar integer multiply-add types"
        )
    return computation, multiply, addend


def _require_pure_implementation_value(
    expression: ir_expr.Expression,
    kind: str,
) -> None:
    if isinstance(
        expression,
        (ir_expr.InputRef, ir_expr.ParameterRef, ir_expr.Constant),
    ):
        return
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        _require_pure_implementation_value(expression.left, kind)
        _require_pure_implementation_value(expression.right, kind)
        return
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
        ),
    ):
        _require_pure_implementation_value(expression.expression, kind)
        return
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        for operand in expression.operands:
            _require_pure_implementation_value(operand, kind)
        return
    if isinstance(expression, ir_expr.RuntimeIndex):
        _require_pure_implementation_value(expression.expression, kind)
        _require_pure_implementation_value(expression.index, kind)
        return
    if isinstance(expression, ir_expr.VectorUpdate):
        _require_pure_implementation_value(expression.expression, kind)
        _require_pure_implementation_value(expression.index, kind)
        _require_pure_implementation_value(expression.value, kind)
        return
    if isinstance(expression, ir_expr.Mux):
        for operand in (
            expression.condition,
            expression.when_true,
            expression.when_false,
        ):
            _require_pure_implementation_value(operand, kind)
        return
    if isinstance(expression, ir_expr.Switch):
        _require_pure_implementation_value(expression.selector, kind)
        for case in expression.cases:
            _require_pure_implementation_value(case.expression, kind)
        _require_pure_implementation_value(expression.default, kind)
        return
    if isinstance(expression, ir_expr.Call):
        for argument in expression.arguments:
            _require_pure_implementation_value(argument, kind)
        return
    if isinstance(
        expression,
        (
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
        ),
    ):
        raise SemanticError(
            f"implementation '{kind}' depends on protocol state; protocol/"
            "observational alternatives are not supported"
        )
    if isinstance(expression, ir_expr.RegisterRef):
        raise SemanticError(
            f"implementation '{kind}' depends on sequential state; only an "
            "explicit outer pipeline is supported"
        )
    if isinstance(expression, (ir_expr.FifoRef, ir_expr.MemoryRef, ir_expr.RomRef)):
        raise SemanticError(
            f"implementation '{kind}' depends on an architectural storage value"
        )
    raise SemanticError(
        f"implementation '{kind}' contains unsupported nested timing or "
        f"architecture node {type(expression).__name__}"
    )


def _resolve_assignment_target(
    target_text: str,
    ports: dict[str, ir_module.Port],
) -> tuple[ir_module.Port, InterfaceSignal | None, HardwareType]:
    parts = target_text.split(".")
    port = ports.get(parts[0])
    if port is None:
        raise SemanticError(f"assignment target '{target_text}' is not a port")
    field = parts[1] if len(parts) == 2 else None

    if port.protocol is InterfaceProtocol.WIRE:
        if field is not None:
            raise SemanticError(
                f"wire interface '{port.name}' has no field '{field}'"
            )
        if port.direction is ir_module.PortDirection.INPUT:
            raise SemanticError(f"cannot assign to input '{target_text}'")
        return port, None, port.type

    if field is None:
        raise SemanticError(
            f"{port.protocol.value.replace('_', '/')} interface '{port.name}' "
            "must be assigned by field"
        )
    if port.protocol is InterfaceProtocol.READY_VALID:
        try:
            ready_valid_signal = ReadyValidSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"ready/valid interface '{port.name}' has no field '{field}'"
            ) from error
        if ready_valid_signal is ReadyValidSignal.TRANSFER:
            raise SemanticError(
                f"ready/valid transfer '{port.name}.transfer' is read-only"
            )
        writable: set[InterfaceSignal] = (
            {ReadyValidSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
        )
        if ready_valid_signal not in writable:
            raise SemanticError(
                f"cannot drive incoming ready/valid field '{port.name}.{field}'"
            )
        type_ = (
            port.type
            if ready_valid_signal is ReadyValidSignal.PAYLOAD
            else BitType()
        )
        return port, ready_valid_signal, type_

    if port.protocol is InterfaceProtocol.PACKET:
        try:
            packet_signal = PacketSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"packet interface '{port.name}' has no field '{field}'"
            ) from error
        if packet_signal is PacketSignal.TRANSFER:
            raise SemanticError(
                f"packet transfer '{port.name}.transfer' is read-only"
            )
        packet_writable: set[InterfaceSignal] = (
            {PacketSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {
                PacketSignal.PAYLOAD,
                PacketSignal.VALID,
                PacketSignal.LAST,
            }
        )
        if packet_signal not in packet_writable:
            raise SemanticError(
                f"cannot drive incoming packet field '{port.name}.{field}'"
            )
        type_ = (
            port.type if packet_signal is PacketSignal.PAYLOAD else BitType()
        )
        return port, packet_signal, type_

    if port.protocol is InterfaceProtocol.VC_CREDIT:
        try:
            vc_signal = VirtualChannelCreditSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"virtual-channel credit interface '{port.name}' has no field "
                f"'{field}'"
            ) from error
        if vc_signal in {
            VirtualChannelCreditSignal.TRANSFER,
            VirtualChannelCreditSignal.CREDITS,
        }:
            raise SemanticError(
                f"virtual-channel credit field '{port.name}.{field}' is read-only"
            )
        writable: set[InterfaceSignal] = (
            {
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            }
            if port.direction is ir_module.PortDirection.INPUT
            else {
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            }
        )
        if vc_signal not in writable:
            raise SemanticError(
                f"cannot drive incoming virtual-channel credit field "
                f"'{port.name}.{field}'"
            )
        if port.virtual_channels is None:
            raise SemanticError(
                f"virtual-channel credit interface '{port.name}' has no channel count"
            )
        if vc_signal is VirtualChannelCreditSignal.PAYLOAD:
            type_ = port.type
        elif vc_signal in {
            VirtualChannelCreditSignal.VC,
            VirtualChannelCreditSignal.RETURN_VC,
        }:
            type_ = UIntType(max(1, (port.virtual_channels - 1).bit_length()))
        else:
            type_ = BitType()
        return port, vc_signal, type_

    try:
        credit_signal = CreditSignal(field)
    except ValueError as error:
        raise SemanticError(
            f"credit interface '{port.name}' has no field '{field}'"
        ) from error
    if credit_signal in {CreditSignal.TRANSFER, CreditSignal.CREDITS}:
        raise SemanticError(
            f"credit field '{port.name}.{field}' is read-only"
        )
    credit_writable: set[InterfaceSignal] = (
        {CreditSignal.RETURN}
        if port.direction is ir_module.PortDirection.INPUT
        else {CreditSignal.PAYLOAD, CreditSignal.SEND}
    )
    if credit_signal not in credit_writable:
        raise SemanticError(
            f"cannot drive incoming credit field '{port.name}.{field}'"
        )
    credit_type = (
        port.type if credit_signal is CreditSignal.PAYLOAD else BitType()
    )
    return port, credit_signal, credit_type


def _direct_connection_assignments(
    connection: ir_module.Connection,
) -> tuple[ir_module.Assignment, ...]:
    source = connection.source
    destination = connection.destination
    if source.protocol is InterfaceProtocol.WIRE:
        return (
            ir_module.Assignment(
                destination,
                ir_expr.InputRef(source.name, source.type),
            ),
        )
    if source.protocol is InterfaceProtocol.READY_VALID:
        return (
            ir_module.Assignment(
                destination,
                ir_expr.ReadyValidRef(
                    source.name, ReadyValidSignal.PAYLOAD, source.type
                ),
                ReadyValidSignal.PAYLOAD,
            ),
            ir_module.Assignment(
                destination,
                ir_expr.ReadyValidRef(
                    source.name, ReadyValidSignal.VALID, BitType()
                ),
                ReadyValidSignal.VALID,
            ),
            ir_module.Assignment(
                source,
                ir_expr.ReadyValidRef(
                    destination.name, ReadyValidSignal.READY, BitType()
                ),
                ReadyValidSignal.READY,
            ),
        )
    return (
        ir_module.Assignment(
            destination,
            ir_expr.CreditRef(source.name, CreditSignal.PAYLOAD, source.type),
            CreditSignal.PAYLOAD,
        ),
        ir_module.Assignment(
            destination,
            ir_expr.CreditRef(source.name, CreditSignal.SEND, BitType()),
            CreditSignal.SEND,
        ),
        ir_module.Assignment(
            source,
            ir_expr.CreditRef(destination.name, CreditSignal.RETURN, BitType()),
            CreditSignal.RETURN,
        ),
    )


def _connection_output_keys(
    connection: ir_module.Connection,
) -> tuple[tuple[str, None, InterfaceSignal | None], ...]:
    """Return every externally visible field owned by a connection."""

    source = connection.source
    destination = connection.destination
    if source.protocol is InterfaceProtocol.WIRE:
        return ((destination.name, None, None),)
    if source.protocol is InterfaceProtocol.READY_VALID:
        source_signal: InterfaceSignal = ReadyValidSignal.READY
    else:
        source_signal = CreditSignal.RETURN
    if destination.protocol is InterfaceProtocol.READY_VALID:
        destination_signals: tuple[InterfaceSignal, ...] = (
            ReadyValidSignal.PAYLOAD,
            ReadyValidSignal.VALID,
        )
    else:
        destination_signals = (CreditSignal.PAYLOAD, CreditSignal.SEND)
    return (
        (source.name, None, source_signal),
        *((destination.name, None, signal) for signal in destination_signals),
    )


def _priority_orders(
    first: str,
    second: str,
    edges: set[tuple[str, str]],
) -> bool:
    def reaches(source: str, target: str) -> bool:
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(lower for higher, lower in edges if higher == current)
        return False

    return reaches(first, second) or reaches(second, first)


def _guards_are_provably_disjoint(
    first: ir_expr.Expression,
    second: ir_expr.Expression,
) -> bool:
    """Prove only exact register-equality guard disjointness.

    Concise FSM rules for different enum states all write the same generated
    register, but their guards cannot be true together. Treating that fact as
    an implicit priority would violate the source scheduling model. Instead,
    recognize only conjunctions containing ``register == constant`` and prove
    disjointness when the same exact typed register is constrained to different
    values. No general Boolean reasoning or source-order priority is added.
    """

    def constraints(
        expression: ir_expr.Expression,
    ) -> dict[tuple[str, HardwareType], int]:
        if (
            isinstance(expression, ir_expr.Binary)
            and expression.operator is ir_expr.BinaryOperator.BIT_AND
            and isinstance(expression.type, BitType)
        ):
            return {
                **constraints(expression.left),
                **constraints(expression.right),
            }
        if (
            isinstance(expression, ir_expr.Binary)
            and expression.operator is ir_expr.BinaryOperator.EQUAL
        ):
            if isinstance(expression.left, ir_expr.RegisterRef) and isinstance(
                expression.right, ir_expr.Constant
            ):
                return {
                    (expression.left.name, expression.left.type):
                    expression.right.value
                }
            if isinstance(expression.right, ir_expr.RegisterRef) and isinstance(
                expression.left, ir_expr.Constant
            ):
                return {
                    (expression.right.name, expression.right.type):
                    expression.left.value
                }
        return {}

    left = constraints(first)
    right = constraints(second)
    return any(
        left[key] != right[key] for key in left.keys() & right.keys()
    )


def _expression_domains(
    expression: ir_expr.Expression,
    ports: dict[str, ir_module.Port],
    registers: dict[str, ir_module.Register],
) -> set[str | None]:
    if isinstance(expression, ir_expr.InputRef):
        port = ports.get(expression.name)
        return {port.domain} if port is not None else {None}
    if isinstance(expression, ir_expr.RegisterRef):
        register = registers.get(expression.name)
        return {register.domain} if register is not None else {None}
    if isinstance(
        expression,
        (
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
        ),
    ):
        port = ports.get(expression.interface)
        return {port.domain} if port is not None else {None}
    if isinstance(
        expression,
        (
            ir_expr.ParameterRef,
            ir_expr.FunctionalCaptureRef,
            ir_expr.FunctionalTableLookup,
            ir_expr.RequestResponseRef,
            ir_expr.FifoRef,
            ir_expr.MemoryRef,
            ir_expr.RomRef,
            ir_expr.InstanceOutputRef,
            ir_expr.Constant,
        ),
    ):
        return {None}
    if isinstance(expression, (ir_expr.EnumEncode, ir_expr.EnumValid)):
        return _expression_domains(expression.expression, ports, registers)
    if isinstance(expression, (ir_expr.UnionTag, ir_expr.UnionField)):
        return _expression_domains(expression.expression, ports, registers)
    if isinstance(expression, ir_expr.EnumDecode):
        return _expression_domains(
            expression.expression, ports, registers
        ) | _expression_domains(expression.fallback, ports, registers)
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        return _expression_domains(
            expression.left, ports, registers
        ) | _expression_domains(expression.right, ports, registers)
    if isinstance(expression, ir_expr.StructConstruct):
        domains: set[str | None] = set()
        for _, value in expression.fields:
            domains |= _expression_domains(value, ports, registers)
        return domains
    if isinstance(expression, ir_expr.TupleConstruct):
        domains: set[str | None] = set()
        for value in expression.elements:
            domains |= _expression_domains(value, ports, registers)
        return domains or {None}
    if isinstance(expression, ir_expr.UnionConstruct):
        domains: set[str | None] = set()
        for _, value in expression.fields:
            domains |= _expression_domains(value, ports, registers)
        return domains or {None}
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
            ir_expr.Delay,
            ir_expr.Pipeline,
        ),
    ):
        domains = _expression_domains(expression.expression, ports, registers)
        return domains
    if isinstance(expression, (ir_expr.RuntimeIndex, ir_expr.VectorUpdate)):
        domains = _expression_domains(expression.expression, ports, registers)
        domains |= _expression_domains(expression.index, ports, registers)
        if isinstance(expression, ir_expr.VectorUpdate):
            domains |= _expression_domains(expression.value, ports, registers)
        return domains
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        domains: set[str | None] = set()
        for operand in expression.operands:
            domains |= _expression_domains(operand, ports, registers)
        return domains
    if isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
        domains: set[str | None] = set()
        for element in expression.elements:
            domains |= _expression_domains(element, ports, registers)
        return domains
    if isinstance(expression, ir_expr.FunctionalRegion):
        # Template-internal capture/table references are resolved by the
        # region. Only their retained concrete values can carry a domain.
        domains: set[str | None] = set()
        for table in expression.tables:
            for value in table.values:
                domains |= _expression_domains(value, ports, registers)
        for _, value in expression.captures:
            domains |= _expression_domains(value, ports, registers)
        return domains or {None}
    if isinstance(expression, ir_expr.Dot):
        return _expression_domains(
            expression.left, ports, registers
        ) | _expression_domains(expression.right, ports, registers)
    if isinstance(expression, ir_expr.Reduce):
        return _expression_domains(expression.collection, ports, registers)
    if isinstance(expression, ir_expr.Mux):
        return (
            _expression_domains(expression.condition, ports, registers)
            | _expression_domains(expression.when_true, ports, registers)
            | _expression_domains(expression.when_false, ports, registers)
        )
    if isinstance(expression, ir_expr.Switch):
        domains = _expression_domains(expression.selector, ports, registers)
        domains |= _expression_domains(expression.default, ports, registers)
        for case in expression.cases:
            domains |= _expression_domains(case.expression, ports, registers)
        return domains
    if isinstance(expression, ir_expr.Call):
        domains: set[str | None] = set()
        for argument in expression.arguments:
            domains |= _expression_domains(argument, ports, registers)
        return domains
    if isinstance(expression, ir_expr.ImplementationChoice):
        domains: set[str | None] = set()
        for alternative in expression.alternatives:
            domains |= _expression_domains(
                alternative.expression,
                ports,
                registers,
            )
        return domains
    raise SemanticError(f"cannot determine clock domains of {expression!r}")


def _validate_contract_expression(
    expression: ir_expr.Expression,
    ports: dict[str, ir_module.Port],
    contract_name: str,
) -> None:
    """Keep the first SVA slice observable and exactly translatable."""

    scalar_types = (BitType, UIntType, SIntType, BitsType)
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
        if expression.signal is CreditSignal.CREDITS:
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
        if expression.signal is VirtualChannelCreditSignal.CREDITS:
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
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        _validate_contract_expression(expression.left, ports, contract_name)
        _validate_contract_expression(expression.right, ports, contract_name)
        return
    if isinstance(expression, (ir_expr.Extend, ir_expr.Truncate, ir_expr.FixedConvert)):
        _validate_contract_expression(expression.expression, ports, contract_name)
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
    if isinstance(expression, ir_expr.Mux):
        _validate_contract_expression(expression.condition, ports, contract_name)
        _validate_contract_expression(expression.when_true, ports, contract_name)
        _validate_contract_expression(expression.when_false, ports, contract_name)
        return
    if isinstance(expression, ir_expr.Switch):
        _validate_contract_expression(expression.selector, ports, contract_name)
        for case in expression.cases:
            _validate_contract_expression(case.expression, ports, contract_name)
        _validate_contract_expression(expression.default, ports, contract_name)
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


def _validate_verification_expression(
    expression: ir_expr.Expression,
    ports: dict[str, ir_module.Port],
    clause_name: str,
    *,
    public_only: bool = False,
    allow_aggregate_base: bool = False,
) -> None:
    """Validate the bounded same-cycle verification predicate subset.

    This is intentionally more useful than the historical SVA-only contract
    slice: assertions may observe typed registers and already-published FIFO
    observations.  ``ensure`` uses the same expression language but remains a
    public-ABI guarantee and therefore rejects hidden state.
    """

    scalar_types = (
        BitType,
        UIntType,
        SIntType,
        BitsType,
        FixedType,
        UFixedType,
        EnumType,
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
        if (
            not isinstance(expression.type, scalar_types)
            and not allow_aggregate_base
        ):
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
        if (
            not isinstance(expression.type, scalar_types)
            and not allow_aggregate_base
        ):
            unsupported("must project an aggregate FIFO value to a scalar")
        return
    if isinstance(expression, (ir_expr.MemoryRef, ir_expr.RomRef)):
        unsupported("cannot observe memory or ROM contents in this slice")
    if isinstance(expression, ir_expr.InstanceOutputRef):
        unsupported(
            "cannot observe a child instance output without a published "
            "recursive verification binding"
        )
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        _validate_verification_expression(
            expression.left, ports, clause_name, public_only=public_only
        )
        _validate_verification_expression(
            expression.right, ports, clause_name, public_only=public_only
        )
        return
    if isinstance(expression, (ir_expr.Bitcast, ir_expr.Pack, ir_expr.Unpack)):
        _validate_verification_expression(
            expression.expression,
            ports,
            clause_name,
            public_only=public_only,
            allow_aggregate_base=True,
        )
        return
    if isinstance(expression, ir_expr.Concat):
        for operand in expression.operands:
            _validate_verification_expression(
                operand,
                ports,
                clause_name,
                public_only=public_only,
                allow_aggregate_base=True,
            )
        return
    if isinstance(expression, ir_expr.StructConstruct):
        if not allow_aggregate_base:
            unsupported("must project or bitcast a constructed struct value")
        for _, value in expression.fields:
            _validate_verification_expression(
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
        for value in expression.elements:
            _validate_verification_expression(
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
                "or use the existing M36 reference-equivalence route",
                code="ZL-VERIFY-PREDICATE",
                primary=expression.origin,
            )
        _validate_verification_expression(
            expression.expression,
            ports,
            clause_name,
            public_only=public_only,
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
        _validate_verification_expression(
            expression.expression, ports, clause_name, public_only=public_only
        )
        # EnumDecode has a fallback operand in addition to its raw expression.
        fallback = getattr(expression, "fallback", None)
        if isinstance(fallback, ir_expr.TracedExpression):
            _validate_verification_expression(
                fallback, ports, clause_name, public_only=public_only
            )
        return
    if isinstance(
        expression,
        (ir_expr.FieldAccess, ir_expr.TupleProject, ir_expr.VectorIndex),
    ):
        if (
            not isinstance(expression.type, scalar_types)
            and not allow_aggregate_base
        ):
            unsupported(
                "must continue projecting the aggregate value to a scalar"
            )
        _validate_verification_expression(
            expression.expression,
            ports,
            clause_name,
            public_only=public_only,
            allow_aggregate_base=True,
        )
        return
    if isinstance(expression, ir_expr.RuntimeIndex):
        if (
            not isinstance(expression.type, scalar_types)
            and not allow_aggregate_base
        ):
            unsupported(
                "must continue projecting the runtime-selected value to a scalar"
            )
        _validate_verification_expression(
            expression.expression,
            ports,
            clause_name,
            public_only=public_only,
            allow_aggregate_base=True,
        )
        _validate_verification_expression(
            expression.index,
            ports,
            clause_name,
            public_only=public_only,
        )
        return
    if isinstance(expression, ir_expr.Mux):
        for operand in (
            expression.condition,
            expression.when_true,
            expression.when_false,
        ):
            _validate_verification_expression(
                operand, ports, clause_name, public_only=public_only
            )
        return
    if isinstance(expression, ir_expr.Switch):
        _validate_verification_expression(
            expression.selector, ports, clause_name, public_only=public_only
        )
        for case in expression.cases:
            _validate_verification_expression(
                case.expression, ports, clause_name, public_only=public_only
            )
        _validate_verification_expression(
            expression.default, ports, clause_name, public_only=public_only
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
            ir_expr.FunctionalTableLookup,
            ir_expr.ImplementationChoice,
        ),
    ):
        unsupported(
            f"uses unsupported aggregate/functional expression "
            f"{type(expression).__name__}"
        )
    unsupported(f"uses unsupported expression {expression!r}")


def _observes_public_implementation_output(
    expression: ir_expr.Expression,
    ports: dict[str, ir_module.Port],
    request_responses: dict[str, ir_module.RequestResponseInterface],
) -> bool:
    """Return whether a public expression observes any DUT-owned leaf."""

    if isinstance(expression, ir_expr.InputRef):
        port = ports.get(expression.name)
        return bool(
            port is not None
            and port.protocol is InterfaceProtocol.WIRE
            and port.direction is ir_module.PortDirection.OUTPUT
        )
    if isinstance(expression, ir_expr.ReadyValidRef):
        port = ports[expression.interface]
        if expression.signal is ReadyValidSignal.TRANSFER:
            return True
        implementation = (
            {ReadyValidSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
        )
        return expression.signal in implementation
    if isinstance(expression, ir_expr.PacketRef):
        port = ports[expression.interface]
        if expression.signal is PacketSignal.TRANSFER:
            return True
        implementation = (
            {PacketSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {PacketSignal.PAYLOAD, PacketSignal.VALID, PacketSignal.LAST}
        )
        return expression.signal in implementation
    if isinstance(expression, ir_expr.CreditRef):
        port = ports[expression.interface]
        signal = (
            CreditSignal.SEND
            if expression.signal is CreditSignal.TRANSFER
            else expression.signal
        )
        implementation = (
            {CreditSignal.RETURN}
            if port.direction is ir_module.PortDirection.INPUT
            else {CreditSignal.PAYLOAD, CreditSignal.SEND}
        )
        return signal in implementation
    if isinstance(expression, ir_expr.VirtualChannelCreditRef):
        port = ports[expression.interface]
        signal = (
            VirtualChannelCreditSignal.SEND
            if expression.signal is VirtualChannelCreditSignal.TRANSFER
            else expression.signal
        )
        implementation = (
            {
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            }
            if port.direction is ir_module.PortDirection.INPUT
            else {
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            }
        )
        return signal in implementation
    if isinstance(expression, ir_expr.RequestResponseRef):
        interface = request_responses[expression.interface]
        if expression.signal is ReadyValidSignal.TRANSFER:
            return True
        requester_impl = {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
        }
        responder_impl = {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
        }
        implementation = (
            requester_impl
            if interface.role is RequestResponseRole.REQUESTER
            else responder_impl
        )
        return (expression.channel, expression.signal) in implementation
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        return _observes_public_implementation_output(
            expression.left, ports, request_responses
        ) or _observes_public_implementation_output(
            expression.right, ports, request_responses
        )
    if isinstance(
        expression,
        (
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
        ),
    ):
        result = _observes_public_implementation_output(
            expression.expression, ports, request_responses
        )
        fallback = getattr(expression, "fallback", None)
        return result or (
            isinstance(fallback, ir_expr.TracedExpression)
            and _observes_public_implementation_output(
                fallback, ports, request_responses
            )
        )
    if isinstance(expression, ir_expr.Mux):
        return any(
            _observes_public_implementation_output(item, ports, request_responses)
            for item in (
                expression.condition,
                expression.when_true,
                expression.when_false,
            )
        )
    if isinstance(expression, ir_expr.Switch):
        return any(
            _observes_public_implementation_output(item, ports, request_responses)
            for item in (
                expression.selector,
                *(case.expression for case in expression.cases),
                expression.default,
            )
        )
    return False


def _validate_assumption_ownership(
    expression: ir_expr.Expression,
    ports: dict[str, ir_module.Port],
    request_responses: dict[str, ir_module.RequestResponseInterface],
    contract_name: str,
) -> None:
    """Reject assumptions that can constrain implementation-owned behavior.

    An assumption is an environment contract.  Allowing it to mention a DUT
    output (including one half of a transfer event) can make an otherwise
    incorrect implementation pass vacuously.  Ownership is derived from the
    typed port direction/protocol role, never from source or RTL names.
    """

    def reject(observation: str, *, mixed: bool = False) -> None:
        detail = (
            "combines environment- and implementation-owned signals"
            if mixed else "is implementation-owned"
        )
        raise SemanticError(
            f"assumption contract '{contract_name}' cannot reference "
            f"'{observation}': the observation {detail}; assumptions may "
            "constrain environment-owned inputs only"
        )

    if isinstance(expression, ir_expr.Constant):
        return
    if isinstance(expression, ir_expr.InputRef):
        port = ports.get(expression.name)
        if (
            port is None
            or port.protocol is not InterfaceProtocol.WIRE
            or port.direction is not ir_module.PortDirection.INPUT
        ):
            reject(expression.name)
        return
    if isinstance(expression, ir_expr.ReadyValidRef):
        port = ports[expression.interface]
        if expression.signal is ReadyValidSignal.TRANSFER:
            reject(f"{expression.interface}.transfer", mixed=True)
        environment = (
            {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
            if port.direction is ir_module.PortDirection.INPUT
            else {ReadyValidSignal.READY}
        )
        if expression.signal not in environment:
            reject(f"{expression.interface}.{expression.signal.value}")
        return
    if isinstance(expression, ir_expr.PacketRef):
        port = ports[expression.interface]
        if expression.signal is PacketSignal.TRANSFER:
            reject(f"{expression.interface}.transfer", mixed=True)
        environment = (
            {PacketSignal.PAYLOAD, PacketSignal.VALID, PacketSignal.LAST}
            if port.direction is ir_module.PortDirection.INPUT
            else {PacketSignal.READY}
        )
        if expression.signal not in environment:
            reject(f"{expression.interface}.{expression.signal.value}")
        return
    if isinstance(expression, ir_expr.CreditRef):
        port = ports[expression.interface]
        signal = (
            CreditSignal.SEND
            if expression.signal is CreditSignal.TRANSFER
            else expression.signal
        )
        environment = (
            {CreditSignal.PAYLOAD, CreditSignal.SEND}
            if port.direction is ir_module.PortDirection.INPUT
            else {CreditSignal.RETURN}
        )
        if signal not in environment:
            reject(f"{expression.interface}.{expression.signal.value}")
        return
    if isinstance(expression, ir_expr.VirtualChannelCreditRef):
        port = ports[expression.interface]
        signal = (
            VirtualChannelCreditSignal.SEND
            if expression.signal is VirtualChannelCreditSignal.TRANSFER
            else expression.signal
        )
        environment = (
            {
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            }
            if port.direction is ir_module.PortDirection.INPUT
            else {
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            }
        )
        if signal not in environment:
            reject(f"{expression.interface}.{expression.signal.value}")
        return
    if isinstance(expression, ir_expr.RequestResponseRef):
        interface = request_responses[expression.interface]
        if expression.signal is ReadyValidSignal.TRANSFER:
            reject(
                f"{expression.interface}.{expression.channel.value}.transfer",
                mixed=True,
            )
        requester_environment = {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
        }
        responder_environment = {
            (RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
            (RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
            (RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
        }
        environment = (
            requester_environment
            if interface.role is RequestResponseRole.REQUESTER
            else responder_environment
        )
        if (expression.channel, expression.signal) not in environment:
            reject(
                f"{expression.interface}.{expression.channel.value}."
                f"{expression.signal.value}"
            )
        return
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        _validate_assumption_ownership(
            expression.left, ports, request_responses, contract_name
        )
        _validate_assumption_ownership(
            expression.right, ports, request_responses, contract_name
        )
        return
    if isinstance(
        expression,
        (
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
        ),
    ):
        _validate_assumption_ownership(
            expression.expression, ports, request_responses, contract_name
        )
        fallback = getattr(expression, "fallback", None)
        if isinstance(fallback, ir_expr.TracedExpression):
            _validate_assumption_ownership(
                fallback, ports, request_responses, contract_name
            )
        return
    if isinstance(expression, ir_expr.Mux):
        for operand in (
            expression.condition,
            expression.when_true,
            expression.when_false,
        ):
            _validate_assumption_ownership(
                operand, ports, request_responses, contract_name
            )
        return
    if isinstance(expression, ir_expr.Switch):
        _validate_assumption_ownership(
            expression.selector, ports, request_responses, contract_name
        )
        for case in expression.cases:
            _validate_assumption_ownership(
                case.expression, ports, request_responses, contract_name
            )
        _validate_assumption_ownership(
            expression.default, ports, request_responses, contract_name
        )
        return
    raise SemanticError(
        f"assumption contract '{contract_name}' uses an unsupported ownership "
        f"expression {expression!r}"
    )


def _has_priority_cycle(
    names: set[str], edges: set[tuple[str, str]]
) -> bool:
    def reaches(source: str, target: str) -> bool:
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(lower for higher, lower in edges if higher == current)
        return False

    return any(
        reaches(lower, higher) for higher, lower in edges
    )


def _resolve_request_response_target(
    target_text: str,
    interfaces: dict[str, ir_module.RequestResponseInterface],
) -> tuple[
    ir_module.RequestResponseInterface,
    RequestResponseChannel,
    ReadyValidSignal,
    HardwareType,
]:
    parts = target_text.split(".")
    interface = interfaces.get(parts[0])
    if interface is None:
        raise SemanticError(
            f"assignment target '{target_text}' is not a request/response interface"
        )
    if len(parts) != 3:
        raise SemanticError(
            f"request/response interface '{interface.name}' assignments require "
            "a channel and field"
        )
    try:
        channel = RequestResponseChannel(parts[1])
    except ValueError as error:
        raise SemanticError(
            f"request/response interface '{interface.name}' has no channel "
            f"'{parts[1]}'"
        ) from error
    try:
        signal = ReadyValidSignal(parts[2])
    except ValueError as error:
        raise SemanticError(
            f"request/response channel '{interface.name}.{channel.value}' has "
            f"no field '{parts[2]}'"
        ) from error
    if signal is ReadyValidSignal.TRANSFER:
        raise SemanticError(
            f"request/response transfer '{target_text}' is read-only"
        )
    # Role is inferred after all fields are seen.  Accept both channel halves
    # here so a child responder can use the same declaration syntax; the
    # complete, non-overlapping ownership contract is checked below.
    writable = {
        (RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
        (RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
        (RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
        (RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
        (RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD),
        (RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
    }
    if (channel, signal) not in writable:
        raise SemanticError(
            f"cannot drive incoming request/response field '{target_text}'"
        )
    if signal is ReadyValidSignal.PAYLOAD:
        type_ = (
            interface.request_type
            if channel is RequestResponseChannel.REQUEST
            else interface.response_type
        )
    else:
        type_ = BitType()
    return interface, channel, signal, type_


def _render_assignment_target(
    target: ir_module.Port | ir_module.RequestResponseInterface,
    signal: InterfaceSignal | None,
    channel: RequestResponseChannel | None = None,
) -> str:
    if channel is not None and signal is not None:
        return f"{target.name}.{channel.value}.{signal.value}"
    return target.name if signal is None else f"{target.name}.{signal.value}"


def _check_operand_pair(
    left_syntax: ast.Expression,
    right_syntax: ast.Expression,
    inputs: dict[str, _ValueSymbol],
    context: _ExpressionContext,
) -> tuple[ir_expr.Expression, ir_expr.Expression]:
    folded_left = _fold_compile_time_parameter_expression(left_syntax, context)
    folded_right = _fold_compile_time_parameter_expression(right_syntax, context)
    left_is_literal = _is_direct_integer_literal_syntax(folded_left)
    right_is_literal = _is_direct_integer_literal_syntax(folded_right)
    if left_is_literal and right_is_literal:
        return (
            _check_expression(left_syntax, inputs, None, context),
            _check_expression(right_syntax, inputs, None, context),
        )
    if left_is_literal and not right_is_literal:
        right = _check_expression(right_syntax, inputs, None, context)
        left = _check_expression(left_syntax, inputs, right.type, context)
        return left, right
    left = _check_expression(left_syntax, inputs, None, context)
    right_expected = left.type if right_is_literal else None
    right = _check_expression(right_syntax, inputs, right_expected, context)
    return left, right


def _check_binary(
    expression: ast.BinaryExpr,
    inputs: dict[str, _ValueSymbol],
    context: _ExpressionContext,
) -> ir_expr.Expression:
    if expression.operator in {
        ast.BinaryOperator.LOGIC_AND,
        ast.BinaryOperator.LOGIC_OR,
    }:
        raise SemanticError(
            f"runtime logical operator '{expression.operator.value}' is not "
            "supported; use bitwise '&'/'|' for hardware values, or keep "
            "'&&'/'||' inside a compile-time condition or equiv guard",
            code="ZL-SEMANTIC-RUNTIME-LOGIC",
            primary=_semantic_origin(expression, context),
            fixes=(
                "replace the operator with '&' or '|' when both operands are bits",
            ),
        )
    if expression.operator is ast.BinaryOperator.DIVIDE:
        raise SemanticError(
            "division is only supported in compile-time constant expressions"
        )
    if expression.operator in {
        ast.BinaryOperator.SHIFT_LEFT, ast.BinaryOperator.SHIFT_RIGHT,
    }:
        amount = _constant_parameter_expression_text(expression.right, context)
        if amount is not None and context.type_resolver is not None:
            resolved_amount = context.type_resolver._eval_constant_integer(
                amount,
                description="shift amount",
                allow_zero=True,
                allow_negative=True,
            )
            if resolved_amount < 0:
                raise SemanticError("shift amount must be non-negative")
    operator = ir_expr.BinaryOperator(expression.operator.value)
    if operator in {
        ir_expr.BinaryOperator.SHIFT_LEFT,
        ir_expr.BinaryOperator.SHIFT_RIGHT,
    }:
        left = _check_expression(expression.left, inputs, None, context)
        right = _check_expression(expression.right, inputs, None, context)
    else:
        left, right = _check_operand_pair(
            expression.left, expression.right, inputs, context
        )
    if (
        operator in {ir_expr.BinaryOperator.SUBTRACT, ir_expr.BinaryOperator.MULTIPLY}
        and (isinstance(left.type, StructType) or isinstance(right.type, StructType))
    ):
        return _resolve_operator(
            operator.value,
            (left, right),
            context,
            call_origin=_semantic_origin(expression, context),
        )
    if operator in {
        ir_expr.BinaryOperator.EQUAL,
        ir_expr.BinaryOperator.NOT_EQUAL,
    } and (
        isinstance(left.type, (StructType, TupleType, VecType))
        or isinstance(right.type, (StructType, TupleType, VecType))
    ):
        if left.type != right.type:
            raise SemanticError(
                f"aggregate equality requires one exact type, got "
                f"{left.type} and {right.type}"
            )
        equal = _build_aggregate_equality(left, right)
        if operator is ir_expr.BinaryOperator.EQUAL:
            return equal
        return _build_binary(
            ir_expr.BinaryOperator.EQUAL,
            equal,
            ir_expr.Constant(0, BitType()),
        )
    return _build_binary(operator, left, right)


def _build_aggregate_equality(
    left: ir_expr.Expression,
    right: ir_expr.Expression,
) -> ir_expr.Expression:
    """Lower exact aggregate equality to ordinary typed scalar comparisons."""

    if left.type != right.type:
        raise SemanticError(
            f"aggregate equality requires one exact type, got {left.type} and {right.type}"
        )
    comparisons: list[ir_expr.Expression] = []
    if isinstance(left.type, StructType):
        for field in left.type.fields:
            left_item = ir_expr.FieldAccess(left, field.name, field.type)
            right_item = ir_expr.FieldAccess(right, field.name, field.type)
            comparisons.append(
                _build_aggregate_equality(left_item, right_item)
                if isinstance(field.type, (StructType, TupleType, VecType))
                else _build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    elif isinstance(left.type, TupleType):
        for index, element_type in enumerate(left.type.elements):
            left_item = ir_expr.TupleProject(left, index, element_type)
            right_item = ir_expr.TupleProject(right, index, element_type)
            comparisons.append(
                _build_aggregate_equality(left_item, right_item)
                if isinstance(element_type, (StructType, TupleType, VecType))
                else _build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    elif isinstance(left.type, VecType):
        for index in range(left.type.length):
            left_item = ir_expr.VectorIndex(left, index, left.type.element_type)
            right_item = ir_expr.VectorIndex(right, index, right.type.element_type)
            comparisons.append(
                _build_aggregate_equality(left_item, right_item)
                if isinstance(left.type.element_type, (StructType, TupleType, VecType))
                else _build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    else:
        return _build_binary(ir_expr.BinaryOperator.EQUAL, left, right)
    result: ir_expr.Expression = ir_expr.Constant(1, BitType())
    for comparison in comparisons:
        result = _build_binary(
            ir_expr.BinaryOperator.BIT_AND, result, comparison
        )
    return result


def _outer_nominal_name(syntax: ast.TypeSyntax) -> str | None:
    if not isinstance(syntax, ast.TypeName):
        return None
    generic = _TypeResolver._generic_parts(syntax.text)
    return generic[0] if generic is not None else syntax.text


def _validate_operator_declarations(
    declarations: tuple[ast.OperatorDecl, ...],
    structs: tuple[ast.StructDecl, ...],
) -> None:
    struct_by_name = {declaration.name: declaration for declaration in structs}
    struct_names = set(struct_by_name)
    generic_signatures: dict[tuple[str, str, int], list[ast.OperatorDecl]] = {}

    def patterns_overlap(left: ast.OperatorDecl, right: ast.OperatorDecl) -> bool:
        left_names = {item.name for item in left.generic_parameters}
        right_names = {item.name for item in right.generic_parameters}

        def pattern(syntax: ast.TypeSyntax, names: set[str], side: str) -> object:
            if isinstance(syntax, ast.VectorTypeName):
                length: object = (
                    ("var", side, syntax.length)
                    if isinstance(syntax.length, str) and syntax.length in names
                    else ("const", str(syntax.length))
                )
                return ("vec", length, pattern(syntax.element_type, names, side))
            if isinstance(syntax, ast.TupleTypeName):
                return (
                    "tuple",
                    *(pattern(item, names, side) for item in syntax.elements),
                )
            if syntax.text in names:
                return ("var", side, syntax.text)
            generic = _TypeResolver._generic_parts(syntax.text)
            if generic is None:
                return ("type", syntax.text)
            base, arguments = generic
            return (
                "type", base,
                *(pattern(ast.TypeName(item), names, side) for item in arguments),
            )

        substitutions: dict[object, object] = {}

        def dereference(value: object) -> object:
            while isinstance(value, tuple) and value[:1] == ("var",) and value in substitutions:
                value = substitutions[value]
            return value

        def unify(a: object, b: object) -> bool:
            a, b = dereference(a), dereference(b)
            if a == b:
                return True
            if isinstance(a, tuple) and a[:1] == ("var",):
                substitutions[a] = b
                return True
            if isinstance(b, tuple) and b[:1] == ("var",):
                substitutions[b] = a
                return True
            if not isinstance(a, tuple) or not isinstance(b, tuple):
                return False
            return len(a) == len(b) and a[0] == b[0] and all(
                unify(x, y) for x, y in zip(a[1:], b[1:], strict=True)
            )

        return all(
            unify(
                pattern(a.type_name, left_names, "left"),
                pattern(b.type_name, right_names, "right"),
            )
            for a, b in zip(left.parameters, right.parameters, strict=True)
        )
    for declaration in declarations:
        if declaration.operator not in {"+", "-", "*"}:
            raise SemanticError(f"unsupported operator overload '{declaration.operator}'")
        expected_arity = 1 if len(declaration.parameters) == 1 else 2
        if len(declaration.parameters) not in {1, 2} or (
            len(declaration.parameters) == 1 and declaration.operator != "-"
        ):
            raise SemanticError(
                f"operator '{declaration.operator}' has unsupported arity {len(declaration.parameters)}"
            )
        del expected_arity
        owners = tuple(
            _outer_nominal_name(parameter.type_name)
            for parameter in declaration.parameters
        )
        if not any(owner in struct_names for owner in owners):
            raise SemanticError(
                f"operator '{declaration.operator}' must be owned by a nominal struct operand; "
                "built-in scalar and fixed operators cannot be overloaded"
            )
        first_owner = next(owner for owner in owners if owner in struct_names)
        owner_declaration = struct_by_name[first_owner]
        if declaration.source_identity != owner_declaration.source_identity:
            raise SemanticError(
                f"operator '{declaration.operator}' violates nominal-owner coherence: "
                f"'{first_owner}' is owned by {owner_declaration.source_identity or 'this source'}"
            )
        if declaration.generic_parameters:
            key = (first_owner, declaration.operator, len(declaration.parameters))
            previous = next(
                (
                    item for item in generic_signatures.get(key, ())
                    if patterns_overlap(item, declaration)
                ),
                None,
            )
            if previous is not None:
                raise SemanticError(
                    f"overlapping generic operator '{declaration.operator}' declarations "
                    f"for owner '{first_owner}'"
                )
            generic_signatures.setdefault(key, []).append(declaration)


def _resolve_operator(
    symbol: str,
    arguments: tuple[ir_expr.Expression, ...],
    context: _ExpressionContext,
    *,
    call_origin: SourceOrigin | None = None,
) -> ir_expr.Expression:
    struct_names = {declaration.name for declaration in context.struct_declarations}
    candidates: list[tuple[int, ast.OperatorDecl]] = []
    diagnostics: list[str] = []
    rejected: list[ast.OperatorDecl] = []
    for declaration in context.operator_declarations:
        if declaration.operator != symbol or len(declaration.parameters) != len(arguments):
            continue
        owners = [
            _outer_nominal_name(parameter.type_name)
            for parameter in declaration.parameters
        ]
        if not any(owner in struct_names for owner in owners):
            raise SemanticError(
                f"operator '{symbol}' must be owned by at least one nominal operand type"
            )
        try:
            _specialization_bindings(declaration, (), arguments, context)
        except SemanticError as error:
            diagnostics.append(str(error))
            rejected.append(declaration)
            continue
        rank = 1 if declaration.generic_parameters else 2
        candidates.append((rank, declaration))
    if not candidates:
        detail = f"; candidates rejected: {diagnostics[0]}" if diagnostics else ""
        rendered = ", ".join(str(argument.type) for argument in arguments)
        error = SemanticError(
            f"no exact overload for operator '{symbol}' with ({rendered}){detail}"
        )
        if rejected:
            raise _annotate_callable_error(
                error,
                rejected[0],
                context,
                call_origin,
            )
        raise error
    best_rank = max(rank for rank, _ in candidates)
    best = [declaration for rank, declaration in candidates if rank == best_rank]
    if len(best) != 1:
        raise SemanticError(
            f"ambiguous operator '{symbol}' overload for "
            f"({', '.join(str(argument.type) for argument in arguments)})"
        )
    return _specialize_callable(
        best[0],
        arguments,
        (),
        context,
        call_origin=call_origin,
    )


def _build_binary(
    operator: ir_expr.BinaryOperator,
    left: ir_expr.Expression,
    right: ir_expr.Expression,
) -> ir_expr.Expression:
    if isinstance(left.type, EnumType) or isinstance(right.type, EnumType):
        if operator in {
            ir_expr.BinaryOperator.LESS,
            ir_expr.BinaryOperator.LESS_EQUAL,
            ir_expr.BinaryOperator.GREATER,
            ir_expr.BinaryOperator.GREATER_EQUAL,
        }:
            raise SemanticError(
                "ordered comparison is not defined for enum values"
            )
        if operator not in {
            ir_expr.BinaryOperator.EQUAL,
            ir_expr.BinaryOperator.NOT_EQUAL,
        }:
            raise SemanticError("arithmetic is not defined for enum values")
    if operator is ir_expr.BinaryOperator.SUBTRACT:
        try:
            rule = subtraction_rule(left.type, right.type)
        except NumericTypeError as error:
            if error.reason is NumericTypeErrorReason.FRACTION_MISMATCH:
                raise SemanticError(
                    "fixed-point subtraction requires identical fractional widths; "
                    "use explicit quantize/rescale before the operator"
                ) from error
            raise SemanticError(
                f"subtraction requires matching integer families, got "
                f"{left.type} and {right.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator is ir_expr.BinaryOperator.MULTIPLY:
        try:
            rule = multiplication_rule(left.type, right.type)
        except NumericTypeError as error:
            raise SemanticError(
                f"multiplication requires matching integer families, got "
                f"{left.type} and {right.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator in {
        ir_expr.BinaryOperator.BIT_AND,
        ir_expr.BinaryOperator.BIT_OR,
        ir_expr.BinaryOperator.BIT_XOR,
    }:
        try:
            rule = bitwise_rule(left.type, right.type)
        except NumericTypeError as error:
            if error.reason is NumericTypeErrorReason.FAMILY_MISMATCH:
                raise SemanticError(
                    f"bitwise operands require matching type families, got "
                    f"{left.type} and {right.type}"
                ) from error
            raise SemanticError(
                f"bitwise operation is not defined for {left.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator in {
        ir_expr.BinaryOperator.SHIFT_LEFT,
        ir_expr.BinaryOperator.SHIFT_RIGHT,
    }:
        if not isinstance(left.type, (UIntType, SIntType, BitsType)):
            raise SemanticError(f"shift is not defined for {left.type}")
        if not isinstance(right.type, UIntType):
            raise SemanticError(f"shift amount must be unsigned, got {right.type}")
        operand_type = left.type
        result_type = left.type
    else:
        equality = operator in {
            ir_expr.BinaryOperator.EQUAL,
            ir_expr.BinaryOperator.NOT_EQUAL,
        }
        try:
            rule = comparison_rule(
                left.type,
                right.type,
                equality=equality,
            )
        except NumericTypeError as error:
            if error.reason is NumericTypeErrorReason.FAMILY_MISMATCH:
                message = (
                    f"comparison requires matching type families, got "
                    f"{left.type} and {right.type}"
                )
            elif error.reason is NumericTypeErrorReason.NOMINAL_ENUM_MISMATCH:
                message = (
                    f"enum comparison requires matching nominal enum types, got "
                    f"{left.type} and {right.type}"
                )
            elif error.reason is NumericTypeErrorReason.ORDERED_ENUM:
                assert isinstance(left.type, EnumType)
                message = (
                    f"ordered comparison is not defined for enum "
                    f"'{left.type.name}'"
                )
            elif error.reason is NumericTypeErrorReason.ORDERED_BIT:
                message = "ordered comparison is not defined for bit"
            elif error.reason is NumericTypeErrorReason.ORDERED_BITS:
                message = "ordered comparison is not defined for bit vectors"
            elif error.reason is NumericTypeErrorReason.FRACTION_MISMATCH:
                message = (
                    "fixed-point comparison requires identical fractional widths"
                )
            else:
                message = f"comparison is not defined for {left.type}"
            raise SemanticError(message) from error
        operand_type = rule.operand_type
        result_type = rule.result_type

    if isinstance(left, ir_expr.Constant) and isinstance(right, ir_expr.Constant):
        value = _evaluate_constant_binary(operator, left.value, right.value, result_type)
        return ir_expr.Constant(value, result_type)
    return ir_expr.Binary(operator, left, right, operand_type, result_type)


def _check_alternatives(
    syntax: tuple[ast.Expression, ...],
    inputs: dict[str, _ValueSymbol],
    expected: HardwareType | None,
    description: str,
    context: _ExpressionContext,
) -> tuple[tuple[ir_expr.Expression, ...], HardwareType]:
    result_type = expected
    if result_type is None:
        first_nonliteral = next(
            (
                item
                for item in syntax
                if not _is_direct_integer_literal_syntax(item)
            ),
            syntax[0],
        )
        result_type = _check_expression(
            first_nonliteral, inputs, None, context
        ).type
    branches = tuple(
        _check_expression(item, inputs, result_type, context) for item in syntax
    )
    for branch in branches:
        if branch.type != result_type:
            raise SemanticError(
                f"{description} has type {branch.type}, expected {result_type}"
            )
    return branches, result_type


def _evaluate_constant_binary(
    operator: ir_expr.BinaryOperator,
    left: int,
    right: int,
    result_type: HardwareType,
) -> int:
    if operator is ir_expr.BinaryOperator.SUBTRACT:
        value = left - right
    elif operator is ir_expr.BinaryOperator.MULTIPLY:
        value = left * right
    elif operator is ir_expr.BinaryOperator.BIT_AND:
        value = left & right
    elif operator is ir_expr.BinaryOperator.BIT_OR:
        value = left | right
    elif operator is ir_expr.BinaryOperator.BIT_XOR:
        value = left ^ right
    elif operator is ir_expr.BinaryOperator.SHIFT_LEFT:
        value = left << right
    elif operator is ir_expr.BinaryOperator.SHIFT_RIGHT:
        value = left >> right
    elif operator is ir_expr.BinaryOperator.EQUAL:
        return int(left == right)
    elif operator is ir_expr.BinaryOperator.NOT_EQUAL:
        return int(left != right)
    elif operator is ir_expr.BinaryOperator.LESS:
        return int(left < right)
    elif operator is ir_expr.BinaryOperator.LESS_EQUAL:
        return int(left <= right)
    elif operator is ir_expr.BinaryOperator.GREATER:
        return int(left > right)
    elif operator is ir_expr.BinaryOperator.GREATER_EQUAL:
        return int(left >= right)
    else:  # Defensive guard for future operators.
        raise SemanticError(f"cannot fold operator {operator.value}")
    return _normalize(value, result_type)


def _constant_fits(value: int, type_: HardwareType) -> bool:
    # Enum constants have their own nominal-member/decode paths.  Preserve the
    # existing literal boundary, which accepted only numeric scalar families.
    return not isinstance(type_, EnumType) and scalar_fits(value, type_)


def _normalize(value: int, type_: HardwareType) -> int:
    return normalize_scalar(value, type_)


def _reject_recursive_functions(functions: tuple[ir_module.Function, ...]) -> None:
    dependencies = {
        function.name: _called_functions(function.body) for function in functions
    }
    visited: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in active:
            start = active.index(name)
            cycle = " -> ".join((*active[start:], name))
            raise SemanticError(f"recursive function call: {cycle}")
        active.append(name)
        for dependency in dependencies[name]:
            # Name/type resolution owns the unknown-function diagnostic.  The
            # recursion pass follows only concrete definitions participating
            # in this module's retained call graph.
            if dependency in dependencies:
                visit(dependency)
        active.pop()
        visited.add(name)

    for function in functions:
        visit(function.name)


def _called_functions(expression: ir_expr.Expression) -> set[str]:
    if isinstance(
        expression,
        (
            ir_expr.InputRef,
            ir_expr.ParameterRef,
            ir_expr.FunctionalCaptureRef,
            ir_expr.FunctionalTableLookup,
            ir_expr.RegisterRef,
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
            ir_expr.FifoRef,
            ir_expr.MemoryRef,
            ir_expr.RomRef,
            ir_expr.Constant,
        ),
    ):
        return set()
    if isinstance(expression, (ir_expr.EnumEncode, ir_expr.EnumValid)):
        return _called_functions(expression.expression)
    if isinstance(expression, (ir_expr.UnionTag, ir_expr.UnionField)):
        return _called_functions(expression.expression)
    if isinstance(expression, ir_expr.EnumDecode):
        return (
            _called_functions(expression.expression)
            | _called_functions(expression.fallback)
        )
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        return _called_functions(expression.left) | _called_functions(expression.right)
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
            ir_expr.InstanceOutputRef,
            ir_expr.Delay,
            ir_expr.Pipeline,
        ),
    ):
        return _called_functions(expression.expression)
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        called: set[str] = set()
        for operand in expression.operands:
            called |= _called_functions(operand)
        return called
    if isinstance(expression, ir_expr.RuntimeIndex):
        return (
            _called_functions(expression.expression)
            | _called_functions(expression.index)
        )
    if isinstance(expression, ir_expr.VectorUpdate):
        return (
            _called_functions(expression.expression)
            | _called_functions(expression.index)
            | _called_functions(expression.value)
        )
    if isinstance(expression, ir_expr.Mux):
        return (
            _called_functions(expression.condition)
            | _called_functions(expression.when_true)
            | _called_functions(expression.when_false)
        )
    if isinstance(expression, ir_expr.Switch):
        called = _called_functions(expression.selector) | _called_functions(
            expression.default
        )
        for case in expression.cases:
            called |= _called_functions(case.expression)
        return called
    if isinstance(expression, ir_expr.StructConstruct):
        called: set[str] = set()
        for _, value in expression.fields:
            called |= _called_functions(value)
        return called
    if isinstance(expression, ir_expr.TupleConstruct):
        called: set[str] = set()
        for value in expression.elements:
            called |= _called_functions(value)
        return called
    if isinstance(expression, ir_expr.UnionConstruct):
        called: set[str] = set()
        for _, value in expression.fields:
            called |= _called_functions(value)
        return called
    if isinstance(expression, ir_expr.Call):
        called = {expression.function}
        for argument in expression.arguments:
            called |= _called_functions(argument)
        return called
    if isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
        called: set[str] = set()
        for element in expression.elements:
            called |= _called_functions(element)
        return called
    if isinstance(expression, ir_expr.FunctionalRegion):
        called = _called_functions(expression.template)
        for table in expression.tables:
            for value in table.values:
                called |= _called_functions(value)
        for _, value in expression.captures:
            called |= _called_functions(value)
        return called
    if isinstance(expression, ir_expr.Dot):
        return _called_functions(expression.left) | _called_functions(
            expression.right
        )
    if isinstance(expression, ir_expr.Reduce):
        return _called_functions(expression.collection)
    if isinstance(expression, ir_expr.ImplementationChoice):
        called: set[str] = set()
        for alternative in expression.alternatives:
            called |= _called_functions(alternative.expression)
        return called
    raise SemanticError(f"cannot inspect function calls in {expression!r}")


def _reject_instance_output_dependency_cycles(
    child_irs: dict[str, ir_module.Module],
    bindings: tuple[ir_module.InstancePortBinding, ...],
    locals_: tuple[ir_module.LocalValue, ...],
) -> None:
    """Reject current-cycle cycles across scalar child instance boundaries.

    An InstanceOutputRef is a read-only value, but a combinational child's
    output can still depend on one of its input bindings.  Model exactly that
    typed dependency relation.  Registers, storage and explicit delay/pipeline
    nodes terminate the current-cycle walk, so sequential feedback remains
    legal and no cross-module scheduler is introduced.
    """

    local_values = {item.name: item.expression for item in locals_}

    def references(
        expression: ir_expr.Expression,
        *,
        local_expressions: dict[str, ir_expr.Expression],
        active_locals: frozenset[str] = frozenset(),
    ) -> tuple[set[str], set[tuple[str, str]]]:
        input_names: set[str] = set()
        instance_outputs: set[tuple[str, str]] = set()

        def visit(value: object, active: frozenset[str]) -> None:
            if isinstance(value, ir_expr.InputRef):
                replacement = local_expressions.get(value.name)
                if replacement is not None:
                    if value.name in active:
                        raise SemanticError(
                            f"cyclic immutable local '{value.name}' in child "
                            "dependency analysis"
                        )
                    visit(replacement, active | {value.name})
                else:
                    input_names.add(value.name)
                return
            if isinstance(value, ir_expr.InstanceOutputRef):
                instance_outputs.add((value.instance, value.port))
                return
            if isinstance(
                value,
                (
                    ir_expr.RegisterRef,
                    ir_expr.FifoRef,
                    ir_expr.MemoryRef,
                    ir_expr.RomRef,
                    ir_expr.Delay,
                    ir_expr.Pipeline,
                    ir_expr.Constant,
                    ir_expr.ParameterRef,
                    ir_expr.FunctionalCaptureRef,
                    ir_expr.FunctionalTableLookup,
                ),
            ):
                return
            if isinstance(value, tuple):
                for item in value:
                    visit(item, active)
                return
            if not is_dataclass(value):
                return
            for description in fields(value):
                if description.name in {"origin", "type"} or not description.init:
                    continue
                visit(getattr(value, description.name), active)

        visit(expression, active_locals)
        return input_names, instance_outputs

    binding_by_input = {
        (item.instance, item.port): item.expression for item in bindings
    }
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for instance, child in child_irs.items():
        # The declaration-name alias and each physical name point at the same
        # child IR.  Only physical instances that own bindings participate.
        if not any(item.instance == instance for item in bindings):
            continue
        child_locals = {item.name: item.expression for item in child.locals}
        child_inputs = {item.name for item in child.inputs}
        for output in child.outputs:
            if output.protocol is not InterfaceProtocol.WIRE:
                continue
            assignment = next(
                (
                    item
                    for item in child.assignments
                    if item.target.name == output.name
                    and item.signal is None
                    and item.channel is None
                ),
                None,
            )
            dependencies: set[tuple[str, str]] = set()
            if assignment is not None:
                input_dependencies, _ = references(
                    assignment.expression,
                    local_expressions=child_locals,
                )
                for input_name in input_dependencies & child_inputs:
                    bound = binding_by_input.get((instance, input_name))
                    if bound is None:
                        continue
                    _, referenced_outputs = references(
                        bound,
                        local_expressions=local_values,
                    )
                    dependencies.update(referenced_outputs)
            graph[(instance, output.name)] = dependencies

    visited: set[tuple[str, str]] = set()
    active: list[tuple[str, str]] = []

    def visit(node: tuple[str, str]) -> None:
        if node in active:
            start = active.index(node)
            cycle = " -> ".join(
                f"{instance}.{port}" for instance, port in (*active[start:], node)
            )
            raise SemanticError(
                f"combinational child dependency cycle: {cycle}"
            )
        if node in visited or node not in graph:
            return
        active.append(node)
        for dependency in sorted(graph[node]):
            visit(dependency)
        active.pop()
        visited.add(node)

    for node in sorted(graph):
        visit(node)


def _reject_interface_dependency_cycles(
    assignments: tuple[ir_module.Assignment, ...],
) -> None:
    driven = {
        (assignment.target.name, assignment.channel, assignment.signal): assignment
        for assignment in assignments
        if assignment.signal is not None
    }
    dependencies = {
        target: _interface_dependencies(assignment.expression) & driven.keys()
        for target, assignment in driven.items()
    }
    visited: set[
        tuple[str, RequestResponseChannel | None, InterfaceSignal]
    ] = set()
    active: list[
        tuple[str, RequestResponseChannel | None, InterfaceSignal]
    ] = []

    def visit(
        target: tuple[str, RequestResponseChannel | None, InterfaceSignal]
    ) -> None:
        if target in active:
            start = active.index(target)
            cycle = " -> ".join(
                (
                    f"{interface}.{channel.value}.{signal.value}"
                    if channel is not None
                    else f"{interface}.{signal.value}"
                )
                for interface, channel, signal in (*active[start:], target)
            )
            protocols = {
                "request_response" if channel is not None else type(signal)
                for _, channel, signal in (*active[start:], target)
            }
            description = (
                "ready/valid"
                if protocols == {ReadyValidSignal}
                else "credit"
                if protocols == {CreditSignal}
                else "request/response"
                if protocols == {"request_response"}
                else "interface"
            )
            raise SemanticError(f"combinational {description} dependency cycle: {cycle}")
        if target in visited:
            return
        active.append(target)
        for dependency in dependencies[target]:
            visit(dependency)
        active.pop()
        visited.add(target)

    for target in driven:
        visit(target)


def _interface_dependencies(
    expression: ir_expr.Expression,
) -> set[
    tuple[str, RequestResponseChannel | None, InterfaceSignal]
]:
    if isinstance(expression, ir_expr.ReadyValidRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            return {
                (expression.interface, None, ReadyValidSignal.VALID),
                (expression.interface, None, ReadyValidSignal.READY),
            }
        return {(expression.interface, None, expression.signal)}
    if isinstance(expression, ir_expr.CreditRef):
        if expression.signal is CreditSignal.TRANSFER:
            return {(expression.interface, None, CreditSignal.SEND)}
        return {(expression.interface, None, expression.signal)}
    if isinstance(expression, ir_expr.PacketRef):
        if expression.signal is PacketSignal.TRANSFER:
            return {
                (expression.interface, None, PacketSignal.VALID),
                (expression.interface, None, PacketSignal.READY),
            }
        return {(expression.interface, None, expression.signal)}
    if isinstance(expression, ir_expr.VirtualChannelCreditRef):
        if expression.signal is VirtualChannelCreditSignal.TRANSFER:
            return {
                (expression.interface, None, VirtualChannelCreditSignal.SEND)
            }
        return {(expression.interface, None, expression.signal)}
    if isinstance(expression, ir_expr.RequestResponseRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            return {
                (
                    expression.interface,
                    expression.channel,
                    ReadyValidSignal.VALID,
                ),
                (
                    expression.interface,
                    expression.channel,
                    ReadyValidSignal.READY,
                ),
            }
        return {
            (expression.interface, expression.channel, expression.signal)
        }
    if isinstance(
        expression,
        (
            ir_expr.InputRef,
            ir_expr.ParameterRef,
            ir_expr.FunctionalCaptureRef,
            ir_expr.FunctionalTableLookup,
            ir_expr.RegisterRef,
            ir_expr.FifoRef,
            ir_expr.MemoryRef,
            ir_expr.RomRef,
            ir_expr.InstanceOutputRef,
            ir_expr.Constant,
        ),
    ):
        return set()
    if isinstance(expression, (ir_expr.EnumEncode, ir_expr.EnumValid)):
        return _interface_dependencies(expression.expression)
    if isinstance(expression, (ir_expr.UnionTag, ir_expr.UnionField)):
        return _interface_dependencies(expression.expression)
    if isinstance(expression, ir_expr.EnumDecode):
        return (
            _interface_dependencies(expression.expression)
            | _interface_dependencies(expression.fallback)
        )
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        return _interface_dependencies(expression.left) | _interface_dependencies(
            expression.right
        )
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
            ir_expr.Delay,
            ir_expr.Pipeline,
        ),
    ):
        return _interface_dependencies(expression.expression)
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for operand in expression.operands:
            dependencies |= _interface_dependencies(operand)
        return dependencies
    if isinstance(expression, ir_expr.RuntimeIndex):
        return (
            _interface_dependencies(expression.expression)
            | _interface_dependencies(expression.index)
        )
    if isinstance(expression, ir_expr.VectorUpdate):
        return (
            _interface_dependencies(expression.expression)
            | _interface_dependencies(expression.index)
            | _interface_dependencies(expression.value)
        )
    if isinstance(expression, ir_expr.Mux):
        return (
            _interface_dependencies(expression.condition)
            | _interface_dependencies(expression.when_true)
            | _interface_dependencies(expression.when_false)
        )
    if isinstance(expression, ir_expr.StructConstruct):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for _, value in expression.fields:
            dependencies |= _interface_dependencies(value)
        return dependencies
    if isinstance(expression, ir_expr.TupleConstruct):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for value in expression.elements:
            dependencies |= _interface_dependencies(value)
        return dependencies
    if isinstance(expression, ir_expr.UnionConstruct):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for _, value in expression.fields:
            dependencies |= _interface_dependencies(value)
        return dependencies
    if isinstance(expression, ir_expr.Switch):
        dependencies = _interface_dependencies(
            expression.selector
        ) | _interface_dependencies(expression.default)
        for case in expression.cases:
            dependencies |= _interface_dependencies(case.expression)
        return dependencies
    if isinstance(expression, ir_expr.Call):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for argument in expression.arguments:
            dependencies |= _interface_dependencies(argument)
        return dependencies
    if isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for element in expression.elements:
            dependencies |= _interface_dependencies(element)
        return dependencies
    if isinstance(expression, ir_expr.FunctionalRegion):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for table in expression.tables:
            for value in table.values:
                dependencies |= _interface_dependencies(value)
        for _, value in expression.captures:
            dependencies |= _interface_dependencies(value)
        return dependencies
    if isinstance(expression, ir_expr.Dot):
        return _interface_dependencies(expression.left) | _interface_dependencies(
            expression.right
        )
    if isinstance(expression, ir_expr.Reduce):
        return _interface_dependencies(expression.collection)
    if isinstance(expression, ir_expr.ImplementationChoice):
        dependencies: set[
            tuple[str, RequestResponseChannel | None, InterfaceSignal]
        ] = set()
        for alternative in expression.alternatives:
            dependencies |= _interface_dependencies(alternative.expression)
        return dependencies
    raise SemanticError(
        f"cannot inspect interface dependencies in {expression!r}"
    )


def _inline_semantic_locals(
    expression: ir_expr.Expression,
    locals_: tuple[ir_module.LocalValue, ...],
) -> ir_expr.Expression:
    """Resolve pure locals before architecture/pipeline recognition."""
    values = {item.name: item.expression for item in locals_}

    def walk(value):
        if isinstance(value, ir_expr.InputRef) and value.name in values:
            return walk(values[value.name])
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value):
            updates = {}
            for item in fields(value):
                if item.name == "origin" or not item.init:
                    continue
                current = getattr(value, item.name)
                if isinstance(current, tuple):
                    updates[item.name] = tuple(walk(child) for child in current)
                elif is_dataclass(current):
                    updates[item.name] = walk(current)
            return replace(value, **updates) if updates else value
        return value

    return walk(expression)


def _expand_analysis_calls(
    expression: ir_expr.Expression,
    context: _ExpressionContext,
    *,
    purpose: str,
) -> ir_expr.Expression:
    """Create an exact temporary analysis view of retained callable bodies."""

    function_definitions = tuple(
        context.function_definitions[name]
        for name in sorted(context.function_definitions)
    )
    callable_definitions = tuple(
        context.callable_definitions[identity]
        for identity in sorted(context.callable_definitions)
    )
    try:
        return expand_callable_calls(
            expression,
            (*function_definitions, *callable_definitions),
        )
    except CallableExpansionError as error:
        raise SemanticError(f"cannot analyze {purpose}: {error}") from error


def _expand_exploration_calls(
    expression: ir_expr.Expression,
    functions: tuple[ir_module.Function, ...] | list[ir_module.Function],
    context: _ExpressionContext,
) -> ir_expr.Expression:
    """Expose callable arithmetic to the existing bounded matchers.

    Specialization remains represented once in the module IR; this expansion
    is a temporary analysis view used only by exploration.  The shared helper
    supplies deterministic depth/node bounds and deliberately keeps the exact
    implementation attached to a nominal ``Reduce`` opaque.
    """

    if not context.function_definitions:
        context.function_definitions.update(
            (function.name, function) for function in functions
        )
    return _expand_analysis_calls(expression, context, purpose="exploration operand")


def _expression_latency(expression: ir_expr.Expression) -> int | None:
    if isinstance(expression, ir_expr.Constant):
        return None
    if isinstance(
        expression,
        (
            ir_expr.InputRef,
            ir_expr.ParameterRef,
            ir_expr.FunctionalCaptureRef,
            ir_expr.FunctionalTableLookup,
            ir_expr.RegisterRef,
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
            ir_expr.FifoRef,
            ir_expr.MemoryRef,
            ir_expr.RomRef,
        ),
    ):
        return 0
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        return _aligned_latency(
            expression.operator.value if isinstance(expression, ir_expr.Binary) else "+",
            (
                _expression_latency(expression.left),
                _expression_latency(expression.right),
            ),
        )
    if isinstance(expression, ir_expr.InstanceOutputRef):
        return 0
    if isinstance(expression, (ir_expr.EnumEncode, ir_expr.EnumValid)):
        return _expression_latency(expression.expression)
    if isinstance(expression, (ir_expr.UnionTag, ir_expr.UnionField)):
        return _expression_latency(expression.expression)
    if isinstance(expression, ir_expr.EnumDecode):
        return _aligned_latency(
            "enum decode",
            (
                _expression_latency(expression.expression),
                _expression_latency(expression.fallback),
            ),
        )
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
        ),
    ):
        return _expression_latency(expression.expression)
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        return _aligned_latency(
            "concat",
            tuple(_expression_latency(operand) for operand in expression.operands),
        )
    if isinstance(expression, ir_expr.RuntimeIndex):
        return _aligned_latency(
            "runtime index",
            (
                _expression_latency(expression.expression),
                _expression_latency(expression.index),
            ),
        )
    if isinstance(expression, ir_expr.VectorUpdate):
        return _aligned_latency(
            "vector update",
            (
                _expression_latency(expression.expression),
                _expression_latency(expression.index),
                _expression_latency(expression.value),
            ),
        )
    if isinstance(expression, ir_expr.Delay):
        return (_expression_latency(expression.expression) or 0) + expression.cycles
    if isinstance(expression, ir_expr.Pipeline):
        return (_expression_latency(expression.expression) or 0) + expression.stages
    if isinstance(expression, ir_expr.Mux):
        return _aligned_latency(
            "mux",
            (
                _expression_latency(expression.condition),
                _expression_latency(expression.when_true),
                _expression_latency(expression.when_false),
            ),
        )
    if isinstance(expression, ir_expr.Switch):
        return _aligned_latency(
            "switch",
            (
                _expression_latency(expression.selector),
                *(
                    _expression_latency(case.expression)
                    for case in expression.cases
                ),
                _expression_latency(expression.default),
            ),
        )
    if isinstance(expression, ir_expr.Call):
        return _aligned_latency(
            f"call to {expression.function}",
            tuple(_expression_latency(argument) for argument in expression.arguments),
        )
    if isinstance(expression, ir_expr.StructConstruct):
        return _aligned_latency(
            "struct construction",
            tuple(_expression_latency(value) for _, value in expression.fields),
        )
    if isinstance(expression, ir_expr.TupleConstruct):
        return _aligned_latency(
            "tuple construction",
            tuple(_expression_latency(value) for value in expression.elements),
        )
    if isinstance(expression, ir_expr.UnionConstruct):
        return _aligned_latency(
            "tagged-union construction",
            tuple(_expression_latency(value) for _, value in expression.fields),
        )
    if isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
        return _aligned_latency(
            type(expression).__name__.lower(),
            tuple(_expression_latency(element) for element in expression.elements),
        )
    if isinstance(expression, ir_expr.FunctionalRegion):
        retained = (
            *(value for table in expression.tables for value in table.values),
            *(value for _, value in expression.captures),
        )
        return _aligned_latency(
            f"functional {expression.kind.value}",
            tuple(_expression_latency(value) for value in retained),
        )
    if isinstance(expression, ir_expr.Dot):
        return _aligned_latency(
            "dot",
            tuple(_expression_latency(product) for product in expression.products),
        )
    if isinstance(expression, ir_expr.Reduce):
        return _expression_latency(expression.collection)
    if isinstance(expression, ir_expr.ImplementationChoice):
        # Semantic validation proves every implementation arm has identical
        # cycle timing before an automatic policy is extracted.
        return expression.alternatives[0].semantics.latency
    raise SemanticError(f"cannot determine latency of {expression!r}")


def _aligned_latency(description: str, latencies: tuple[int | None, ...]) -> int | None:
    concrete = {latency for latency in latencies if latency is not None}
    if len(concrete) > 1:
        rendered = ", ".join(str(latency) for latency in sorted(concrete))
        raise SemanticError(
            f"latency mismatch in {description}: operands have latencies {rendered}",
            code="ZL-TIMING-MISMATCH",
            fixes=("align operands explicitly before combining them",),
        )
    return next(iter(concrete), None)


def _resized_type(type_: HardwareType, width: int) -> HardwareType:
    if isinstance(type_, UIntType):
        return UIntType(width)
    if isinstance(type_, SIntType):
        return SIntType(width)
    if isinstance(type_, BitsType):
        return BitsType(width)
    if isinstance(type_, (FixedType, UFixedType)):
        if width <= type_.fraction:
            raise SemanticError("fixed-point resize must retain at least one integer/sign bit")
        return type(type_)(width, type_.fraction)
    raise SemanticError(f"cannot resize {type_}")
