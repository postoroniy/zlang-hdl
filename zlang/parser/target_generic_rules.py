# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by TargetAndGenericRules."""

from __future__ import annotations

from dataclasses import replace

from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError
from zlang.source import SourceSpan

from .rules_support import _tagged


class TargetAndGenericRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    def compilation_unit(self, items: list[object]) -> ast_nodes.Module:
        modules = tuple(item for item in items if isinstance(item, ast_nodes.Module))
        module = (
            modules[-1]
            if modules
            else ast_nodes.Module(
                name="__declaration_unit__",
                ports=(),
                assignments=(),
                ordered_items=(),
                declaration_only=True,
            )
        )
        aliases = tuple(item for item in items if isinstance(item, ast_nodes.TypeAlias))
        enums = tuple(item for item in items if isinstance(item, ast_nodes.EnumDecl))
        tagged_unions = tuple(
            item for item in items if isinstance(item, ast_nodes.TaggedUnionDecl)
        )
        structs = tuple(item for item in items if isinstance(item, ast_nodes.StructDecl))
        functions = tuple(
            item for item in items if isinstance(item, ast_nodes.FunctionDecl)
        )
        operators = tuple(item for item in items if isinstance(item, ast_nodes.OperatorDecl))
        equivalences = tuple(item for item in items if isinstance(item, ast_nodes.EquivDecl))
        imports = tuple(item for item in items if isinstance(item, ast_nodes.ImportDecl))
        protocols = tuple(item for item in items if isinstance(item, ast_nodes.ProtocolDecl))
        module_interfaces = tuple(
            item for item in items if isinstance(item, ast_nodes.ModuleInterfaceDecl)
        )
        resources = tuple(item for item in items if isinstance(item, ast_nodes.ResourceDefinitionDecl))
        target_families = tuple(item for item in items if isinstance(item, ast_nodes.TargetFamilyDecl))
        target_instances = tuple(item for item in items if isinstance(item, ast_nodes.TargetInstanceDecl))
        architecture_templates = tuple(item for item in items if isinstance(item, ast_nodes.ArchitectureTemplateDecl))
        return replace(
            module, type_aliases=aliases, enums=enums, structs=structs, functions=functions,
            operators=operators,
            equivalences=equivalences,
            imports=imports, protocols=protocols,
            module_interfaces=module_interfaces,
            resource_definitions=resources,
            target_families=target_families,
            target_instances=target_instances,
            architecture_templates=architecture_templates,
            tagged_unions=tagged_unions,
            submodules=modules[:-1],
        )

    @v_args(meta=True)
    def import_decl(self, meta: object, items: list[object]) -> ast_nodes.ImportDecl:
        return ast_nodes.ImportDecl(
            str(items[0]),
            self._span(meta),
            str(items[1]) if len(items) > 1 and items[1] is not None else None,
        )

    def qualified_name(self, items: list[object]) -> str:
        return str(items[0])

    def resource_port(self, items: list[object]) -> tuple[str, ast_nodes.ResourcePortDecl]:
        return ("resource_port", ast_nodes.ResourcePortDecl(
            str(items[0]), str(items[1]), str(items[2]), self._parse_number(items[3])
        ))

    def resource_operation(self, items: list[object]) -> tuple[str, str]:
        return ("resource_operation", str(items[0]))

    def resource_class(self, items: list[object]) -> tuple[str, str]:
        return ("resource_class", str(items[0]))

    def resource_capability(self, items: list[object]) -> tuple[str, str, str]:
        return ("resource_capability", str(items[0]), str(items[1]))

    def resource_limit(self, items: list[object]) -> tuple[str, str, int]:
        return ("resource_limit", str(items[0]), self._parse_number(items[1]))

    def resource_register(self, items: list[object]) -> tuple[str, ast_nodes.ResourceRegisterSiteDecl]:
        return ("resource_register", ast_nodes.ResourceRegisterSiteDecl(
            str(items[0]), self._parse_number(items[1]), self._parse_number(items[2])
        ))

    def resource_pipeline_site(self, items: list[object]) -> tuple[str, ast_nodes.ResourcePipelineSiteDecl]:
        return ("resource_pipeline_site", ast_nodes.ResourcePipelineSiteDecl(
            str(items[0]), str(items[1]), self._parse_number(items[2]),
            self._parse_number(items[3]), str(items[4]) == "true",
            self._parse_number(items[5]),
        ))

    def pipeline_enable(self, items: list[object]) -> tuple[str, str]:
        return ("pipeline_enable", str(items[0]))

    def pipeline_setting(self, items: list[object]) -> tuple[str, str, int]:
        return ("pipeline_setting", str(items[0]), self._parse_number(items[1]))

    def resource_pipeline_config(self, items: list[object]) -> tuple[str, ast_nodes.ResourcePipelineConfigurationDecl]:
        name, latency, interval = str(items[0]), self._parse_number(items[1]), self._parse_number(items[2])
        return ("resource_pipeline_config", ast_nodes.ResourcePipelineConfigurationDecl(
            name,
            tuple(item[1] for item in items[3:] if _tagged(item, "pipeline_enable")),
            latency, interval,
            tuple((item[1], item[2]) for item in items[3:] if _tagged(item, "pipeline_setting")),
        ))

    def resource_dedicated(self, items: list[object]) -> tuple[str, ast_nodes.ResourceDedicatedLinkDecl]:
        return ("resource_dedicated", ast_nodes.ResourceDedicatedLinkDecl(
            str(items[0]), str(items[1]), str(items[2]),
            self._parse_number(items[3]), str(items[4]),
            len(items) > 5 and str(items[5]) == "true",
        ))

    def resource_binding(self, items: list[object]) -> tuple[str, str, str]:
        return ("resource_binding", str(items[0]), str(items[1]))

    def resource_physical_primitive(self, items: list[object]) -> tuple[str, str]:
        return ("resource_physical_primitive", str(items[0]))

    def resource_physical_site(self, items: list[object]) -> tuple[str, str, str]:
        return ("resource_physical_site", str(items[0]), str(items[1]))

    def resource_physical_edge(self, items: list[object]) -> tuple[str, str, str, str]:
        return ("resource_physical_edge", str(items[0]), str(items[1]), str(items[2]))

    @v_args(meta=True)
    def resource_decl(self, meta: object, items: list[object]) -> ast_nodes.ResourceDefinitionDecl:
        operation = tuple(item[1] for item in items[1:] if _tagged(item, "resource_operation"))
        if len(operation) != 1:
            raise ParseError(f"resource '{items[0]}' requires exactly one operation")
        return ast_nodes.ResourceDefinitionDecl(
            str(items[0]),
            tuple(item[1] for item in items[1:] if _tagged(item, "resource_port")),
            operation[0],
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "resource_limit")),
            tuple(item[1] for item in items[1:] if _tagged(item, "resource_register")),
            tuple(item[1] for item in items[1:] if _tagged(item, "resource_dedicated")),
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "resource_binding")),
            next((item[1] for item in items[1:] if _tagged(item, "resource_class")), "generic"),
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "resource_capability")),
            tuple(item[1] for item in items[1:] if _tagged(item, "resource_pipeline_site")),
            tuple(item[1] for item in items[1:] if _tagged(item, "resource_pipeline_config")),
            next((item[1] for item in items[1:] if _tagged(item, "resource_physical_primitive")), None),
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "resource_physical_site")),
            tuple((item[1], item[2], item[3]) for item in items[1:] if _tagged(item, "resource_physical_edge")),
            self._span(meta),
            self._token_span(items[0]),
        )

    @v_args(meta=True)
    def target_family_decl(self, meta: object, items: list[object]) -> ast_nodes.TargetFamilyDecl:
        return ast_nodes.TargetFamilyDecl(
            str(items[0]), tuple(str(item) for item in items[1:]),
            self._span(meta), self._token_span(items[0]),
            tuple(self._token_span(item) for item in items[1:]),
        )

    def target_part(self, items: list[object]) -> tuple[str, str]:
        return ("target_part", str(items[0]))

    def target_inventory(self, items: list[object]) -> tuple[str, str, int]:
        return ("target_inventory", str(items[0]), self._parse_number(items[1]))

    def target_dedicated_capacity(self, items: list[object]) -> tuple[str, str, str, int]:
        return ("target_dedicated", str(items[0]), str(items[1]), self._parse_number(items[2]))

    @v_args(meta=True)
    def target_instance_decl(self, meta: object, items: list[object]) -> ast_nodes.TargetInstanceDecl:
        parts = tuple(item[1] for item in items[2:] if _tagged(item, "target_part"))
        if len(parts) != 1:
            raise ParseError(f"device '{items[0]}' requires exactly one part")
        return ast_nodes.TargetInstanceDecl(
            str(items[0]), str(items[1]), parts[0],
            tuple((item[1], item[2]) for item in items[2:] if _tagged(item, "target_inventory")),
            tuple((item[1], item[2], item[3]) for item in items[2:] if _tagged(item, "target_dedicated")),
            self._span(meta),
        )

    def architecture_operation(self, items: list[object]) -> tuple[str, str]:
        return ("architecture_operation", str(items[0]))

    def architecture_resource(self, items: list[object]) -> tuple[str, str, int]:
        return (
            "architecture_resource", str(items[0]), self._parse_number(items[1]),
            self._token_span(items[0]),
        )

    def architecture_latency(self, items: list[object]) -> tuple[str, int]:
        return ("architecture_latency", self._parse_number(items[0]))

    def architecture_ii(self, items: list[object]) -> tuple[str, int]:
        return ("architecture_ii", self._parse_number(items[0]))

    def architecture_register(self, items: list[object]) -> tuple[str, str, int]:
        return ("architecture_register", str(items[0]), self._parse_number(items[1]))

    def architecture_pipeline(self, items: list[object]) -> tuple[str, str]:
        return ("architecture_pipeline", str(items[0]))

    def architecture_dedicated(self, items: list[object]) -> tuple[str, str]:
        return ("architecture_dedicated", str(items[0]))

    @v_args(meta=True)
    def architecture_template_decl(self, meta: object, items: list[object]) -> ast_nodes.ArchitectureTemplateDecl:
        operations = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_operation"))
        resources = tuple(
            (item[1], item[2], item[3])
            for item in items[1:] if _tagged(item, "architecture_resource")
        )
        latencies = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_latency"))
        intervals = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_ii"))
        if not (len(operations) == len(resources) == len(latencies) == len(intervals) == 1):
            raise ParseError(
                f"architecture '{items[0]}' requires one operation, resource, latency, and ii"
            )
        return ast_nodes.ArchitectureTemplateDecl(
            str(items[0]), operations[0], resources[0][0], resources[0][1],
            latencies[0], intervals[0],
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "architecture_register")),
            next((item[1] for item in items[1:] if _tagged(item, "architecture_pipeline")), None),
            next((item[1] for item in items[1:] if _tagged(item, "architecture_dedicated")), None),
            self._span(meta),
            self._token_span(items[0]),
            resources[0][2],
        )

    def protocol_role(self, items: list[object]) -> tuple[str, str]:
        return ("role", str(items[0]))

    def target_role_name(self, _items: list[object]) -> str:
        return "target"

    @staticmethod
    def _protocol_channel_item(
        items: list[object],
    ) -> tuple[str, ast_nodes.ProtocolChannelDecl]:
        domain = str(items[4]) if len(items) > 4 and items[4] is not None else None
        return ("channel", ast_nodes.ProtocolChannelDecl(str(items[0]), items[1], str(items[2]), str(items[3]), domain))

    def protocol_channel(self, items: list[object]) -> tuple[str, ast_nodes.ProtocolChannelDecl]:
        return self._protocol_channel_item(items)

    def protocol_member(self, items: list[object]) -> tuple[str, ast_nodes.ProtocolChannelDecl]:
        return self._protocol_channel_item(items)

    def protocol_decl(self, items: list[object]) -> ast_nodes.ProtocolDecl:
        name = str(items[0])
        parameters = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(p, ast_nodes.ModuleParameter) for p in item)),
            (),
        )
        body = [item for item in items[1:] if item is not parameters and item is not None]
        roles = tuple(item[1] for item in body if item[0] == "role")
        channels = tuple(item[1] for item in body if item[0] == "channel")
        return ast_nodes.ProtocolDecl(name, roles, channels, parameters)

    def parameter_number(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def parameter_name(self, items: list[object]) -> str:
        return str(items[0])

    def type_module_parameter(self, items: list[object]) -> ast_nodes.ModuleParameter:
        return ast_nodes.ModuleParameter(str(items[0]), "type")

    def constant_module_parameter(self, items: list[object]) -> ast_nodes.ModuleParameter:
        return ast_nodes.ModuleParameter(str(items[0]), "constant", type_name=items[1])

    def callable_parameter_types(self, items: list[object]) -> tuple[object, ...]:
        return tuple(items)

    def callable_parameter_type(self, items: list[object]) -> tuple[str, tuple[object, ...], object]:
        parameters = next((item for item in items if isinstance(item, tuple)), ())
        return_type = next(item for item in reversed(items) if not isinstance(item, tuple))
        return ("callable_type", parameters, return_type)

    def callable_module_parameter(self, items: list[object]) -> ast_nodes.ModuleParameter:
        signature = items[1]
        assert isinstance(signature, tuple) and signature[0] == "callable_type"
        return ast_nodes.ModuleParameter(
            str(items[0]),
            "callable",
            callable_parameters=tuple(signature[1]),
            callable_return_type=signature[2],
        )

    def value_module_parameter(self, items: list[object]) -> ast_nodes.ModuleParameter:
        value = items[1] if len(items) > 1 else None
        if isinstance(value, str):
            try:
                value = self._parse_number(value)
            except (ValueError, ParseError):
                pass
        return ast_nodes.ModuleParameter(str(items[0]), "value", value)

    def signed_parameter_number(self, items: list[object]) -> int:
        value = self._parse_number(str(items[1]))
        return -value if str(items[0]) == "-" else value

    def module_parameters(self, items: list[object]) -> tuple[ast_nodes.ModuleParameter, ...]:
        return tuple(items)

    def named_specialization_argument(self, items: list[object]) -> ast_nodes.SpecializationArgument:
        return ast_nodes.SpecializationArgument(str(items[0]), items[1])

    def positional_specialization_argument(self, items: list[object]) -> ast_nodes.SpecializationArgument:
        return ast_nodes.SpecializationArgument(None, items[0])

    def specialization_arguments(self, items: list[object]) -> tuple[ast_nodes.SpecializationArgument, ...]:
        return tuple(items)

    @v_args(meta=True)
    def instance_decl(self, meta: object, items: list[object]) -> ast_nodes.InstanceDecl:
        name = str(items[0])
        array_length = next(
            (item[1] for item in items[1:] if _tagged(item, "array_length")),
            None,
        )
        module = str(next(
            item for item in items[1:]
            if isinstance(item, str)
        ))
        arguments = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)),
            (),
        )
        bindings = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(binding, ast_nodes.Assignment) for binding in item)),
            (),
        )
        return ast_nodes.InstanceDecl(
            name,
            module,
            arguments,
            array_length,
            bindings,
            self._span(meta),
            self._token_span(items[0]),
            self._token_span(next(
                item for item in items[1:] if isinstance(item, str)
            )),
        )

    def csr_declaration_name(self, _items: list[object]) -> str:
        # ``csr`` is a contextual declaration keyword, but remains a legal
        # instance identifier when the following colon makes the category-
        # neutral declaration unambiguous.
        return "csr"

    def generic_initializer(self, items: list[object]) -> tuple[object, ...]:
        return ("generic_initializer", items[0])

    def generic_protocol(self, items: list[object]) -> tuple[object, ...]:
        return (
            "generic_protocol", str(items[0]),
            str(items[1]) if len(items) > 1 and items[1] is not None else None,
        )

    def generic_bindings(self, items: list[object]) -> tuple[object, ...]:
        return ("generic_bindings", items[0])

    def generic_empty(self, _items: list[object]) -> tuple[object, ...]:
        return ("generic_empty",)

    def _concise_reference_parts(
        self, type_name: ast_nodes.TypeName | ast_nodes.VectorTypeName | ast_nodes.TupleTypeName
    ) -> tuple[ast_nodes.TypeName | ast_nodes.VectorTypeName | ast_nodes.TupleTypeName, tuple[ast_nodes.SpecializationArgument, ...]]:
        """Recover named actuals swallowed by ``GENERIC_TYPE_VALUE``.

        The generic nominal token intentionally keeps nested type spellings
        intact.  In a category-neutral declaration that also means a spelling
        such as ``Child<T=u8,N=2>`` arrives as one ``TypeName``.  Split only
        top-level separators here; nested generic types remain untouched.
        Positional references continue through the established semantic
        fallback so their parser behavior is unchanged.
        """

        if not isinstance(type_name, ast_nodes.TypeName):
            return type_name, ()
        text = type_name.text
        if "<" not in text or not text.endswith(">"):
            return type_name, ()
        base, body = text.split("<", 1)
        body = body[:-1]
        fields: list[str] = []
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
                fields.append(body[start:index].strip())
                start = index + 1
        fields.append(body[start:].strip())

        parsed: list[ast_nodes.SpecializationArgument] = []
        for field in fields:
            angle_depth = 0
            paren_depth = 0
            separator = None
            for index, character in enumerate(field):
                if character == "<":
                    angle_depth += 1
                elif character == ">":
                    angle_depth -= 1
                elif character == "(":
                    paren_depth += 1
                elif character == ")":
                    paren_depth -= 1
                elif character == "=" and angle_depth == 0 and paren_depth == 0:
                    separator = index
                    break
            if separator is None:
                # This is an ordinary positional type reference.  Preserve
                # the existing TypeName path and semantic parser exactly.
                return type_name, ()
            name = field[:separator].strip()
            value_text = field[separator + 1 :].strip()
            if not name or not value_text:
                return type_name, ()
            try:
                value: object = self._parse_number(value_text)
            except (ValueError, ParseError):
                value = value_text
            parsed.append(ast_nodes.SpecializationArgument(name, value))
        return ast_nodes.TypeName(base), tuple(parsed)

    def _root_action_chain(
        self,
        guard: object,
        block: object,
        remaining: list[object],
        origin: SourceSpan | None,
    ) -> tuple[object, tuple[object, ...]]:
        """Return one rule guard/action list for a root ``when`` chain.

        The compatibility shape is retained when no ``else`` follows.  A root
        chain must be schedulable even when its first condition is false, so
        it becomes one always-enabled rule containing one conditional action.
        The comparison is an ordinary, constant-foldable bit expression; no
        boolean literal or backend-only node is introduced.
        """

        actions = self._action_block(block)
        alternative = next(
            (item[1] for item in remaining if _tagged(item, "conditional_else")),
            None,
        )
        if alternative is None:
            return guard, actions
        always = ast_nodes.BinaryExpr(
            ast_nodes.BinaryOperator.EQUAL,
            ast_nodes.NumberExpr(0, origin=origin),
            ast_nodes.NumberExpr(0, origin=origin),
            origin=origin,
        )
        return always, (ast_nodes.ConditionalAction(guard, actions, alternative, origin),)

    @v_args(meta=True)
    def concise_rule_body(
        self, meta: object, items: list[object]
    ) -> tuple[object, ...]:
        guard, actions = self._root_action_chain(
            items[0], items[1], items[2:], self._span(meta)
        )
        if not actions:
            raise ParseError("concise atomic rule cannot be empty")
        return (
            "concise_rule",
            guard,
            actions,
        )

    @v_args(meta=True)
    def generic_type_body(self, meta: object, items: list[object]) -> tuple[object, ...]:
        type_origin = items[0].origin
        return ("generic_type", items[0], items[1], type_origin or self._span(meta))

    def instance_array_length(self, items: list[object]) -> tuple[str, object]:
        return ("array_length", items[0])

    @v_args(meta=True)
    def generic_decl(self, meta: object, items: list[object]) -> object:
        name = str(items[0])
        array_length = next((item[1] for item in items[1:] if _tagged(item, "array_length")), None)
        body = items[-1]
        if _tagged(body, "concise_rule"):
            if array_length is not None:
                raise ParseError("concise atomic rules cannot be instance arrays")
            return ast_nodes.RuleDecl(name, body[1], body[2], self._span(meta))
        if not _tagged(body, "generic_type"):
            raise ParseError("invalid concise declaration body")
        type_name = body[1]
        type_name, specializations = self._concise_reference_parts(type_name)
        tail = body[2]
        role = tail[1] if _tagged(tail, "generic_protocol") else None
        domain = tail[2] if _tagged(tail, "generic_protocol") else None
        bindings = tail[1] if _tagged(tail, "generic_bindings") else ()
        initializer = tail[1] if _tagged(tail, "generic_initializer") else None
        return ast_nodes.GenericDeclaration(
            name=name,
            type_name=type_name,
            role=role,
            domain=domain,
            array_length=array_length,
            bindings=bindings,
            initializer=initializer,
            specializations=specializations,
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
            type_origin=body[3],
        )

    def hierarchical_target(self, items: list[object]) -> str:
        result = str(items[0])
        for item in items[1:]:
            if _tagged(item, "target_member"):
                result += "." + str(item[1])
            elif _tagged(item, "target_index"):
                result += "[" + str(item[1]) + "]"
            else:
                result += "." + str(item)
        return result

    def target_member(self, items: list[object]) -> tuple[str, object]:
        return ("target_member", items[0])

    def target_index(self, items: list[object]) -> tuple[str, object]:
        return ("target_index", items[0])

    def instance_input_binding(self, items: list[object]) -> ast_nodes.Assignment:
        expression = (
            items[1]
            if len(items) > 1 and items[1] is not None
            else ast_nodes.NameExpr(str(items[0]))
        )
        return ast_nodes.Assignment(str(items[0]), expression)

    def instance_binding_block(self, items: list[object]) -> tuple[ast_nodes.Assignment, ...]:
        return tuple(items)

    @v_args(meta=True)
    def generate_instances(self, meta: object, items: list[object]) -> ast_nodes.GenerateBlock:
        index, start, stop = items[0]
        return ast_nodes.GenerateBlock(index, start, stop, tuple(items[1:]), self._span(meta))

    @v_args(meta=True)
    def structural_generate_if(
        self, meta: object, items: list[object]
    ) -> ast_nodes.CompileTimeIfDecl:
        condition = items[0]
        true_items = tuple(
            item for item in items[1:]
            if item is not None
            and not _tagged(item, "structural_generate_else")
        )
        false = next(
            (
                item[1] for item in items[1:]
                if _tagged(item, "structural_generate_else")
            ),
            (),
        )
        return ast_nodes.CompileTimeIfDecl(
            condition, true_items, tuple(false), self._span(meta)
        )

    def structural_generate_else(
        self, items: list[object]
    ) -> tuple[str, tuple[object, ...]]:
        return ("structural_generate_else", tuple(items))

    @v_args(meta=True)
    def compile_time_if_decl(self, meta: object, items: list[object]) -> ast_nodes.CompileTimeIfDecl:
        condition = items[0]
        true_items = tuple(item for item in items[1:] if not _tagged(item, "compile_time_else_decl"))
        false = next(
            (item[1] for item in items[1:] if _tagged(item, "compile_time_else_decl")),
            (),
        )
        return ast_nodes.CompileTimeIfDecl(condition, true_items, tuple(false), self._span(meta))

    def compile_time_else_decl(self, items: list[object]) -> tuple[str, tuple[object, ...]]:
        return ("compile_time_else_decl", tuple(items))

    def compile_time_if_expr(self, items: list[object]) -> ast_nodes.CompileTimeIfExpr:
        condition = items[0]
        true = items[1]
        false = next(
            (item[1] for item in items[2:] if _tagged(item, "compile_time_else_expr")),
            None,
        )
        return ast_nodes.CompileTimeIfExpr(condition, true, false)

    def compile_time_else_expr(self, items: list[object]) -> tuple[str, object]:
        return ("compile_time_else_expr", items[0])


__all__ = ["TargetAndGenericRules"]
