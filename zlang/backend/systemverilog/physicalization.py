"""Emission-only physical naming for typed generic callables."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from enum import Enum
import hashlib

from zlang.ir import expressions as expr
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir import module as ir_module
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError

_PHYSICAL_CALLABLE_SCHEMA = "zlang-systemverilog-physical-callable-v1"


def validated_hierarchy(
    module: ir_module.Module,
    *,
    cache: ir_hierarchy.HierarchyTraversalCache | None = None,
) -> ir_hierarchy.HierarchyIndex:
    """Build the physical hierarchy and translate structural failures."""

    try:
        return ir_hierarchy.build_hierarchy_index(module, cache=cache)
    except ir_hierarchy.HierarchyError as error:
        raise SystemVerilogEmissionError(str(error)) from error


def physicalize_generic_callables(module: ir_module.Module) -> ir_module.Module:
    """Return an emission-only module with provenance-free helper names.

    Semantic callable identities remain authoritative on the input IR and in
    BackendArtifact metadata.  This copy only prevents dependency/source
    provenance from becoming an RTL identifier.  Nested calls use the physical
    identity of the referenced typed body, so a dependency-sensitive callee ID
    cannot leak indirectly through a caller's body fingerprint.
    """

    definitions: dict[str, object] = {}
    specializations: dict[str, object] = {}
    collected_modules: dict[int, ir_module.Module] = {}

    def collect(current: ir_module.Module) -> None:
        cached = collected_modules.get(id(current))
        if cached is current:
            return
        collected_modules[id(current)] = current
        for function in (*current.functions, *current.callable_definitions):
            identity = str(function.callee_identity)
            # A hierarchy may carry the same semantic definition through
            # several specialized children with different diagnostic origins.
            # The semantic identity is authoritative; each child still runs
            # the normal local reachability/conflict validation before text is
            # published.
            definitions.setdefault(identity, function)
        for specialization in current.generic_specializations:
            specializations.setdefault(specialization.identity, specialization)
        for child in current.children:
            collect(child)

    collect(module)
    memo: dict[str, str] = {}
    active: set[str] = set()

    def normalize(value: object) -> object:
        if isinstance(value, expr.Call):
            target = definitions.get(value.callee_identity)
            callee = (
                digest(target)
                if target is not None
                else value.callee_identity
            )
            return (
                "Call",
                callee,
                tuple(normalize(item) for item in value.arguments),
                normalize(value.type),
            )
        if isinstance(value, Enum):
            return (type(value).__module__, type(value).__name__, value.value)
        if isinstance(value, tuple):
            return tuple(normalize(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value).__module__,
                type(value).__name__,
                tuple(
                    (item.name, normalize(getattr(value, item.name)))
                    for item in fields(value)
                    if item.name not in {
                        "origin",
                        "source_origin",
                        "formal_records",
                        "formal_eligible",
                        "callee_identity",
                    }
                ),
            )
        return value

    def digest(function: object) -> str:
        semantic_identity = str(getattr(function, "callee_identity"))
        cached = memo.get(semantic_identity)
        if cached is not None:
            return cached
        if semantic_identity in active:
            raise SystemVerilogEmissionError(
                "recursive typed callable cycle while deriving physical helper names"
            )
        active.add(semantic_identity)
        metadata = getattr(function, "metadata", None)
        source_name = (
            str(getattr(metadata, "source_name", ""))
            if metadata is not None
            else str(getattr(function, "name"))
        )
        specialization = specializations.get(semantic_identity)
        binding_payloads: dict[str, object] = {}
        if specialization is not None:
            for binding in specialization.bindings:
                if binding.kind.value == "constant":
                    binding_payloads[binding.name] = (
                        "constant",
                        normalize(binding.canonical_type),
                        normalize(binding.canonical_value),
                        binding.evaluator_schema,
                    )
                else:
                    target = definitions.get(binding.callee_identity)
                    binding_payloads[binding.name] = (
                        "callable",
                        tuple(normalize(item) for item in binding.parameter_types),
                        normalize(binding.return_type),
                        digest(target) if target is not None else binding.callee_identity,
                        binding.evaluator_schema,
                    )
        generic_arguments = (
            tuple(
                (name, binding_payloads.get(name, rendered))
                for name, rendered in specialization.arguments
            )
            if specialization is not None
            else tuple(getattr(metadata, "arguments", ()))
        )
        payload = (
            _PHYSICAL_CALLABLE_SCHEMA,
            source_name,
            generic_arguments,
            tuple(
                (parameter.name, normalize(parameter.type))
                for parameter in getattr(function, "parameters")
            ),
            normalize(getattr(function, "return_type")),
            normalize(getattr(function, "body")),
        )
        result = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        active.remove(semantic_identity)
        memo[semantic_identity] = result
        return result

    generic_identities = {
        identity
        for identity, function in definitions.items()
        if str(getattr(function, "name", "")).startswith("zlang_spec_")
        or (
            getattr(function, "metadata", None) is not None
            and getattr(function.metadata, "specialization_identity", None) is not None
        )
    }
    physical = {identity: digest(definitions[identity]) for identity in generic_identities}
    if not physical:
        return module

    rewritten_objects: dict[int, object] = {}

    def rewrite(value: object) -> object:
        cache_key = id(value)
        cached = rewritten_objects.get(cache_key)
        if cached is not None:
            return cached
        if isinstance(value, expr.Call):
            arguments = tuple(rewrite(item) for item in value.arguments)
            replacement = physical.get(value.callee_identity)
            if replacement is None:
                result = replace(value, arguments=arguments)
            else:
                result = replace(
                    value,
                    function=f"zlang_spec_{replacement}",
                    arguments=arguments,
                    callee_identity=replacement,
                )
            rewritten_objects[cache_key] = result
            return result
        if isinstance(value, ir_module.Function):
            body = rewrite(value.body)
            identity = value.callee_identity
            replacement = physical.get(identity)
            if replacement is None:
                result = replace(value, body=body)
            else:
                assert value.metadata is not None
                result = replace(
                    value,
                    name=f"zlang_spec_{replacement}",
                    body=body,
                    callee_identity=replacement,
                    metadata=replace(
                        value.metadata,
                        specialization_identity=replacement,
                    ),
                )
            rewritten_objects[cache_key] = result
            return result
        if isinstance(value, tuple):
            result = tuple(rewrite(item) for item in value)
            rewritten_objects[cache_key] = result
            return result
        if is_dataclass(value) and not isinstance(value, type):
            field_names = {item.name for item in fields(value)}
            updates = {
                item.name: rewrite(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name not in {"type", "origin", "source_origin"}
            }
            # Functional reduction descriptors carry callable references but
            # are not Function definitions.  Rewrite that exact typed pair;
            # leave specialization-binding provenance records untouched.
            if {"function", "callee_identity"} <= field_names:
                identity = str(getattr(value, "callee_identity", ""))
                replacement = physical.get(identity)
                if replacement is not None:
                    updates["function"] = f"zlang_spec_{replacement}"
                    updates["callee_identity"] = replacement
            result = replace(value, **updates) if updates else value
            rewritten_objects[cache_key] = result
            return result
        return value

    rewritten = rewrite(module)
    if not isinstance(rewritten, ir_module.Module):
        raise SystemVerilogEmissionError("physical callable lowering lost module type")
    return rewritten
