"""Parse ZLang source into a syntax-only AST."""

from __future__ import annotations

from dataclasses import replace
from functools import cache
from fractions import Fraction
import re
from importlib.resources import files
from threading import Lock

from lark import Lark, Transformer, UnexpectedInput, v_args
from lark.exceptions import VisitError

from zlang.ast.nodes import (
    AddExpr,
    AggregateInterfaceDecl,
    ArbiterDecl,
    ArbitrationPolicy,
    Assignment,
    BinaryExpr,
    BinaryOperator,
    BitcastExpr,
    CharLiteralExpr,
    CallExpr,
    ClockPhysicalDecl,
    ConcatExpr,
    ConnectionAdapter,
    ConnectionChainDecl,
    ConnectionDecl,
    CompileTimeIfDecl,
    CompileTimeIfExpr,
    ContractDecl,
    EquivDecl,
    EquivGuard,
    ContractKind,
    VerificationGoalDecl,
    VerificationGoalKind,
    VerificationRequirementDecl,
    VerificationScopeDecl,
    CostConstraint,
    CostMetric,
    CostPolicy,
    CollectionSumExpr,
    ConditionalAction,
    Crossing,
    CrossingKind,
    CsrAccess,
    CsrBinding,
    CsrBindingKind,
    CsrBlockDecl,
    CsrFieldDecl,
    CsrPriority,
    CsrRegisterDecl,
    DelayExpr,
    Direction,
    DotExpr,
    EnumDecl,
    EnumMemberRef,
    FixedOverflowMode,
    FixedRoundingMode,
    ExplorationConstraint,
    ExplorationObjective,
    ExplorationRelation,
    FieldExpr,
    FifoDecl,
    FsmDecl,
    FsmStateDecl,
    FsmTransitionDecl,
    FunctionDecl,
    OperatorDecl,
    ImportDecl,
    GrantScope,
    GenerateExpr,
    GenericDeclaration,
    GenerateBlock,
    InterfaceKind,
    InterfaceTypeName,
    IndexExpr,
    IndexedAssignmentTarget,
    IndexedSumExpr,
    ImplementExpr,
    ImplementationArm,
    ImplementationChoiceExpr,
    ImplementationKind,
    Module,
    ModuleInterfaceDecl,
    ModuleInterfaceRef,
    ModuleTimingDecl,
    ProtocolChannelDecl,
    ProtocolDecl,
    ModuleParameter,
    CallableRef,
    SpecializationArgument,
    InstanceDecl,
    StructFieldValue,
    StructConstructExpr,
    StructDestructureDecl,
    StructUpdateExpr,
    StringLiteralExpr,
    TupleDestructureDecl,
    TupleLiteralExpr,
    TupleTypeName,
    MuxExpr,
    MemoryCollision,
    MemoryDecl,
    MemoryResetPolicy,
    MapExpr,
    NameExpr,
    NumberExpr,
    QuantizeExpr,
    PackExpr,
    RationalExpr,
    PatternConstantExpr,
    PatternConstantKind,
    NextAssignment,
    Parameter,
    PipelineExpr,
    ProtocolTransformExpr,
    PipelineConstraint,
    PipelineMetric,
    PipelineRelation,
    PortDecl,
    PriorityBlockDecl,
    PriorityRuleArm,
    ResizeExpr,
    ResizeKind,
    RequestResponseDecl,
    RequestResponseOrdering,
    ReshapeExpr,
    ResourceAction,
    ResourcePortDecl,
    ResourceRegisterSiteDecl,
    ResourceDedicatedLinkDecl,
    ResourceDefinitionDecl,
    ResourcePipelineConfigurationDecl,
    ResourcePipelineSiteDecl,
    TargetFamilyDecl,
    TargetInstanceDecl,
    ArchitectureTemplateDecl,
    AnonymousRuleDecl,
    RuleDecl,
    RulePriority,
    RulePriorityChain,
    RegisterDecl,
    ResetPhysicalDecl,
    RomDecl,
    ReduceExpr,
    ReductionOperator,
    SwitchArm,
    SwitchExpr,
    SliceExpr,
    VectorRangeExpr,
    StructDecl,
    StructFieldDecl,
    TaggedUnionConstructExpr,
    TaggedUnionDecl,
    TaggedUnionFieldDecl,
    TaggedUnionMatchArm,
    TaggedUnionMatchExpr,
    TaggedUnionVariantDecl,
    SynthesisFeedback,
    TypeAlias,
    TypeName,
    TypeValueExpr,
    UnpackExpr,
    UnaryExpr,
    VectorTypeName,
    VectorLiteralExpr,
)
from zlang.source import SourceSpan
from zlang.diagnostics import DiagnosticError


class ParseError(DiagnosticError):
    """A source file does not conform to the ZLang grammar."""

    default_code = "ZL-PARSE-001"


def _cost_metric(value: str) -> CostMetric:
    return CostMetric.FMAX_EST if value == "fmax" else CostMetric(value)


class _AstBuilder(Transformer):
    def compilation_unit(self, items: list[object]) -> Module:
        modules = tuple(item for item in items if isinstance(item, Module))
        module = (
            modules[-1]
            if modules
            else Module(
                name="__declaration_unit__",
                ports=(),
                assignments=(),
                declaration_only=True,
            )
        )
        aliases = tuple(item for item in items if isinstance(item, TypeAlias))
        enums = tuple(item for item in items if isinstance(item, EnumDecl))
        tagged_unions = tuple(
            item for item in items if isinstance(item, TaggedUnionDecl)
        )
        structs = tuple(item for item in items if isinstance(item, StructDecl))
        functions = tuple(
            item for item in items if isinstance(item, FunctionDecl)
        )
        operators = tuple(item for item in items if isinstance(item, OperatorDecl))
        equivalences = tuple(item for item in items if isinstance(item, EquivDecl))
        imports = tuple(item for item in items if isinstance(item, ImportDecl))
        protocols = tuple(item for item in items if isinstance(item, ProtocolDecl))
        module_interfaces = tuple(
            item for item in items if isinstance(item, ModuleInterfaceDecl)
        )
        resources = tuple(item for item in items if isinstance(item, ResourceDefinitionDecl))
        target_families = tuple(item for item in items if isinstance(item, TargetFamilyDecl))
        target_instances = tuple(item for item in items if isinstance(item, TargetInstanceDecl))
        architecture_templates = tuple(item for item in items if isinstance(item, ArchitectureTemplateDecl))
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
    def import_decl(self, meta: object, items: list[object]) -> ImportDecl:
        return ImportDecl(
            str(items[0]),
            self._span(meta),
            str(items[1]) if len(items) > 1 and items[1] is not None else None,
        )

    def qualified_name(self, items: list[object]) -> str:
        return str(items[0])

    def resource_port(self, items: list[object]) -> tuple[str, ResourcePortDecl]:
        return ("resource_port", ResourcePortDecl(
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

    def resource_register(self, items: list[object]) -> tuple[str, ResourceRegisterSiteDecl]:
        return ("resource_register", ResourceRegisterSiteDecl(
            str(items[0]), self._parse_number(items[1]), self._parse_number(items[2])
        ))

    def resource_pipeline_site(self, items: list[object]) -> tuple[str, ResourcePipelineSiteDecl]:
        return ("resource_pipeline_site", ResourcePipelineSiteDecl(
            str(items[0]), str(items[1]), self._parse_number(items[2]),
            self._parse_number(items[3]), str(items[4]) == "true",
            self._parse_number(items[5]),
        ))

    def pipeline_enable(self, items: list[object]) -> tuple[str, str]:
        return ("pipeline_enable", str(items[0]))

    def pipeline_setting(self, items: list[object]) -> tuple[str, str, int]:
        return ("pipeline_setting", str(items[0]), self._parse_number(items[1]))

    def resource_pipeline_config(self, items: list[object]) -> tuple[str, ResourcePipelineConfigurationDecl]:
        name, latency, interval = str(items[0]), self._parse_number(items[1]), self._parse_number(items[2])
        return ("resource_pipeline_config", ResourcePipelineConfigurationDecl(
            name,
            tuple(item[1] for item in items[3:] if _tagged(item, "pipeline_enable")),
            latency, interval,
            tuple((item[1], item[2]) for item in items[3:] if _tagged(item, "pipeline_setting")),
        ))

    def resource_dedicated(self, items: list[object]) -> tuple[str, ResourceDedicatedLinkDecl]:
        return ("resource_dedicated", ResourceDedicatedLinkDecl(
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
    def resource_decl(self, meta: object, items: list[object]) -> ResourceDefinitionDecl:
        operation = tuple(item[1] for item in items[1:] if _tagged(item, "resource_operation"))
        if len(operation) != 1:
            raise ParseError(f"resource '{items[0]}' requires exactly one operation")
        return ResourceDefinitionDecl(
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
        )

    @v_args(meta=True)
    def target_family_decl(self, meta: object, items: list[object]) -> TargetFamilyDecl:
        return TargetFamilyDecl(str(items[0]), tuple(str(item) for item in items[1:]), self._span(meta))

    def target_part(self, items: list[object]) -> tuple[str, str]:
        return ("target_part", str(items[0]))

    def target_inventory(self, items: list[object]) -> tuple[str, str, int]:
        return ("target_inventory", str(items[0]), self._parse_number(items[1]))

    def target_dedicated_capacity(self, items: list[object]) -> tuple[str, str, str, int]:
        return ("target_dedicated", str(items[0]), str(items[1]), self._parse_number(items[2]))

    @v_args(meta=True)
    def target_instance_decl(self, meta: object, items: list[object]) -> TargetInstanceDecl:
        parts = tuple(item[1] for item in items[2:] if _tagged(item, "target_part"))
        if len(parts) != 1:
            raise ParseError(f"device '{items[0]}' requires exactly one part")
        return TargetInstanceDecl(
            str(items[0]), str(items[1]), parts[0],
            tuple((item[1], item[2]) for item in items[2:] if _tagged(item, "target_inventory")),
            tuple((item[1], item[2], item[3]) for item in items[2:] if _tagged(item, "target_dedicated")),
            self._span(meta),
        )

    def architecture_operation(self, items: list[object]) -> tuple[str, str]:
        return ("architecture_operation", str(items[0]))

    def architecture_resource(self, items: list[object]) -> tuple[str, str, int]:
        return ("architecture_resource", str(items[0]), self._parse_number(items[1]))

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
    def architecture_template_decl(self, meta: object, items: list[object]) -> ArchitectureTemplateDecl:
        operations = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_operation"))
        resources = tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "architecture_resource"))
        latencies = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_latency"))
        intervals = tuple(item[1] for item in items[1:] if _tagged(item, "architecture_ii"))
        if not (len(operations) == len(resources) == len(latencies) == len(intervals) == 1):
            raise ParseError(
                f"architecture '{items[0]}' requires one operation, resource, latency, and ii"
            )
        return ArchitectureTemplateDecl(
            str(items[0]), operations[0], resources[0][0], resources[0][1],
            latencies[0], intervals[0],
            tuple((item[1], item[2]) for item in items[1:] if _tagged(item, "architecture_register")),
            next((item[1] for item in items[1:] if _tagged(item, "architecture_pipeline")), None),
            next((item[1] for item in items[1:] if _tagged(item, "architecture_dedicated")), None),
            self._span(meta),
        )

    def protocol_role(self, items: list[object]) -> tuple[str, str]:
        return ("role", str(items[0]))

    def target_role_name(self, items: list[object]) -> str:
        return "target"

    @staticmethod
    def _protocol_channel_item(
        items: list[object],
    ) -> tuple[str, ProtocolChannelDecl]:
        domain = str(items[4]) if len(items) > 4 and items[4] is not None else None
        return ("channel", ProtocolChannelDecl(str(items[0]), items[1], str(items[2]), str(items[3]), domain))

    def protocol_channel(self, items: list[object]) -> tuple[str, ProtocolChannelDecl]:
        return self._protocol_channel_item(items)

    def protocol_member(self, items: list[object]) -> tuple[str, ProtocolChannelDecl]:
        return self._protocol_channel_item(items)

    def protocol_decl(self, items: list[object]) -> ProtocolDecl:
        name = str(items[0])
        parameters = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(p, ModuleParameter) for p in item)),
            (),
        )
        body = [item for item in items[1:] if item is not parameters and item is not None]
        roles = tuple(item[1] for item in body if item[0] == "role")
        channels = tuple(item[1] for item in body if item[0] == "channel")
        return ProtocolDecl(name, roles, channels, parameters)

    def parameter_number(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def parameter_name(self, items: list[object]) -> str:
        return str(items[0])

    def type_module_parameter(self, items: list[object]) -> ModuleParameter:
        return ModuleParameter(str(items[0]), "type")

    def constant_module_parameter(self, items: list[object]) -> ModuleParameter:
        return ModuleParameter(str(items[0]), "constant", type_name=items[1])

    def callable_parameter_types(self, items: list[object]) -> tuple[object, ...]:
        return tuple(items)

    def callable_parameter_type(self, items: list[object]) -> tuple[str, tuple[object, ...], object]:
        parameters = next((item for item in items if isinstance(item, tuple)), ())
        return_type = next(item for item in reversed(items) if not isinstance(item, tuple))
        return ("callable_type", parameters, return_type)

    def callable_module_parameter(self, items: list[object]) -> ModuleParameter:
        signature = items[1]
        assert isinstance(signature, tuple) and signature[0] == "callable_type"
        return ModuleParameter(
            str(items[0]),
            "callable",
            callable_parameters=tuple(signature[1]),
            callable_return_type=signature[2],
        )

    def value_module_parameter(self, items: list[object]) -> ModuleParameter:
        value = items[1] if len(items) > 1 else None
        if isinstance(value, str):
            try:
                value = self._parse_number(value)
            except (ValueError, ParseError):
                pass
        return ModuleParameter(str(items[0]), "value", value)

    def signed_parameter_number(self, items: list[object]) -> int:
        value = self._parse_number(str(items[1]))
        return -value if str(items[0]) == "-" else value

    def module_parameters(self, items: list[object]) -> tuple[ModuleParameter, ...]:
        return tuple(items)

    def named_specialization_argument(self, items: list[object]) -> SpecializationArgument:
        return SpecializationArgument(str(items[0]), items[1])

    def positional_specialization_argument(self, items: list[object]) -> SpecializationArgument:
        return SpecializationArgument(None, items[0])

    def specialization_arguments(self, items: list[object]) -> tuple[SpecializationArgument, ...]:
        return tuple(items)

    def instance_decl(self, items: list[object]) -> InstanceDecl:
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
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(arg, SpecializationArgument) for arg in item)),
            (),
        )
        bindings = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(binding, Assignment) for binding in item)),
            (),
        )
        return InstanceDecl(name, module, arguments, array_length, bindings)

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
        self, type_name: TypeName | VectorTypeName | TupleTypeName
    ) -> tuple[TypeName | VectorTypeName | TupleTypeName, tuple[SpecializationArgument, ...]]:
        """Recover named actuals swallowed by ``GENERIC_TYPE_VALUE``.

        The generic nominal token intentionally keeps nested type spellings
        intact.  In a category-neutral declaration that also means a spelling
        such as ``Child<T=u8,N=2>`` arrives as one ``TypeName``.  Split only
        top-level separators here; nested generic types remain untouched.
        Positional references continue through the established semantic
        fallback so their parser behavior is unchanged.
        """

        if not isinstance(type_name, TypeName):
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

        parsed: list[SpecializationArgument] = []
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
            parsed.append(SpecializationArgument(name, value))
        return TypeName(base), tuple(parsed)

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
        always = BinaryExpr(
            BinaryOperator.EQUAL,
            NumberExpr(0, origin=origin),
            NumberExpr(0, origin=origin),
            origin=origin,
        )
        return always, (ConditionalAction(guard, actions, alternative, origin),)

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

    def generic_type_body(self, items: list[object]) -> tuple[object, ...]:
        return ("generic_type", items[0], items[1])

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
            return RuleDecl(name, body[1], body[2], self._span(meta))
        if not _tagged(body, "generic_type"):
            raise ParseError("invalid concise declaration body")
        type_name = body[1]
        type_name, specializations = self._concise_reference_parts(type_name)
        tail = body[2]
        role = tail[1] if _tagged(tail, "generic_protocol") else None
        domain = tail[2] if _tagged(tail, "generic_protocol") else None
        bindings = tail[1] if _tagged(tail, "generic_bindings") else ()
        initializer = tail[1] if _tagged(tail, "generic_initializer") else None
        return GenericDeclaration(
            name=name,
            type_name=type_name,
            role=role,
            domain=domain,
            array_length=array_length,
            bindings=bindings,
            initializer=initializer,
            specializations=specializations,
            origin=self._span(meta),
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

    def instance_input_binding(self, items: list[object]) -> Assignment:
        expression = (
            items[1]
            if len(items) > 1 and items[1] is not None
            else NameExpr(str(items[0]))
        )
        return Assignment(str(items[0]), expression)

    def instance_binding_block(self, items: list[object]) -> tuple[Assignment, ...]:
        return tuple(items)

    @v_args(meta=True)
    def generate_instances(self, meta: object, items: list[object]) -> GenerateBlock:
        index, start, stop = items[0]
        return GenerateBlock(index, start, stop, tuple(items[1:]), self._span(meta))

    @v_args(meta=True)
    def compile_time_if_decl(self, meta: object, items: list[object]) -> CompileTimeIfDecl:
        condition = items[0]
        true_items = tuple(item for item in items[1:] if not _tagged(item, "compile_time_else_decl"))
        false = next(
            (item[1] for item in items[1:] if _tagged(item, "compile_time_else_decl")),
            (),
        )
        return CompileTimeIfDecl(condition, true_items, tuple(false), self._span(meta))

    def compile_time_else_decl(self, items: list[object]) -> tuple[str, tuple[object, ...]]:
        return ("compile_time_else_decl", tuple(items))

    def compile_time_if_expr(self, items: list[object]) -> CompileTimeIfExpr:
        condition = items[0]
        true = items[1]
        false = next(
            (item[1] for item in items[2:] if _tagged(item, "compile_time_else_expr")),
            None,
        )
        return CompileTimeIfExpr(condition, true, false)

    def compile_time_else_expr(self, items: list[object]) -> tuple[str, object]:
        return ("compile_time_else_expr", items[0])

    @v_args(meta=True)
    def type_condition_expr(self, meta: object, items: list[object]) -> BinaryExpr:
        match = re.fullmatch(
            r"([A-Z][A-Za-z0-9_]*)[ \t]*(==|!=)[ \t]*"
            r"([A-Z][A-Za-z0-9_]*(?:<[^{}\n]+>)?)",
            str(items[0]),
        )
        if match is None:
            raise ValueError("malformed nominal type condition")
        left = TypeValueExpr(TypeName(match.group(1)), origin=self._span(meta))
        right = TypeValueExpr(TypeName(match.group(3)), origin=self._span(meta))
        return BinaryExpr(
            BinaryOperator(match.group(2)), left, right, origin=self._span(meta)
        )

    def explicit_struct_field_value(self, items: list[object]) -> StructFieldValue:
        return StructFieldValue(str(items[0]), items[1])

    def shorthand_struct_field_value(self, items: list[object]) -> StructFieldValue:
        return StructFieldValue(str(items[0]), None)

    @v_args(meta=True)
    def struct_construct_expr(self, meta: object, items: list[object]) -> StructConstructExpr:
        return StructConstructExpr(str(items[0]), tuple(items[1:]), origin=self._span(meta))

    @v_args(meta=True)
    def qualified_struct_construct_expr(
        self, meta: object, items: list[object]
    ) -> StructConstructExpr:
        qualified_name = str(items[0])
        return StructConstructExpr(
            qualified_name,
            tuple(items[1:]),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def struct_update_expr(self, meta: object, items: list[object]) -> StructUpdateExpr:
        return StructUpdateExpr(items[0], tuple(items[1:]), origin=self._span(meta))

    @v_args(meta=True)
    def vector_literal_expr(self, meta: object, items: list[object]) -> VectorLiteralExpr:
        elements = items[0] if items and isinstance(items[0], tuple) else tuple(items)
        return VectorLiteralExpr(tuple(elements), origin=self._span(meta))

    @staticmethod
    def _decode_byte_literal(token: object) -> tuple[int, ...]:
        """Decode one already lexically validated ASCII/byte literal."""

        text = str(token)
        body = text[1:-1]
        values: list[int] = []
        index = 0
        escapes = {
            "0": 0,
            "n": 0x0A,
            "r": 0x0D,
            "t": 0x09,
            "\\": 0x5C,
            "'": 0x27,
            '"': 0x22,
        }
        while index < len(body):
            character = body[index]
            if character != "\\":
                values.append(ord(character))
                index += 1
                continue
            escape = body[index + 1]
            if escape == "x":
                values.append(int(body[index + 2 : index + 4], 16))
                index += 4
                continue
            values.append(escapes[escape])
            index += 2
        return tuple(values)

    @v_args(meta=True)
    def char_literal_expr(
        self, meta: object, items: list[object]
    ) -> CharLiteralExpr:
        values = self._decode_byte_literal(items[0])
        if len(values) != 1:  # Defensive: the token grammar enforces this.
            raise ParseError("character literal must contain exactly one byte")
        return CharLiteralExpr(values[0], origin=self._span(meta))

    @v_args(meta=True)
    def string_literal_expr(
        self, meta: object, items: list[object]
    ) -> StringLiteralExpr:
        return StringLiteralExpr(
            self._decode_byte_literal(items[0]), origin=self._span(meta)
        )

    def destructure_field_list(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def struct_destructure_decl(
        self,
        meta: object,
        items: list[object],
    ) -> StructDestructureDecl:
        return StructDestructureDecl(
            items[0], tuple(items[1]), items[2], origin=self._span(meta)
        )

    @v_args(meta=True)
    def tuple_destructure_decl(
        self, meta: object, items: list[object]
    ) -> TupleDestructureDecl:
        names = tuple(items[1])
        if not 2 <= len(names) <= 8:
            raise ParseError("tuple destructuring requires between 2 and 8 names")
        if "_" in names:
            raise ParseError(
                "tuple wildcard '_' is not supported; bind every component "
                "to a fresh name"
            )
        invalid = next(
            (name for name in names if not _ordinary_binding_name_is_valid(name)),
            None,
        )
        if invalid is not None:
            raise ParseError(
                f"tuple binding '{invalid}' is not a legal immutable binding name"
            )
        return TupleDestructureDecl(names, items[2], origin=self._span(meta))

    def tuple_destructure_names(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def tuple_destructure_name_rhs(
        self, meta: object, items: list[object]
    ) -> NameExpr:
        return NameExpr(str(items[0]), origin=self._span(meta))

    def type_alias(self, items: list[object]) -> TypeAlias:
        return TypeAlias(str(items[0]), items[1])

    def enum_member(self, items: list[object]) -> tuple[str, int | None]:
        return (
            str(items[0]),
            self._parse_number(items[1])
            if len(items) > 1 and items[1] is not None
            else None,
        )

    @v_args(meta=True)
    def enum_decl(self, meta: object, items: list[object]) -> EnumDecl:
        backing_type = next(
            (item for item in items[1:] if isinstance(item, (TypeName, VectorTypeName, TupleTypeName))),
            None,
        )
        members = tuple(
            item for item in items[1:]
            if isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and (item[1] is None or isinstance(item[1], int))
        )
        return EnumDecl(
            str(items[0]), tuple(item[0] for item in members),
            origin=self._span(meta),
            backing_type=backing_type,
            encodings=tuple(item[1] for item in members),
        )

    def tagged_union_field(self, items: list[object]) -> TaggedUnionFieldDecl:
        return TaggedUnionFieldDecl(str(items[0]), items[1])

    def tagged_union_variant(self, items: list[object]) -> TaggedUnionVariantDecl:
        return TaggedUnionVariantDecl(
            str(items[0]),
            tuple(item for item in items[1:] if isinstance(item, TaggedUnionFieldDecl)),
        )

    @v_args(meta=True)
    def tagged_union_decl(
        self, meta: object, items: list[object]
    ) -> TaggedUnionDecl:
        return TaggedUnionDecl(
            str(items[0]),
            tuple(item for item in items[1:] if isinstance(item, TaggedUnionVariantDecl)),
            origin=self._span(meta),
        )

    def struct_field(self, items: list[object]) -> StructFieldDecl:
        return StructFieldDecl(str(items[0]), items[1])

    def struct_decl(self, items: list[object]) -> StructDecl:
        parameters = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(p, ModuleParameter) for p in item)),
            (),
        )
        fields = tuple(item for item in items[1:] if isinstance(item, StructFieldDecl))
        return StructDecl(str(items[0]), fields, parameters)

    def protocol_endpoint_ref(self, items: list[object]) -> tuple[str, tuple[SpecializationArgument, ...], str, str | None]:
        arguments = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(arg, SpecializationArgument) for arg in item)),
            (),
        )
        names = [str(item) for item in items[1:] if isinstance(item, str)]
        role = names[0]
        domain = names[1] if len(names) > 1 else None
        return (str(items[0]), arguments, role, domain)

    def aggregate_interface_decl(self, items: list[object]) -> AggregateInterfaceDecl:
        protocol, arguments, role, domain = items[1]
        return AggregateInterfaceDecl(str(items[0]), protocol, arguments, role, domain)

    def parameter(self, items: list[object]) -> Parameter:
        return Parameter(str(items[0]), items[1])

    def parameter_list(self, items: list[object]) -> tuple[Parameter, ...]:
        return tuple(items)

    @v_args(meta=True)
    def function_decl(self, meta: object, items: list[object]) -> FunctionDecl:
        return self._callable_decl(FunctionDecl, meta, items)

    @v_args(meta=True)
    def operator_decl(self, meta: object, items: list[object]) -> OperatorDecl:
        symbol = str(items[0])
        return self._callable_decl(OperatorDecl, meta, [symbol, *items[1:]])

    @v_args(meta=True)
    def callable_binding(self, meta: object, items: list[object]) -> Assignment:
        return Assignment(str(items[0]), items[1], origin=self._span(meta))

    def callable_body(
        self, items: list[object]
    ) -> tuple[tuple[Assignment | TupleDestructureDecl, ...], object]:
        binding_types = (Assignment, TupleDestructureDecl)
        if not items or isinstance(items[-1], binding_types):
            raise ValueError("callable body requires one final result expression")
        if any(not isinstance(item, binding_types) for item in items[:-1]):
            raise ValueError(
                "only immutable inferred bindings may precede a callable result"
            )
        return tuple(items[:-1]), items[-1]

    def _callable_decl(self, cls: type, meta: object, items: list[object]) -> object:
        name = str(items[0]) if cls is FunctionDecl else items[0]
        generic_parameters = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(p, ModuleParameter) for p in item)
            ),
            (),
        )
        parameters = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(p, Parameter) for p in item)
            ),
            (),
        )
        bindings, body = items[-1]
        return_type = next(
            (
                item for item in items[1:-1]
                if isinstance(item, (TypeName, VectorTypeName, TupleTypeName, InterfaceTypeName))
            ),
            None,
        )
        return cls(
            name,
            parameters,
            return_type,
            body,
            generic_parameters,
            bindings,
            origin=self._span(meta),
        )

    def pattern_function_call(self, items: list[object]) -> CallExpr:
        return CallExpr(str(items[0]), ())

    def equiv_decl(self, items: list[object]) -> EquivDecl:
        guard = items[3] if len(items) == 4 else None
        return EquivDecl(str(items[0]), items[1], items[2], guard)

    def guard_expr(self, items: list[object]) -> EquivGuard:
        return EquivGuard(tuple(str(item) for item in items))

    def pattern_constant_argument(self, items: list[object]) -> str:
        return "".join(str(item) for item in items)

    @v_args(meta=True)
    def pattern_constant_expr(
        self, meta: object, items: list[object]
    ) -> PatternConstantExpr:
        return PatternConstantExpr(
            PatternConstantKind(str(items[0])),
            str(items[1]),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def module_interface_ref(
        self, meta: object, items: list[object]
    ) -> ModuleInterfaceRef:
        arguments = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, SpecializationArgument) for arg in item)
            ),
            (),
        )
        return ModuleInterfaceRef(str(items[0]), arguments, self._span(meta))

    @v_args(meta=True)
    def module_interface_decl(
        self, meta: object, items: list[object]
    ) -> ModuleInterfaceDecl:
        name = str(items[0])
        body = items[1:]
        parameters = next(
            (
                item for item in body
                if isinstance(item, tuple)
                and all(isinstance(p, ModuleParameter) for p in item)
            ),
            (),
        )
        timings = tuple(item for item in body if isinstance(item, ModuleTimingDecl))
        if len(timings) > 1:
            raise ParseError(
                f"module interface '{name}' accepts at most one timing block"
            )
        return ModuleInterfaceDecl(
            name=name,
            parameters=parameters,
            ports=tuple(item for item in body if isinstance(item, PortDecl)),
            clocks=tuple(item[1] for item in body if _tagged(item, "clock")),
            resets=tuple(item[1] for item in body if _tagged(item, "reset")),
            reset_domains=tuple(
                (item[1], item[2]) for item in body if _tagged(item, "reset")
            ),
            clock_physical=tuple(
                item[2] for item in body if _tagged(item, "clock")
            ),
            reset_physical=tuple(
                item[3] for item in body if _tagged(item, "reset")
            ),
            request_responses=tuple(
                item for item in body if isinstance(item, RequestResponseDecl)
            ),
            aggregate_interfaces=tuple(
                item for item in body if isinstance(item, AggregateInterfaceDecl)
            ),
            timing=timings[0] if timings else None,
            origin=self._span(meta),
        )

    def module(self, items: list[object]) -> Module:
        name = str(items[0])
        body: list[object] = []
        for item in items[1:]:
            if isinstance(item, RulePriorityChain):
                body.extend(
                    RulePriority(higher, lower)
                    for higher, lower in zip(item.names, item.names[1:])
                )
            else:
                body.append(item)
        parameters = next((item for item in body if isinstance(item, tuple) and all(isinstance(p, ModuleParameter) for p in item)), ())
        timings = tuple(item for item in body if isinstance(item, ModuleTimingDecl))
        interface_ref = next(
            (item for item in body if isinstance(item, ModuleInterfaceRef)), None
        )
        parameter_constraint = next(
            (item[1] for item in body if _tagged(item, "module_where")), None
        )

        if len(timings) > 1:
            raise ParseError(f"module '{name}' accepts at most one timing block")
        return Module(
            name=name,
            ports=tuple(item for item in body if isinstance(item, PortDecl)),
            assignments=tuple(
                item for item in body if isinstance(item, Assignment)
            ),
            clocks=tuple(item[1] for item in body if _tagged(item, "clock")),
            resets=tuple(item[1] for item in body if _tagged(item, "reset")),
            reset_domains=tuple(
                (item[1], item[2])
                for item in body
                if _tagged(item, "reset")
            ),
            clock_physical=tuple(
                item[2] for item in body if _tagged(item, "clock")
            ),
            reset_physical=tuple(
                item[3] for item in body if _tagged(item, "reset")
            ),
            registers=tuple(
                item for item in body if isinstance(item, RegisterDecl)
            ),
            next_assignments=tuple(
                item for item in body if isinstance(item, NextAssignment)
            ),
            request_responses=tuple(
                item for item in body if isinstance(item, RequestResponseDecl)
            ),
            connections=tuple(
                item for item in body if isinstance(item, ConnectionDecl)
            ),
            connection_chains=tuple(
                item for item in body if isinstance(item, ConnectionChainDecl)
            ),
            csr_blocks=tuple(
                item for item in body if isinstance(item, CsrBlockDecl)
            ),
            rules=tuple(
                item for item in body
                if isinstance(item, (RuleDecl, AnonymousRuleDecl))
            ),
            rule_priorities=tuple(
                item for item in body if isinstance(item, RulePriority)
            ),
            fsms=tuple(item for item in body if isinstance(item, FsmDecl)),
            fifos=tuple(item for item in body if isinstance(item, FifoDecl)),
            memories=tuple(item for item in body if isinstance(item, MemoryDecl)),
            roms=tuple(item for item in body if isinstance(item, RomDecl)),
            arbiters=tuple(item for item in body if isinstance(item, ArbiterDecl)),
            contracts=tuple(item for item in body if isinstance(item, ContractDecl)),
            verification_goals=tuple(
                item for item in body if isinstance(item, VerificationGoalDecl)
            ),
            verification_scopes=tuple(
                item for item in body if isinstance(item, VerificationScopeDecl)
            ),
            parameters=parameters,
            declared_parameters=parameters,
            instances=tuple(item for item in body if isinstance(item, InstanceDecl)),
            aggregate_interfaces=tuple(item for item in body if isinstance(item, AggregateInterfaceDecl)),
            generic_declarations=tuple(
                item for item in body if isinstance(item, GenericDeclaration)
            ),
            compile_time_ifs=tuple(
                item for item in body if isinstance(item, CompileTimeIfDecl)
            ),
            generate_blocks=tuple(
                item for item in body if isinstance(item, GenerateBlock)
            ),
            timing=timings[0] if timings else None,
            conforms_to=interface_ref,
            ordered_items=tuple(
                item for item in body
                if item is not parameters
                and item is not interface_ref
                and not _tagged(item, "module_where")
                and item is not None
            ),
            parameter_constraint=parameter_constraint,
        )

    def module_where(self, items: list[object]) -> tuple[str, object]:
        return ("module_where", items[0])

    @v_args(meta=True)
    def external_module_decl(
        self, meta: object, items: list[object]
    ) -> Module:
        name = str(items[0])
        interface_ref = items[1]
        return Module(
            name=name,
            ports=(),
            assignments=(),
            conforms_to=interface_ref,
            external_model=str(items[2]),
            external_origin=self._span(meta),
        )

    def module_timing_latency(self, items: list[object]) -> tuple[str, int]:
        return ("module_timing_latency", self._parse_number(items[0]))

    def module_timing_ii(self, items: list[object]) -> tuple[str, int]:
        return ("module_timing_ii", self._parse_number(items[0]))

    @v_args(meta=True)
    def module_timing_decl(
        self,
        meta: object,
        items: list[object],
    ) -> ModuleTimingDecl:
        latencies = tuple(
            item[1] for item in items if _tagged(item, "module_timing_latency")
        )
        intervals = tuple(
            item[1] for item in items if _tagged(item, "module_timing_ii")
        )
        if len(latencies) != 1 or len(intervals) != 1:
            raise ParseError(
                "timing block requires exactly one latency directive and "
                "exactly one ii directive"
            )
        return ModuleTimingDecl(
            latency=latencies[0],
            initiation_interval=intervals[0],
            origin=self._span(meta),
        )

    def clock_physical_block(self, items: list[object]) -> tuple[str, str]:
        return ("clock_physical", str(items[0]))

    @v_args(meta=True)
    def clock_decl(self, meta: object, items: list[object]) -> tuple[object, ...]:
        edge = (
            str(items[1][1])
            if len(items) > 1 and items[1] is not None
            else "rising"
        )
        if edge not in {"rising", "falling"}:
            raise ParseError("clock edge must be 'rising' or 'falling'")
        declaration = ClockPhysicalDecl(str(items[0]), edge, self._span(meta))
        return ("clock", declaration.name, declaration)

    def reset_physical_block(self, items: list[object]) -> tuple[str, str, str, str]:
        return ("reset_physical", *(str(item) for item in items))

    def async_reset_physical_block(
        self, items: list[object]
    ) -> tuple[str, str]:
        return ("async_reset_physical", str(items[0]))

    @v_args(meta=True)
    def reset_decl(self, meta: object, items: list[object]) -> tuple[object, ...]:
        name = str(items[0])
        domain = next(
            (
                str(item)
                for item in items[1:]
                if item is not None and not isinstance(item, tuple)
            ),
            None,
        )
        block = next(
            (item for item in items[1:] if _tagged(item, "reset_physical")),
            None,
        )
        mode, polarity, power_up = (
            ("synchronous", "active_high", "unspecified")
            if block is None
            else (str(block[1]), str(block[2]), str(block[3]))
        )
        if mode not in {"synchronous", "asynchronous"}:
            raise ParseError(
                "reset mode must be 'synchronous' or 'asynchronous'"
            )
        if polarity not in {"active_high", "active_low"}:
            raise ParseError(
                "reset polarity must be 'active_high' or 'active_low'"
            )
        if power_up not in {"unspecified", "reset"}:
            raise ParseError("reset power_up must be 'unspecified' or 'reset'")
        declaration = ResetPhysicalDecl(
            name, domain, mode, polarity, power_up, self._span(meta)
        )
        return ("reset", name, domain, declaration)

    @v_args(meta=True)
    def async_reset_decl(
        self, meta: object, items: list[object]
    ) -> tuple[object, ...]:
        name = str(items[0])
        domain = next(
            (
                str(item)
                for item in items[1:]
                if item is not None and not isinstance(item, tuple)
            ),
            None,
        )
        block = next(
            (
                item for item in items[1:]
                if _tagged(item, "async_reset_physical")
            ),
            None,
        )
        polarity = "active_high" if block is None else str(block[1])
        if polarity not in {"active_high", "active_low"}:
            raise ParseError(
                "reset polarity must be 'active_high' or 'active_low'"
            )
        declaration = ResetPhysicalDecl(
            name=name,
            clock=domain,
            mode="asynchronous",
            polarity=polarity,
            power_up="unspecified",
            origin=self._span(meta),
            release_mode="synchronized",
            release_cycles=2,
        )
        return ("reset", name, domain, declaration)

    def register_decl(self, items: list[object]) -> RegisterDecl:
        domain = str(items[3]) if len(items) == 4 and items[3] is not None else None
        return RegisterDecl(str(items[0]), items[1], items[2], domain)

    def next_assignment(self, items: list[object]) -> NextAssignment:
        return NextAssignment(str(items[0]), items[1])

    def scalar_rule_target(self, items: list[object]) -> str:
        return str(items[0])

    @v_args(meta=True)
    def indexed_rule_target(
        self,
        meta: object,
        items: list[object],
    ) -> IndexedAssignmentTarget:
        return IndexedAssignmentTarget(
            str(items[0]),
            items[1],
            self._span(meta),
        )

    def rule_assignment(self, items: list[object]) -> NextAssignment:
        return NextAssignment(items[0], items[1])

    @staticmethod
    def _action_block(value: object) -> tuple[object, ...]:
        if not _tagged(value, "action_block"):
            raise ParseError("invalid atomic action block")
        return value[1]

    def action_block(self, items: list[object]) -> tuple[object, ...]:
        return (
            "action_block",
            tuple(
                item for item in items
                if isinstance(item, (NextAssignment, ResourceAction, ConditionalAction))
            ),
        )

    def conditional_else_block(self, items: list[object]) -> tuple[object, ...]:
        return ("conditional_else", self._action_block(items[0]))

    def conditional_else_when(self, items: list[object]) -> tuple[object, ...]:
        return ("conditional_else", (items[0],))

    @v_args(meta=True)
    def conditional_action(
        self, meta: object, items: list[object]
    ) -> ConditionalAction:
        when_false = next(
            (item[1] for item in items[2:] if _tagged(item, "conditional_else")),
            None,
        )
        return ConditionalAction(
            items[0],
            self._action_block(items[1]),
            when_false,
            self._span(meta),
        )

    @v_args(meta=True)
    def resource_action(self, meta: object, items: list[object]) -> ResourceAction:
        return ResourceAction(
            str(items[0]), str(items[1]),
            tuple(item for item in items[2:] if item is not None),
            self._span(meta),
        )

    @v_args(meta=True)
    def qualified_resource_action(
        self, meta: object, items: list[object]
    ) -> ResourceAction:
        resource, action = str(items[0]).split(".", 1)
        return ResourceAction(
            resource,
            action,
            tuple(item for item in items[1:] if item is not None),
            self._span(meta),
        )

    @v_args(meta=True)
    def rule_decl(self, meta: object, items: list[object]) -> RuleDecl:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[1], items[2], items[3:], origin
        )
        if not actions:
            raise ParseError("atomic rule cannot be empty")
        return RuleDecl(
            str(items[0]),
            guard,
            actions,
            origin,
        )

    def concise_rule_decl(self, items: list[object]) -> RuleDecl:
        """Normalize ``name: when guard { actions }`` to the rule AST."""

        return RuleDecl(
            str(items[0]),
            items[1],
            self._action_block(items[2]),
        )

    @v_args(meta=True)
    def anonymous_rule_decl(self, meta: object, items: list[object]) -> AnonymousRuleDecl:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[0], items[1], items[2:], origin
        )
        if not actions:
            raise ParseError("atomic rule cannot be empty")
        return AnonymousRuleDecl(
            guard,
            actions,
            origin,
        )

    def rule_priority(self, items: list[object]) -> RulePriority | RulePriorityChain:
        names = tuple(str(item) for item in items)
        if len(names) == 2:
            return RulePriority(*names)
        return RulePriorityChain(names)

    @v_args(meta=True)
    def labeled_priority_arm(self, meta: object, items: list[object]) -> PriorityRuleArm:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[1], items[2], items[3:], origin
        )
        return PriorityRuleArm(
            str(items[0]),
            guard,
            actions,
            origin,
        )

    @v_args(meta=True)
    def anonymous_priority_arm(self, meta: object, items: list[object]) -> PriorityRuleArm:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[0], items[1], items[2:], origin
        )
        return PriorityRuleArm(
            None,
            guard,
            actions,
            origin,
        )

    def nested_priority_arm(self, items: list[object]) -> object:
        return items[0]

    @v_args(meta=True)
    def priority_block(self, meta: object, items: list[object]) -> PriorityBlockDecl:
        return PriorityBlockDecl(tuple(items), self._span(meta))

    @v_args(meta=True)
    def fsm_transition(
        self, meta: object, items: list[object]
    ) -> FsmTransitionDecl:
        block = next(
            item for item in items if _tagged(item, "action_block")
        )
        guard = next(
            (
                item for item in items
                if not isinstance(item, str) and item is not block
            ),
            None,
        )
        target = next(str(item) for item in items if isinstance(item, str))
        actions = self._action_block(block)
        return FsmTransitionDecl(target, actions, guard, self._span(meta))

    def fsm_hold(self, _items: list[object]) -> tuple[str]:
        return ("fsm_hold",)

    def fsm_single_transition(
        self, items: list[object]
    ) -> tuple[str, tuple[FsmTransitionDecl, ...]]:
        return ("fsm_transitions", (items[0],))

    def fsm_priority(
        self, items: list[object]
    ) -> tuple[str, tuple[FsmTransitionDecl, ...]]:
        return ("fsm_priority", tuple(items))

    @v_args(meta=True)
    def fsm_state(self, meta: object, items: list[object]) -> FsmStateDecl:
        member = str(items[0])
        body = items[1]
        if isinstance(body, tuple) and body[:1] == ("fsm_hold",):
            return FsmStateDecl(member, hold=True, origin=self._span(meta))
        return FsmStateDecl(
            member,
            body[1],
            priority=_tagged(body, "fsm_priority"),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def typed_fsm_decl(self, meta: object, items: list[object]) -> FsmDecl:
        return FsmDecl(
            str(items[0]),
            items[1],
            str(items[2]),
            tuple(item for item in items[3:] if isinstance(item, FsmStateDecl)),
            self._span(meta),
        )

    @v_args(meta=True)
    def inferred_fsm_decl(self, meta: object, items: list[object]) -> FsmDecl:
        return FsmDecl(
            str(items[0]),
            TypeName(str(items[1])),
            str(items[2]),
            tuple(item for item in items[3:] if isinstance(item, FsmStateDecl)),
            self._span(meta),
        )

    @v_args(meta=True)
    def fifo_decl(self, meta: object, items: list[object]) -> FifoDecl:
        return FifoDecl(str(items[0]), items[1], items[2], self._span(meta))

    @v_args(meta=True)
    def memory_decl(self, meta: object, items: list[object]) -> MemoryDecl:
        reset_policy = next(
            (item for item in items if _tagged(item, "memory_reset_policy")),
            None,
        )
        return MemoryDecl(
            str(items[0]),
            items[1],
            items[2],
            self._parse_number(items[3]),
            MemoryCollision(str(items[4])),
            self._span(meta),
            contents_reset=(
                reset_policy[1]
                if reset_policy is not None else MemoryResetPolicy.CLEAR
            ),
            read_data_reset=(
                reset_policy[2]
                if reset_policy is not None else MemoryResetPolicy.CLEAR
            ),
        )

    def memory_reset_policy(self, items: list[object]) -> tuple[object, ...]:
        return (
            "memory_reset_policy",
            MemoryResetPolicy(str(items[0])),
            MemoryResetPolicy(str(items[1])),
        )

    @v_args(meta=True)
    def rom_decl(self, meta: object, items: list[object]) -> RomDecl:
        return RomDecl(
            str(items[0]),
            items[1],
            items[2],
            int(str(items[3])),
            items[4],
            self._span(meta),
        )

    def port_name_list(self, items: list[object]) -> tuple[str, tuple[str, ...]]:
        return ("port_names", tuple(str(item) for item in items))

    @v_args(meta=True)
    def port_decl(self, meta: object, items: list[object]) -> PortDecl:
        names_item = next(item for item in items if _tagged(item, "port_names"))
        names = tuple(names_item[1])
        type_name = next(
            item for item in items
            if isinstance(item, (TypeName, VectorTypeName, TupleTypeName, InterfaceTypeName))
        )
        trailing = [
            item for item in items[2:]
            if item is not type_name and item is not None
        ]
        domain = next((str(item) for item in trailing if isinstance(item, str)), None)
        initializer = next((item for item in trailing if not isinstance(item, str)), None)
        return PortDecl(
            Direction(str(items[0])), names[0], type_name, domain,
            () if len(names) == 1 else names,
            initializer, self._span(meta),
        )

    def wire_interface_type(self, items: list[object]) -> InterfaceTypeName:
        return InterfaceTypeName(InterfaceKind.WIRE, items[0])

    def ready_valid_interface_type(self, items: list[object]) -> InterfaceTypeName:
        return InterfaceTypeName(InterfaceKind.READY_VALID, items[0])

    def credit_interface_type(self, items: list[object]) -> InterfaceTypeName:
        return InterfaceTypeName(InterfaceKind.CREDIT, items[0], int(str(items[1])))

    def packet_interface_type(self, items: list[object]) -> InterfaceTypeName:
        return InterfaceTypeName(InterfaceKind.PACKET, items[0])

    def vc_credit_interface_type(self, items: list[object]) -> InterfaceTypeName:
        return InterfaceTypeName(
            InterfaceKind.VC_CREDIT,
            items[0],
            int(str(items[2])),
            int(str(items[1])),
        )

    def interface_decl(self, items: list[object]) -> RequestResponseDecl:
        name = str(items[0])
        request_type = items[1]
        response_type = items[2]
        max_outstanding = int(str(items[3]))
        ordering = RequestResponseOrdering(str(items[4]))
        match_by = items[5] if len(items) == 6 else None
        return RequestResponseDecl(
            name,
            request_type,
            response_type,
            max_outstanding,
            ordering,
            str(match_by) if match_by is not None else None,
        )

    request_response_interface_decl = interface_decl

    def connection_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("buffer", int(str(items[0])))

    def connection_request_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("request_buffer", int(str(items[0])))

    def connection_response_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("response_buffer", int(str(items[0])))

    def connection_adapter(self, items: list[object]) -> tuple[str, object]:
        return ("adapter", ConnectionAdapter(str(items[0])))

    def basic_crossing(self, items: list[object]) -> Crossing:
        return Crossing(CrossingKind(str(items[0])))

    def async_fifo_crossing(self, items: list[object]) -> Crossing:
        return Crossing(CrossingKind.ASYNC_FIFO, int(str(items[0])))

    def connection_crossing(self, items: list[object]) -> tuple[str, object]:
        return ("crossing", items[0])

    def connection_transform(self, items: list[object]) -> tuple[str, object]:
        return ("transform", items[0])

    def connect_decl(self, items: list[object]) -> ConnectionDecl:
        options = dict(item for item in items[2:] if item is not None)
        return ConnectionDecl(
            str(items[0]),
            str(items[1]),
            int(options.get("buffer", 0)),
            int(options.get("request_buffer", 0)),
            int(options.get("response_buffer", 0)),
            options.get("adapter"),
            options.get("crossing"),
            options.get("transform"),
        )

    bare_connect_decl = connect_decl

    @v_args(meta=True)
    def connection_chain_decl(
        self, meta: object, items: list[object]
    ) -> ConnectionChainDecl:
        return ConnectionChainDecl(
            tuple(str(item) for item in items), origin=self._span(meta)
        )

    def hierarchical_endpoint(self, items: list[object]) -> str:
        result = str(items[0])
        for item in items[1:]:
            if item is None:
                continue
            if _tagged(item, "endpoint_index"):
                result += f"[{item[1]}]"
            else:
                result += "." + str(item)
        return result

    def endpoint_index(self, items: list[object]) -> tuple[str, object]:
        token = str(items[0])
        try:
            value: object = self._parse_number(token)
        except (ParseError, ValueError):
            value = token
        return ("endpoint_index", value)

    def arbiter_sources(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    def arbiter_decl(self, items: list[object]) -> ArbiterDecl:
        return ArbiterDecl(
            items[0],
            str(items[1]),
            ArbitrationPolicy(str(items[2])),
            GrantScope(str(items[3])),
        )

    def contract_decl(self, items: list[object]) -> ContractDecl:
        return ContractDecl(
            ContractKind(str(items[0])),
            str(items[1]),
            str(items[2]),
            str(items[3]),
            items[4],
        )

    @v_args(meta=True)
    def verification_goal_decl(
        self, meta: object, items: list[object]
    ) -> VerificationGoalDecl:
        return VerificationGoalDecl(
            VerificationGoalKind(str(items[0])),
            str(items[1]),
            items[3],
            str(items[2]) if items[2] is not None else None,
            self._span(meta),
        )

    @v_args(meta=True)
    def verification_requirement_decl(
        self, meta: object, items: list[object]
    ) -> VerificationRequirementDecl:
        return VerificationRequirementDecl(
            str(items[0]), items[1], self._span(meta)
        )

    @v_args(meta=True)
    def verification_scoped_goal_decl(
        self, meta: object, items: list[object]
    ) -> VerificationGoalDecl:
        return VerificationGoalDecl(
            VerificationGoalKind(str(items[0])),
            str(items[1]),
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def verification_scope_decl(
        self, meta: object, items: list[object]
    ) -> VerificationScopeDecl:
        return VerificationScopeDecl(
            str(items[0]),
            tuple(
                item for item in items[2:]
                if isinstance(item, VerificationRequirementDecl)
            ),
            tuple(
                item for item in items[2:]
                if isinstance(item, VerificationGoalDecl)
            ),
            str(items[1]) if items[1] is not None else None,
            self._span(meta),
        )

    def csr_position(self, items: list[object]) -> tuple[str, int, int]:
        numbers = [item for item in items if item is not None]
        msb = self._parse_number(numbers[0])
        lsb = self._parse_number(numbers[1]) if len(numbers) == 2 else msb
        return ("position", msb, lsb)

    @v_args(meta=True)
    def csr_field(self, meta: object, items: list[object]) -> CsrFieldDecl:
        name = str(items[0])
        type_name = items[1]
        position = next(
            (item for item in items[2:] if _tagged_position(item)), None
        )
        access_index = next(
            index
            for index, item in enumerate(items[2:], start=2)
            if str(item) in {access.value for access in CsrAccess}
        )
        trailing = [
            item for item in items[access_index + 1 :] if item is not None
        ]
        binding = next(
            (item for item in trailing if isinstance(item, CsrBinding)), None
        )
        reset_item = next(
            (item for item in trailing if not isinstance(item, CsrBinding)), None
        )
        reset = self._parse_number(reset_item) if reset_item is not None else None
        msb = position[1] if position is not None else None
        lsb = position[2] if position is not None else None
        return CsrFieldDecl(
            name,
            type_name,
            CsrAccess(str(items[access_index])),
            msb,
            lsb,
            reset,
            binding,
            self._span(meta),
        )

    def signal_ref(self, items: list[object]) -> str:
        return ".".join(str(item) for item in items)

    def csr_status_binding(self, items: list[object]) -> CsrBinding:
        return CsrBinding(CsrBindingKind.STATUS, str(items[0]))

    def csr_sticky_binding(self, items: list[object]) -> CsrBinding:
        values = [item for item in items if item is not None]
        priority = (
            CsrPriority(str(values[1]))
            if len(values) == 2
            else CsrPriority.HARDWARE
        )
        return CsrBinding(CsrBindingKind.STICKY, str(values[0]), priority)

    def csr_command_binding(self, items: list[object]) -> CsrBinding:
        return CsrBinding(CsrBindingKind.COMMAND, str(items[0]))

    @v_args(meta=True)
    def csr_register(self, meta: object, items: list[object]) -> CsrRegisterDecl:
        return CsrRegisterDecl(
            str(items[0]),
            self._parse_number(items[1]),
            tuple(item for item in items[2:] if isinstance(item, CsrFieldDecl)),
            self._span(meta),
        )

    @v_args(meta=True)
    def csr_decl(self, meta: object, items: list[object]) -> CsrBlockDecl:
        return CsrBlockDecl(
            str(items[0]),
            self._parse_number(items[1]),
            tuple(
                item for item in items[2:] if isinstance(item, CsrRegisterDecl)
            ),
            self._span(meta),
        )

    def builtin_type(self, items: list[object]) -> TypeName:
        return TypeName(str(items[0]))

    def generic_fixed_type_value(self, items: list[object]) -> TypeName:
        return TypeName(str(items[0]))

    def generic_type_value(self, items: list[object]) -> TypeName:
        return TypeName(str(items[0]))

    def alias_type(self, items: list[object]) -> TypeName:
        return TypeName(str(items[0]))

    def generic_type_ref(self, items: list[object]) -> TypeName:
        if len(items) == 1:
            text = str(items[0])
            match = re.fullmatch(r"(uint|sint|bits)<([0-9]+)>", text)
            if match is not None and int(match.group(2)) < 1:
                raise ParseError("generic hardware type width must be positive")
            return TypeName(text)
        if str(items[0]) in {"uint", "sint", "bits"} and str(items[1]).isdigit() and int(str(items[1])) < 1:
            raise ParseError("generic hardware type width must be positive")
        return TypeName(f"{items[0]}<{','.join(str(item) for item in items[1:])}>")

    def specialization_type_ref(self, items: list[object]) -> TypeName:
        return TypeName(str(items[0]))

    def type_ref(self, items: list[object]) -> str:
        return f"{items[0]}<{','.join(str(item) for item in items[1:])}>"

    def qualified_type_ref(self, items: list[object]) -> str:
        return f"{items[0]}<{','.join(str(item) for item in items[1:])}>"

    def type_argument(self, items: list[object]) -> int | str:
        return items[0]

    def type_value_argument(self, items: list[object]) -> str:
        return str(items[0])

    def width_expression(self, items: list[object]) -> str:
        return "".join(str(item) for item in items)

    def intrinsic_width(self, items: list[object]) -> str:
        return f"{items[0]}({items[1]})"

    def intrinsic_param_value(self, items: list[object]) -> str:
        return f"{items[0]}({items[1]})"

    def parenthesized_width(self, items: list[object]) -> str:
        return f"({items[0]})"

    def signed_width(self, items: list[object]) -> str:
        return f"{items[0]}{items[1]}"

    @staticmethod
    def _storage_depth_value(value: object) -> int | str:
        text = str(value)
        try:
            return int(text, 0)
        except ValueError:
            return text

    def storage_depth_atom(self, items: list[object]) -> int | str:
        return self._storage_depth_value(items[0])

    def storage_depth_signed(self, items: list[object]) -> str:
        return f"{items[0]}{items[1]}"

    def storage_depth_parenthesized(self, items: list[object]) -> str:
        return f"({items[0]})"

    def storage_depth_expression(self, items: list[object]) -> str:
        return str(items[0])

    def vector_type(self, items: list[object]) -> VectorTypeName:
        length = str(items[0])
        return VectorTypeName(int(length) if length.isdigit() else length, items[1])

    def string_type(self, items: list[object]) -> VectorTypeName:
        length = str(items[0])
        return VectorTypeName(
            int(length) if length.isdigit() else length,
            TypeName("char"),
        )

    def tuple_type(self, items: list[object]) -> TupleTypeName:
        if not 2 <= len(items) <= 8:
            raise ParseError("a tuple type requires between 2 and 8 components")
        return TupleTypeName(tuple(items))

    def tuple_type_passthrough(self, items: list[object]) -> TupleTypeName:
        return items[0]

    @v_args(meta=True)
    def tuple_literal_expr(
        self, meta: object, items: list[object]
    ) -> TupleLiteralExpr:
        if not 2 <= len(items) <= 8:
            raise ParseError("a tuple literal requires between 2 and 8 elements")
        return TupleLiteralExpr(tuple(items), origin=self._span(meta))

    def assignment(self, items: list[object]) -> Assignment:
        return Assignment(str(items[0]), items[1])

    def typed_assignment(self, items: list[object]) -> Assignment:
        return Assignment(str(items[0]), items[2], items[1])

    def assignment_target(self, items: list[object]) -> str:
        return ".".join(str(item) for item in items)

    @v_args(meta=True)
    def name_expr(self, meta: object, items: list[object]) -> NameExpr:
        return NameExpr(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_builtin(self, meta: object, items: list[object]) -> TypeValueExpr:
        return TypeValueExpr(TypeName(str(items[0])), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_generic(self, meta: object, items: list[object]) -> TypeValueExpr:
        return TypeValueExpr(TypeName(str(items[0])), origin=self._span(meta))

    @v_args(meta=True)
    def type_value_vec(self, meta: object, items: list[object]) -> TypeValueExpr:
        text = str(items[0])
        match = re.fullmatch(r"vec<([0-9]+),(.+)>", text)
        if match is None:
            raise ParseError(f"invalid vector type value '{text}'")
        return TypeValueExpr(
            VectorTypeName(int(match.group(1)), TypeName(match.group(2))),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def type_value_ref(self, meta: object, items: list[object]) -> TypeValueExpr:
        syntax = items[0] if isinstance(items[0], (TypeName, VectorTypeName, TupleTypeName)) else TypeName(str(items[0]))
        return TypeValueExpr(syntax, origin=self._span(meta))

    @v_args(meta=True)
    def number_expr(self, meta: object, items: list[object]) -> NumberExpr:
        return NumberExpr(self._parse_number(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def rational_expr(self, meta: object, items: list[object]) -> RationalExpr:
        value = Fraction(str(items[0]).replace("_", ""))
        return RationalExpr(value.numerator, value.denominator, origin=self._span(meta))

    def argument_list(self, items: list[object]) -> tuple[object, ...]:
        return tuple(items)

    def named_call_specialization_argument(self, items: list[object]) -> SpecializationArgument:
        return SpecializationArgument(str(items[0]), items[1])

    def positional_call_specialization_argument(self, items: list[object]) -> SpecializationArgument:
        return SpecializationArgument(None, items[0])

    def call_specialization_arguments(self, items: list[object]) -> tuple[SpecializationArgument, ...]:
        return tuple(items)

    def callable_ref_specializations(self, items: list[object]) -> tuple[SpecializationArgument, ...]:
        return tuple(items)

    def call_parameter_number(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def callable_specialization_ref(self, items: list[object]) -> CallableRef:
        specializations = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, SpecializationArgument) for arg in item)
            ),
            (),
        )
        return CallableRef(str(items[0]), specializations)

    @v_args(meta=True)
    def function_call(self, meta: object, items: list[object]) -> CallExpr:
        specializations = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, SpecializationArgument) for arg in item)
            ),
            (),
        )
        arguments = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and not all(isinstance(arg, SpecializationArgument) for arg in item)
            ),
            (),
        )
        return CallExpr(
            str(items[0]), arguments, specializations, origin=self._span(meta)
        )

    @v_args(meta=True)
    def qualified_function_call(
        self, meta: object, items: list[object]
    ) -> CallExpr:
        return self.function_call(meta, items)

    @v_args(meta=True)
    def unary_negate_expr(self, meta: object, items: list[object]) -> UnaryExpr:
        return UnaryExpr(BinaryOperator.SUBTRACT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unary_not_expr(self, meta: object, items: list[object]) -> UnaryExpr:
        return UnaryExpr(BinaryOperator.LOGIC_NOT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unary_bit_not_expr(self, meta: object, items: list[object]) -> UnaryExpr:
        return UnaryExpr(BinaryOperator.BIT_NOT, items[0], origin=self._span(meta))

    @v_args(meta=True)
    def field_expr(self, meta: object, items: list[object]) -> FieldExpr:
        return FieldExpr(items[0], str(items[1]), origin=self._span(meta))

    @v_args(meta=True)
    def index_expr(self, meta: object, items: list[object]) -> IndexExpr:
        index = items[1]
        if isinstance(index, NumberExpr):
            index = index.value
        return IndexExpr(items[0], index, origin=self._span(meta))

    @v_args(meta=True)
    def slice_expr(self, meta: object, items: list[object]) -> SliceExpr:
        bounds = [self._slice_bound(item) for item in items[1:]]
        return SliceExpr(items[0], bounds[0], bounds[1], origin=self._span(meta))

    @v_args(meta=True)
    def vector_range_expr(
        self, meta: object, items: list[object]
    ) -> VectorRangeExpr:
        bounds = [self._slice_bound(item) for item in items[1:]]
        return VectorRangeExpr(
            items[0], bounds[0], bounds[1], origin=self._span(meta)
        )

    @classmethod
    def _slice_bound(cls, expression: object) -> int | str:
        """Retain a parsed bound as a compile-time integer expression."""

        if isinstance(expression, NumberExpr):
            return expression.value
        if isinstance(expression, NameExpr):
            return expression.name
        if isinstance(expression, UnaryExpr):
            return f"{expression.operator.value}{cls._slice_bound(expression.expression)}"
        if isinstance(expression, BinaryExpr):
            return (
                f"{cls._slice_bound(expression.left)}"
                f"{expression.operator.value}"
                f"{cls._slice_bound(expression.right)}"
            )
        if isinstance(expression, CallExpr) and not expression.specializations:
            arguments = ",".join(
                str(cls._slice_bound(argument)) for argument in expression.arguments
            )
            return f"{expression.function}({arguments})"
        # Preserve parsing as the syntax boundary; semantic analysis owns the
        # compile-time-only diagnostic for other expression shapes.
        return str(expression)

    @v_args(meta=True)
    def concat_expr(self, meta: object, items: list[object]) -> ConcatExpr:
        arguments = items[0] if items and items[0] is not None else ()
        return ConcatExpr(tuple(arguments), origin=self._span(meta))

    @v_args(meta=True)
    def bitcast_expr(self, meta: object, items: list[object]) -> BitcastExpr:
        return BitcastExpr(items[0], items[1], origin=self._span(meta))

    @v_args(meta=True)
    def reshape_expr(self, meta: object, items: list[object]) -> ReshapeExpr:
        if len(items) == 1:
            return ReshapeExpr(None, items[0], origin=self._span(meta))
        return ReshapeExpr(items[0], items[1], origin=self._span(meta))

    @v_args(meta=True)
    def pack_expr(self, meta: object, items: list[object]) -> PackExpr:
        return PackExpr(items[0], origin=self._span(meta))

    @v_args(meta=True)
    def unpack_expr(self, meta: object, items: list[object]) -> UnpackExpr:
        return UnpackExpr(items[0], items[1], origin=self._span(meta))

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
    def generate_expr(self, meta: object, items: list[object]) -> GenerateExpr:
        index, start, stop = items[0]
        return GenerateExpr(
            index, start, stop, items[1], origin=self._span(meta)
        )

    @v_args(meta=True)
    def map_expr(self, meta: object, items: list[object]) -> MapExpr:
        index, start, stop = items[0]
        return MapExpr(index, start, stop, items[1], origin=self._span(meta))

    @v_args(meta=True)
    def indexed_sum_expr(
        self, meta: object, items: list[object]
    ) -> IndexedSumExpr:
        index, start, stop = items[0]
        return IndexedSumExpr(
            index, start, stop, items[1], origin=self._span(meta)
        )

    @v_args(meta=True)
    def collection_sum_expr(
        self, meta: object, items: list[object]
    ) -> CollectionSumExpr:
        return CollectionSumExpr(items[0], origin=self._span(meta))

    @v_args(meta=True)
    def reduce_expr(self, meta: object, items: list[object]) -> ReduceExpr:
        return ReduceExpr(
            ReductionOperator(str(items[0])),
            items[1],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def dot_expr(self, meta: object, items: list[object]) -> DotExpr:
        rounding = (
            FixedRoundingMode(str(items[2]))
            if len(items) == 3 and items[2] is not None
            else None
        )
        return DotExpr(items[0], items[1], rounding, origin=self._span(meta))

    @v_args(meta=True)
    def contextual_quantize_expr(self, meta: object, items: list[object]) -> QuantizeExpr:
        return QuantizeExpr(
            items[0], FixedRoundingMode(str(items[1])), origin=self._span(meta)
        )

    @v_args(meta=True)
    def contextual_full_quantize_expr(
        self, meta: object, items: list[object]
    ) -> QuantizeExpr:
        """Keep policy-explicit contextual quantize on the existing AST node."""

        return QuantizeExpr(
            items[0],
            FixedRoundingMode(str(items[1])),
            overflow=FixedOverflowMode(str(items[2])),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def full_quantize_expr(self, meta: object, items: list[object]) -> QuantizeExpr:
        return QuantizeExpr(
            items[1],
            FixedRoundingMode(str(items[2])),
            target_type=items[0],
            overflow=FixedOverflowMode(str(items[3])),
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
                AddExpr(expression, operand, origin=origin)
                if operator == "+"
                else BinaryExpr(
                    BinaryOperator.SUBTRACT,
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
            expression = BinaryExpr(
                BinaryOperator.MULTIPLY
                if str(items[index]) == "*" else BinaryOperator.DIVIDE,
                expression,
                items[index + 1],
                origin=origin,
            )
        return expression

    @v_args(meta=True)
    def bit_and_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, BinaryOperator.BIT_AND, self._span(meta))

    @v_args(meta=True)
    def bit_xor_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, BinaryOperator.BIT_XOR, self._span(meta))

    @v_args(meta=True)
    def bit_or_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, BinaryOperator.BIT_OR, self._span(meta))

    @v_args(meta=True)
    def logical_and_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, BinaryOperator.LOGIC_AND, self._span(meta))

    @v_args(meta=True)
    def logical_or_expr(self, meta: object, items: list[object]) -> object:
        return self._fold(items, BinaryOperator.LOGIC_OR, self._span(meta))

    @v_args(meta=True)
    def shift_expr(self, meta: object, items: list[object]) -> object:
        expression = items[0]
        origin = self._span(meta)
        for index in range(1, len(items), 2):
            operator = BinaryOperator(str(items[index]))
            expression = BinaryExpr(
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
        return BinaryExpr(
            BinaryOperator(str(items[1])),
            items[0],
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def conditional_expr(self, meta: object, items: list[object]) -> MuxExpr:
        return MuxExpr(items[0], items[1], items[2], origin=self._span(meta))

    @v_args(meta=True)
    def explicit_resize_expr(self, meta: object, items: list[object]) -> ResizeExpr:
        return ResizeExpr(
            ResizeKind(str(items[0])),
            int(str(items[1])) if str(items[1]).isdigit() else str(items[1]),
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def contextual_resize_expr(
        self, meta: object, items: list[object]
    ) -> ResizeExpr:
        return ResizeExpr(
            ResizeKind(str(items[0])),
            None,
            items[1],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def delay_expr(self, meta: object, items: list[object]) -> DelayExpr:
        return DelayExpr(
            int(str(items[0])), items[1], origin=self._span(meta)
        )

    def pipeline_constraint(self, items: list[object]) -> PipelineConstraint:
        return PipelineConstraint(
            PipelineMetric(str(items[0])),
            PipelineRelation(str(items[1])),
            self._parse_number(items[2]),
        )

    @v_args(meta=True)
    def pipeline_expr(self, meta: object, items: list[object]) -> PipelineExpr:
        depth = str(items[0])
        return PipelineExpr(
            int(depth),
            items[-1],
            tuple(
                item for item in items[1:-1]
                if isinstance(item, PipelineConstraint)
            ),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def removed_pipeline_auto_expr(
        self, meta: object, items: list[object]
    ) -> object:
        raise ParseError(
            "scalar pipeline(auto) was removed; use implement { expression "
            "intent { ... } } for compiler-selected implementation, or "
            "pipeline(N) for exact latency"
        )

    @v_args(meta=True)
    def protocol_transform_expr(
        self, meta: object, items: list[object]
    ) -> ProtocolTransformExpr:
        return ProtocolTransformExpr(
            items[-1],
            tuple(
                item for item in items[:-1]
                if isinstance(item, PipelineConstraint)
            ),
            origin=self._span(meta),
        )

    def architecture_constraint(self, items: list[object]) -> tuple[str, int]:
        return (str(items[0]), self._parse_number(items[1]))

    @v_args(meta=True)
    def removed_architecture_expr(
        self, meta: object, items: list[object]
    ) -> object:
        raise ParseError(
            "scalar architecture(auto) was removed; use implement { expression "
            "intent { ... } } for compiler-selected implementation"
        )

    def implementation_arm(self, items: list[object]) -> ImplementationArm:
        return ImplementationArm(ImplementationKind(str(items[0])), items[1])

    def explicit_implementation_selection(
        self, items: list[object]
    ) -> tuple[ImplementationKind, CostPolicy | None]:
        return (ImplementationKind(str(items[0])), None)

    def cost_constraint(self, items: list[object]) -> CostConstraint:
        return CostConstraint(
            _cost_metric(str(items[0])),
            self._parse_number(items[1]),
        )

    def feedback_policy(self, items: list[object]) -> SynthesisFeedback:
        return SynthesisFeedback(str(items[0]))

    def cost_implementation_selection(
        self, items: list[object]
    ) -> tuple[ImplementationKind | None, CostPolicy]:
        return (
            None,
            CostPolicy(
                _cost_metric(str(items[0])),
                tuple(
                    item for item in items[1:] if isinstance(item, CostConstraint)
                ),
                next(
                    (
                        item
                        for item in items[1:]
                        if isinstance(item, SynthesisFeedback)
                    ),
                    None,
                ),
            ),
        )

    @v_args(meta=True)
    def implementation_choice_expr(
        self, meta: object, items: list[object]
    ) -> ImplementationChoiceExpr:
        selected, cost_policy = items[0]
        return ImplementationChoiceExpr(
            selected,
            tuple(items[1:]),
            cost_policy,
            origin=self._span(meta),
        )

    def explore_allow(self, items: list[object]) -> tuple[str, tuple[str, ...]]:
        return ("allow", (str(items[0]),))

    def explore_avoid(self, items: list[object]) -> tuple[str, tuple[str, ...]]:
        return ("avoid", (str(items[0]),))

    def explore_allow_group(self, items: list[object]) -> tuple[str, tuple[str, ...]]:
        return ("allow", tuple(str(item) for item in items))

    def explore_avoid_group(self, items: list[object]) -> tuple[str, tuple[str, ...]]:
        return ("avoid", tuple(str(item) for item in items))

    def explore_require(self, items: list[object]) -> tuple[str, str, int]:
        return (
            str(items[0]),
            str(items[1]),
            self._parse_number(items[2]),
        )

    def explore_require_group(self, items: list[object]) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (str(items[index]), str(items[index + 1]), self._parse_number(items[index + 2]))
            for index in range(0, len(items), 3)
        )

    def explore_minimize(self, items: list[object]) -> tuple[str, str]:
        return ("minimize", str(items[0]))

    def explore_maximize(self, items: list[object]) -> tuple[str, str]:
        return ("maximize", str(items[0]))

    @v_args(meta=True)
    def removed_explore_expr(self, meta: object, items: list[object]) -> object:
        raise ParseError(
            "scalar explore was removed; use implement { expression intent "
            "{ ... } } for compiler-selected implementation"
        )

    def implement_constraint(self, items: list[object]) -> ExplorationConstraint:
        return ExplorationConstraint(
            _cost_metric(str(items[0])),
            ExplorationRelation(str(items[1])),
            self._parse_number(items[2]),
        )

    def implement_minimize(self, items: list[object]) -> ExplorationObjective:
        return ExplorationObjective("minimize", _cost_metric(str(items[0])))

    def implement_maximize(self, items: list[object]) -> ExplorationObjective:
        return ExplorationObjective("maximize", _cost_metric(str(items[0])))

    def implement_intent(
        self, items: list[object]
    ) -> tuple[tuple[ExplorationConstraint, ...], ExplorationObjective | None]:
        constraints: list[ExplorationConstraint] = []
        objective = None
        for item in items:
            if isinstance(item, ExplorationConstraint):
                constraints.append(item)
            elif isinstance(item, ExplorationObjective):
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
    def implement_expr(self, meta: object, items: list[object]) -> ImplementExpr:
        constraints, objective = items[1]
        return ImplementExpr(
            items[0],
            constraints,
            objective,
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def mux_expr(self, meta: object, items: list[object]) -> MuxExpr:
        return MuxExpr(
            items[0], items[1], items[2], origin=self._span(meta)
        )

    def numeric_switch_key(self, items: list[object]) -> int:
        return self._parse_number(items[0])

    def enum_switch_key(self, items: list[object]) -> EnumMemberRef:
        owner, member = str(items[0]).split(".", 1)
        return EnumMemberRef(owner, member)

    def qualified_nominal_ref(self, items: list[object]) -> tuple[str, str]:
        owner, member = str(items[0]).split(".", 1)
        return owner, member

    @v_args(meta=True)
    def qualified_nominal_expr(
        self, meta: object, items: list[object]
    ) -> FieldExpr:
        owner, member = items[0]
        origin = self._span(meta)
        return FieldExpr(NameExpr(owner, origin=origin), member, origin=origin)

    def switch_arm(self, items: list[object]) -> SwitchArm:
        return SwitchArm(items[0], items[1])

    def else_arm(self, items: list[object]) -> object:
        return items[0]

    @v_args(meta=True)
    def switch_expr(self, meta: object, items: list[object]) -> SwitchExpr:
        arms = tuple(item for item in items[1:] if isinstance(item, SwitchArm))
        default = next(
            (item for item in items[1:] if not isinstance(item, SwitchArm)), None
        )
        return SwitchExpr(
            items[0],
            arms,
            default,
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def qualified_switch_expr(
        self, meta: object, items: list[object]
    ) -> SwitchExpr:
        owner, member = str(items[0]).split(".", 1)
        origin = self._span(meta)
        selector = FieldExpr(NameExpr(owner, origin=origin), member, origin=origin)
        arms = tuple(item for item in items[1:] if isinstance(item, SwitchArm))
        default = next(
            (
                item for item in items[1:]
                if item is not None and not isinstance(item, SwitchArm)
            ),
            None,
        )
        return SwitchExpr(selector, arms, default, origin=origin)

    @v_args(meta=True)
    def tagged_union_construct_expr(
        self, meta: object, items: list[object]
    ) -> TaggedUnionConstructExpr:
        head = items[0]
        owner, variant = (
            head
            if isinstance(head, tuple)
            else str(head).split(".", 1)
        )
        return TaggedUnionConstructExpr(
            owner, variant, tuple(items[1:]), origin=self._span(meta)
        )

    def tagged_union_binders(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def tagged_union_match_arm(
        self, meta: object, items: list[object]
    ) -> TaggedUnionMatchArm:
        owner, variant = str(items[0]).split(".", 1)
        binders = next((item for item in items[1:-1] if isinstance(item, tuple)), ())
        return TaggedUnionMatchArm(
            owner, variant, binders, items[-1], self._span(meta)
        )

    @v_args(meta=True)
    def tagged_union_match_expr(
        self, meta: object, items: list[object]
    ) -> TaggedUnionMatchExpr:
        return TaggedUnionMatchExpr(
            items[0], tuple(items[1:]), origin=self._span(meta)
        )

    @v_args(meta=True)
    def qualified_tagged_union_match_expr(
        self, meta: object, items: list[object]
    ) -> TaggedUnionMatchExpr:
        owner, member = str(items[0]).split(".", 1)
        origin = self._span(meta)
        selector = FieldExpr(NameExpr(owner, origin=origin), member, origin=origin)
        return TaggedUnionMatchExpr(
            selector, tuple(items[1:]), origin=origin
        )

    @staticmethod
    def _fold(
        items: list[object],
        operator: BinaryOperator,
        origin: SourceSpan,
    ) -> object:
        expression = items[0]
        for operand in items[1:]:
            expression = BinaryExpr(
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
    def _parse_number(value: object) -> int:
        text = str(value).replace("_", "")
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.lower().startswith("0b"):
            return int(text, 2)
        return int(text, 10)


_GRAMMAR = files("zlang.parser").joinpath("grammar.lark").read_text()
_PARSER: Lark | None = None
_PARSER_LOCK = Lock()


def _get_parser() -> Lark:
    """Construct the shared parser once; a failed construction can be retried."""

    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None:
            _PARSER = Lark(
                _GRAMMAR,
                parser="lalr",
                propagate_positions=True,
            )
        return _PARSER


@cache
def _ordinary_binding_name_is_valid(name: str) -> bool:
    """Apply the parser's existing immutable-binding name policy exactly."""

    probe = (
        f"fn __tuple_binding_probe(x:u8){{{name}=x {name}}} "
        "module __TupleBindingProbe{out y:u1 y=0}"
    )
    try:
        _get_parser().parse(probe)
    except UnexpectedInput:
        return False
    return True


def _tagged(item: object, tag: str) -> bool:
    return isinstance(item, tuple) and len(item) >= 2 and item[0] == tag


def _tagged_position(item: object) -> bool:
    return isinstance(item, tuple) and len(item) == 3 and item[0] == "position"


def parse(source: str) -> Module:
    """Parse one ZLang module."""

    try:
        tree = _get_parser().parse(source)
    except UnexpectedInput as error:
        context = error.get_context(source).strip()
        raise ParseError(
            f"syntax error at line {error.line}, column {error.column}: {context}"
        ) from error
    try:
        result = _AstBuilder().transform(tree)
    except VisitError as error:
        if isinstance(error.orig_exc, ParseError):
            raise error.orig_exc from error
        raise
    if not isinstance(result, Module):  # Defensive check at the parser boundary.
        raise ParseError("source did not produce a module")
    return result
