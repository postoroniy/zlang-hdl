# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned source type resolution and exact type observations."""

from __future__ import annotations

import ast as pyast
import re
from fractions import Fraction
from typing import TYPE_CHECKING

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import compile_time_real as ct_real
from . import limits as semantic_limits
from . import observations
from .errors import SemanticError
from .integer_intrinsics import INTEGER_INTRINSICS, evaluate_integer_intrinsic

if TYPE_CHECKING:
    from . import context as semantic_context

_UNSIGNED_PATTERN = re.compile(r"u([1-9][0-9]*)\Z")
_SIGNED_PATTERN = re.compile(r"s([1-9][0-9]*)\Z")
_GENERIC_PATTERN = re.compile(r"(uint|sint|bits)<(.+)>\Z")

def apply_compile_time_real_intrinsic(
    name: str,
    arguments: tuple[ct_real.CompileTimeReal, ...],
) -> ct_real.CompileTimeReal:
    """Apply one compiler-owned real intrinsic with one arity contract."""

    validate_compile_time_real_intrinsic_arity(name, len(arguments))
    try:
        if name == "pi":
            return ct_real.CompileTimeReal.pi()
        if name == "sin":
            return ct_real.sin(arguments[0])
        if name == "cos":
            return ct_real.cos(arguments[0])
        if name == "log2":
            return ct_real.log2(arguments[0])
        if name == "log":
            return ct_real.log(arguments[0], arguments[1])
        if name == "exp":
            return ct_real.exp(arguments[0])
        if name == "sqrt":
            return ct_real.sqrt(arguments[0])
    except ct_real.CompileTimeRealError as error:
        raise SemanticError(str(error)) from error
    raise SemanticError(f"unknown compile-time real intrinsic '{name}'")


def validate_compile_time_real_intrinsic_arity(name: str, actual: int) -> None:
    expected_arity = 0 if name == "pi" else 2 if name == "log" else 1
    if actual != expected_arity:
        noun = "no arguments" if expected_arity == 0 else (
            "one argument" if expected_arity == 1 else "two arguments"
        )
        raise SemanticError(f"intrinsic '{name}' expects {noun}")


def builtin_type(name: str) -> ir_types.HardwareType | None:
    if name == "bit":
        return ir_types.BitType()
    if name == "char":
        return ir_types.UIntType(8)
    unsigned_match = _UNSIGNED_PATTERN.fullmatch(name)
    if unsigned_match is not None:
        return ir_types.UIntType(int(unsigned_match.group(1)))
    signed_match = _SIGNED_PATTERN.fullmatch(name)
    if signed_match is not None:
        return ir_types.SIntType(int(signed_match.group(1)))
    generic_match = _GENERIC_PATTERN.fullmatch(name)
    if generic_match is None:
        return None
    family, width_text = generic_match.groups()
    if not width_text.isdigit():
        return None
    width = int(width_text)
    if family == "uint":
        return ir_types.UIntType(width)
    if family == "sint":
        return ir_types.SIntType(width)
    return ir_types.BitsType(width)


def contains_nominal_type(
    type_: ir_types.HardwareType,
    nominal_type: type[ir_types.HardwareType],
) -> bool:
    """Return whether an aggregate recursively contains ``nominal_type``."""

    if isinstance(type_, nominal_type):
        return True
    if isinstance(type_, ir_types.StructType):
        return any(
            contains_nominal_type(field.type, nominal_type)
            for field in type_.fields
        )
    if isinstance(type_, ir_types.TupleType):
        return any(
            contains_nominal_type(element, nominal_type)
            for element in type_.elements
        )
    if isinstance(type_, ir_types.VecType):
        return contains_nominal_type(type_.element_type, nominal_type)
    return False


def tuple_type_parts(text: str) -> tuple[str, ...] | None:
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


class TypeResolver:
    def __init__(
        self,
        aliases: tuple[ast.TypeAlias, ...],
        structs: tuple[ast.StructDecl, ...],
        enums: tuple[ast.EnumDecl, ...] = (),
        parameters: tuple[ast.ModuleParameter, ...] = (),
        parameter_values: dict[str, int | str] | None = None,
        type_bindings: dict[str, ir_types.HardwareType] | None = None,
        identity_namespace: str | None = None,
        tagged_unions: tuple[ast.TaggedUnionDecl, ...] = (),
    ) -> None:
        self._aliases: dict[str, ast.TypeSyntax] = {}
        self._alias_declarations: dict[str, ast.TypeAlias] = {}
        self._structs: dict[str, ast.StructDecl] = {}
        self._enum_declarations: dict[str, ast.EnumDecl] = {}
        self._enums: dict[str, ir_types.EnumType] = {}
        self._tagged_union_declarations: dict[str, ast.TaggedUnionDecl] = {}
        self._resolved: dict[str, ir_types.HardwareType] = {}
        self._active: list[str] = []
        self._parameters = {p.name: p for p in parameters}
        self._parameter_values = dict(parameter_values or {})
        self._type_bindings = dict(type_bindings or {})
        self._identity_namespace = identity_namespace
        # Set by the owning semantic context once optional editor metadata is
        # enabled.  Type resolution remains fully semantic; this reference is
        # only an observational sink for authoritative definition records.
        self._definition_context: semantic_context.ExpressionContext | None = None

        for declaration in aliases:
            self._check_available(declaration.name, "type alias")
            self._aliases[declaration.name] = declaration.target
            self._alias_declarations[declaration.name] = declaration
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
                if not isinstance(backing, ir_types.BitsType):
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
            enum_type = ir_types.EnumType(
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
        if builtin_type(name) is not None or name == "string":
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

    def resolve(self, syntax: ast.TypeSyntax) -> ir_types.HardwareType:
        if self._definition_context is not None:
            record_named_type_definition(self._definition_context, syntax, self)
        if isinstance(syntax, ast.VectorTypeName):
            length = syntax.length
            if isinstance(length, str):
                length = self._eval_width(length)
            if length < 1:
                raise SemanticError("vector width must be positive")
            return ir_types.VecType(length, self.resolve(syntax.element_type))
        if isinstance(syntax, ast.TupleTypeName):
            return ir_types.TupleType(tuple(self.resolve(item) for item in syntax.elements))
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

        def visit_real(node: pyast.AST) -> ct_real.CompileTimeReal:
            if isinstance(node, pyast.Constant) and isinstance(node.value, int):
                return ct_real.CompileTimeReal.rational_value(Fraction(node.value))
            if isinstance(node, pyast.Name):
                return ct_real.CompileTimeReal.rational_value(Fraction(visit(node)))
            if isinstance(node, pyast.UnaryOp) and isinstance(
                node.op, (pyast.UAdd, pyast.USub)
            ):
                value = visit_real(node.operand)
                return value if isinstance(node.op, pyast.UAdd) else ct_real.negate(value)
            if isinstance(node, pyast.BinOp) and isinstance(
                node.op, (pyast.Add, pyast.Sub, pyast.Mult, pyast.Div)
            ):
                left = visit_real(node.left)
                right = visit_real(node.right)
                if isinstance(node.op, pyast.Add):
                    return ct_real.add(left, right)
                if isinstance(node.op, pyast.Sub):
                    return ct_real.subtract(left, right)
                if isinstance(node.op, pyast.Mult):
                    return ct_real.multiply(left, right)
                return ct_real.divide(left, right)
            if (
                isinstance(node, pyast.Call)
                and isinstance(node.func, pyast.Name)
                and node.func.id in semantic_limits.REAL_INTRINSICS
            ):
                validate_compile_time_real_intrinsic_arity(
                    node.func.id, len(node.args)
                )
                return apply_compile_time_real_intrinsic(
                    node.func.id,
                    tuple(visit_real(argument) for argument in node.args),
                )
            raise SemanticError(
                f"{description} '{text}' is not a compile-time real expression"
            )

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
                if isinstance(node.op, pyast.Add):
                    return left + right
                if isinstance(node.op, pyast.Sub):
                    return left - right
                if isinstance(node.op, pyast.Mult):
                    return left * right
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
                if isinstance(node.op, pyast.BitAnd):
                    return left & right
                if isinstance(node.op, pyast.BitOr):
                    return left | right
                if isinstance(node.op, pyast.BitXor):
                    return left ^ right
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
                if node.func.id in semantic_limits.REAL_INTRINSICS:
                    try:
                        value = visit_real(node)
                    except ct_real.CompileTimeRealError as error:
                        raise SemanticError(str(error)) from error
                    exact = value.exact_integer()
                    if exact is None:
                        raise SemanticError(
                            f"constant {description} intrinsic '{node.func.id}' "
                            "produced a non-integral compile-time real value"
                        )
                    return exact
                if len(node.args) != 1:
                    raise SemanticError(
                        f"compile-time intrinsic '{node.func.id}' expects one argument"
                    )
                value = visit(node.args[0])
                if node.func.id in INTEGER_INTRINSICS:
                    return evaluate_integer_intrinsic(
                        node.func.id, value
                    )
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

    def evaluate_constant_integer(
        self,
        text: str,
        *,
        description: str,
        allow_zero: bool = False,
    ) -> int:
        """Expose the bounded integer evaluator to semantic subsystems."""

        return self._eval_constant_integer(
            text,
            description=description,
            allow_zero=allow_zero,
        )

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

    def _resolve_builtin(self, name: str) -> ir_types.HardwareType | None:
        if name == "bit":
            return ir_types.BitType()
        if name == "char":
            return ir_types.UIntType(8)
        string = re.fullmatch(r"string<(.+)>", name)
        if string:
            length = self._eval_width(string.group(1))
            return ir_types.VecType(length, ir_types.UIntType(8))
        vector = re.fullmatch(r"vec<([^,]+),(.+)>", name)
        if vector:
            length = self._eval_width(vector.group(1))
            return ir_types.VecType(length, self.resolve(ast.TypeName(vector.group(2).strip())))
        match = re.fullmatch(r"(uint|sint|bits)<(.+)>", name)
        if match:
            width = self._eval_width(match.group(2))
            return {"uint": ir_types.UIntType, "sint": ir_types.SIntType, "bits": ir_types.BitsType}[match.group(1)](width)
        concise_fixed = re.fullmatch(r"(SF_Sat|UF_Sat|SF|UF)([1-9][0-9]*)\.([0-9]+)", name)
        if concise_fixed:
            family, integer_text, fraction_text = concise_fixed.groups()
            integer, fraction = int(integer_text), int(fraction_text)
            fixed_class = ir_types.FixedType if family.startswith("SF") else ir_types.UFixedType
            overflow = (ir_types.FixedOverflowPolicy.SATURATE
                        if "_Sat" in family else ir_types.FixedOverflowPolicy.WRAP)
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
            fixed_class = ir_types.FixedType if family in {"fixed", "fixed_sat"} else ir_types.UFixedType
            overflow = (ir_types.FixedOverflowPolicy.SATURATE
                        if family.endswith("_sat") else ir_types.FixedOverflowPolicy.WRAP)
            return fixed_class(width, fraction, overflow)
        return builtin_type(name)

    @staticmethod
    def _generic_parts(name: str) -> tuple[str, tuple[str, ...]] | None:
        start = name.find("<")
        if start < 0 or not name.endswith(">"):
            return None
        angle_depth, paren_depth, parts, begin = 0, 0, [], start + 1
        for index in range(start + 1, len(name) - 1):
            char = name[index]
            if char == "<":
                angle_depth += 1
            elif char == ">":
                angle_depth -= 1
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "," and angle_depth == 0 and paren_depth == 0:
                parts.append(name[begin:index].strip())
                begin = index + 1
        parts.append(name[begin:-1].strip())
        return name[:start], tuple(parts)

    def _resolve_named(self, name: str) -> ir_types.HardwareType:
        if name in self._resolved:
            return self._resolved[name]
        if name in self._active:
            start = self._active.index(name)
            cycle = " -> ".join((*self._active[start:], name))
            raise SemanticError(f"cyclic type alias: {cycle}")
        self._active.append(name)
        try:
            tuple_parts = tuple_type_parts(name)
            if tuple_parts is not None:
                resolved_tuple = ir_types.TupleType(
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
                bindings: dict[str, ir_types.HardwareType] = {}
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
                nested = TypeResolver(
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
                fields = tuple(ir_types.StructField(field.name, nested.resolve(field.type_name)) for field in declaration.fields)
                result = ir_types.StructType(
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
                fields: list[ir_types.StructField] = []
                for field in declaration.fields:
                    if field.name in field_names:
                        raise SemanticError(
                            f"duplicate field '{field.name}' in struct '{name}'"
                        )
                    field_names.add(field.name)
                    fields.append(ir_types.StructField(field.name, self.resolve(field.type_name)))
                result = ir_types.StructType(name, tuple(fields))
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
                variants: list[ir_types.TaggedUnionVariant] = []
                flat = (ir_types.BitType, ir_types.BitsType, ir_types.UIntType, ir_types.SIntType, ir_types.FixedType, ir_types.UFixedType)
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
                    fields_: list[ir_types.TaggedUnionField] = []
                    for field_ in variant.fields:
                        resolved = self.resolve(field_.type_name)
                        if not isinstance(resolved, flat):
                            raise SemanticError(
                                f"tagged-union field '{name}.{variant.name}.{field_.name}' "
                                f"must have a flat scalar type, got {resolved}"
                            )
                        fields_.append(ir_types.TaggedUnionField(field_.name, resolved))
                    variants.append(ir_types.TaggedUnionVariant(variant.name, tuple(fields_)))
                result = ir_types.TaggedUnionType(
                    name, tuple(variants), f"{owner}::union::{name}"
                )
            elif (
                name in self._parameters
                and self._parameters[name].kind == "type"
            ):
                raise SemanticError(
                    f"unknown type '{name}': generic type parameter requires "
                    "a concrete specialization",
                    code="ZL-GENERIC-SPECIALIZATION-REQUIRED",
                )
            else:
                raise SemanticError(f"unknown type '{name}'")
            self._resolved[name] = result
            return result
        finally:
            self._active.pop()

    def resolve_all(self) -> tuple[ir_types.StructType, ...]:
        for name in self._aliases:
            self._resolve_named(name)
        return tuple(
            self._resolve_named(name)
            for name, declaration in self._structs.items()
            if not declaration.parameters
        )

    def resolve_enums(self) -> tuple[ir_types.EnumType, ...]:
        """Return nominal enums in source declaration order."""
        return tuple(self._enums.values())

    def resolve_tagged_unions(self) -> tuple[ir_types.TaggedUnionType, ...]:
        return tuple(
            self._resolve_named(name)
            for name in self._tagged_union_declarations
        )

    def tagged_union_type(self, name: str) -> ir_types.TaggedUnionType | None:
        if name not in self._tagged_union_declarations:
            return None
        resolved = self._resolve_named(name)
        assert isinstance(resolved, ir_types.TaggedUnionType)
        return resolved

    def enum_type(self, name: str) -> ir_types.EnumType | None:
        return self._enums.get(name)

    def named_declaration(
        self, name: str
    ) -> tuple[str, ast.TypeAlias | ast.StructDecl | ast.EnumDecl] | None:
        """Return one source declaration for a non-builtin named type."""

        if name in self._alias_declarations:
            return "type", self._alias_declarations[name]
        if name in self._structs:
            return "type", self._structs[name]
        if name in self._enum_declarations:
            return "enum", self._enum_declarations[name]
        generic = self._generic_parts(name)
        if generic is not None:
            base, _arguments = generic
            if base in self._structs:
                return "type", self._structs[base]
            if base in self._alias_declarations:
                return "type", self._alias_declarations[base]
            if base in self._enum_declarations:
                return "enum", self._enum_declarations[base]
        return None

    def set_definition_context(self, context: semantic_context.ExpressionContext | None) -> None:
        """Attach the current semantic context for optional type definitions."""

        self._definition_context = context


def record_named_type_definition(
    context: semantic_context.ExpressionContext,
    syntax: ast.TypeSyntax,
    resolver: TypeResolver,
) -> None:
    """Record a resolved named-type occurrence for editor definition lookup.

    This is deliberately attached to the existing type resolver rather than
    re-resolving syntax in tooling.  Builtin and module type parameters have
    no source declaration target in the current workspace and are therefore
    excluded.  The operation is observational only and is gated by the
    demand-driven ``DEFINITIONS`` need.
    """

    if (
        not context.services.tooling.analysis_needs.wants(AnalysisNeeds.DEFINITIONS)
        or context.services.tooling.definition_resolutions is None
        or not isinstance(syntax, ast.TypeName)
        or syntax.origin is None
    ):
        return
    entries = syntax.named_origins or ((syntax.text, syntax.origin),)
    for name, occurrence_span in entries:
        generic = resolver._generic_parts(name)
        base = generic[0] if generic is not None else name
        if (
            resolver._resolve_builtin(base) is not None
            or base in resolver._type_bindings
            or base in resolver._parameter_values
        ):
            continue
        found = resolver.named_declaration(name)
        if found is None:
            continue
        kind, declaration = found
        target_span = (
            getattr(declaration, "name_origin", None)
            or getattr(declaration, "origin", None)
        )
        if target_span is None:
            continue
        target_unit = getattr(declaration, "source_identity", None)
        target = observations.declaration_origin(
            target_span,
            f"{kind} {base}",
            context,
            source_unit=target_unit,
        )
        occurrence = SourceOrigin(
            occurrence_span,
            f"{kind} {base}",
            context.scope.source_unit,
            context.scope.source_digest,
        )
        if any(
            item.occurrence == occurrence
            and item.target == target
            and item.name == base
            and item.kind == kind
            for item in context.services.tooling.definition_resolutions
        ):
            continue
        observations.record_definition(context, occurrence, target, name=base, kind=kind)


def record_type_syntax_definitions(
    context: semantic_context.ExpressionContext,
    syntax: ast.TypeSyntax,
    resolver: TypeResolver,
) -> None:
    """Record named types nested in one declaration's source type syntax."""

    if isinstance(syntax, ast.TypeName):
        record_named_type_definition(context, syntax, resolver)
    elif isinstance(syntax, ast.VectorTypeName):
        record_type_syntax_definitions(context, syntax.element_type, resolver)
    elif isinstance(syntax, ast.TupleTypeName):
        for element in syntax.elements:
            record_type_syntax_definitions(context, element, resolver)


def record_enum_member_definition(
    context: semantic_context.ExpressionContext,
    expression: ast.FieldExpr | ast.EnumMemberRef,
    enum_type: ir_types.EnumType,
    resolver: TypeResolver,
) -> None:
    """Record one compiler-resolved enum member occurrence."""

    if (
        not context.services.tooling.analysis_needs.wants(AnalysisNeeds.DEFINITIONS)
        or context.services.tooling.definition_resolutions is None
    ):
        return
    declaration = resolver._enum_declarations.get(enum_type.name)
    if declaration is None:
        return
    try:
        member_name = (
            expression.field
            if isinstance(expression, ast.FieldExpr)
            else expression.member
        )
        member_index = declaration.members.index(member_name)
    except ValueError:
        return
    member_span = (
        declaration.member_origins[member_index]
        if member_index < len(declaration.member_origins)
        else None
    )
    if member_span is None:
        return
    target = observations.declaration_origin(
        member_span,
        f"enum member {declaration.name}.{member_name}",
        context,
        source_unit=declaration.source_identity,
    )
    occurrence_span = (
        expression.member_origin or expression.origin
        if isinstance(expression, ast.FieldExpr)
        else expression.origin
    )
    occurrence = (
        SourceOrigin(
            occurrence_span,
            f"enum member {enum_type.name}.{member_name}",
            context.scope.source_unit,
            context.scope.source_digest,
        )
        if occurrence_span is not None
        else None
    )
    observations.record_definition(
        context,
        occurrence,
        target,
        name=member_name,
        kind="enum_member",
    )
