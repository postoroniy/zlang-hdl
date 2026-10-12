"""Validation passes for immutable canonical optimization modules."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from zlang.ir.types import TaggedUnionType


def validate_declared_union_types(module: object) -> None:
    """Require every nominal union use to match the exact declaration table."""

    tagged_unions = tuple(getattr(module, "tagged_unions"))
    declaration_names = tuple(item.name for item in tagged_unions)
    declaration_identities = tuple(
        item.declaration_identity for item in tagged_unions
    )
    if len(declaration_names) != len(set(declaration_names)):
        raise ValueError("canonical tagged-union declaration names must be unique")
    if len(declaration_identities) != len(set(declaration_identities)):
        raise ValueError(
            "canonical tagged-union declaration identities must be unique"
        )
    declared = {item.declaration_identity: item for item in tagged_unions}

    def validate(value: object, seen: set[int]) -> None:
        if isinstance(value, TaggedUnionType):
            declaration = declared.get(value.declaration_identity)
            if declaration is None or declaration != value:
                raise ValueError(
                    f"tagged-union type '{value.name}' is absent from the "
                    "exact canonical declaration table"
                )
            return
        if isinstance(value, tuple):
            for item in value:
                validate(item, seen)
            return
        if is_dataclass(value) and not isinstance(value, type):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            for descriptor in fields(value):
                if descriptor.name in {"origin", "source_origin", "children"}:
                    continue
                validate(getattr(value, descriptor.name), seen)

    validate(
        (
            getattr(module, "ports"),
            getattr(module, "expressions"),
            getattr(module, "entities"),
            getattr(module, "functions"),
            getattr(module, "registers"),
            getattr(module, "next_assignments"),
            getattr(module, "request_responses"),
            getattr(module, "connections"),
            getattr(module, "rules"),
            getattr(module, "fifos"),
            getattr(module, "memories"),
            getattr(module, "roms"),
            getattr(module, "locals"),
            getattr(module, "instance_bindings"),
            getattr(module, "elaborated_instances"),
            getattr(module, "protocol_endpoints"),
            getattr(module, "hierarchical_connections"),
            getattr(module, "request_response_connections"),
            getattr(module, "protocol_schemas"),
            getattr(module, "aggregate_protocol_endpoints"),
            getattr(module, "aggregate_protocol_connections"),
            getattr(module, "callable_definitions"),
            getattr(module, "module_signature"),
            getattr(module, "external_contract"),
        ),
        set(),
    )


__all__ = ["validate_declared_union_types"]
