"""Declaration diagnostics preserve provenance without changing semantics."""

from pathlib import Path

import pytest

from zlang.ast import nodes as ast
from zlang.diagnostics import DiagnosticEdit, DiagnosticFix
from zlang.ir.cdc import ClockDomain
from zlang.semantic import SemanticError
from zlang.semantic.hierarchy import StateDomainResolver
from zlang.semantic.type_resolution import TypeResolver
from zlang.source import SourceOrigin, SourceSpan


@pytest.mark.parametrize("has_primary", [False, True])
def test_type_origin_binding_preserves_all_exception_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_primary: bool,
) -> None:
    origin = SourceOrigin(SourceSpan(4, 7, 4, 14), "type annotation", "types.zhl", "1" * 64)
    fix = DiagnosticFix("Replace type", (DiagnosticEdit(origin, "u8"),))
    error = SemanticError(
        "invalid type", code="ZL-TEST-TYPE", primary=origin if has_primary else None,
        notes=("note",), fixes=("fix",), machine_fixes=(fix,), physical_source=tmp_path,
    )
    resolver = TypeResolver((), (), source_unit="top.zhl", source_digest="0" * 64)

    def fail(_name: str):
        raise error

    monkeypatch.setattr(resolver, "_resolve_named", fail)
    with pytest.raises(SemanticError) as raised:
        resolver.resolve(ast.TypeName("Missing", origin=SourceSpan(8, 3, 8, 10)))
    assert raised.value is error
    assert error.code == "ZL-TEST-TYPE"
    assert error.notes == ("note",) and error.fixes == ("fix",)
    assert error.machine_fixes == (fix,) and error.physical_source == tmp_path
    assert error.primary == (
        origin if has_primary else SourceOrigin(
            SourceSpan(8, 3, 8, 10), "type annotation", "top.zhl", "0" * 64,
        )
    )


@pytest.mark.parametrize(
    ("domains", "requested", "inferred", "message"),
    [
        ((), None, (), "requires a clock and reset"),
        ((ClockDomain("a", "ar"),), "absent", (), "unknown clock domain"),
        ((ClockDomain("a", "ar"), ClockDomain("b", "br")), None, (), "ambiguous clock domain"),
        ((ClockDomain("a", "ar"), ClockDomain("b", "br")), None, ("a", "b"), "clock-domain mismatch"),
    ],
)
def test_state_domain_errors_keep_the_owning_declaration(
    domains, requested, inferred, message: str,
) -> None:
    origin = SourceOrigin(SourceSpan(7, 9, 7, 16), "register address", "bus.zhl")
    with pytest.raises(SemanticError, match=message) as raised:
        StateDomainResolver(domains).resolve(
            "register", "address", requested, inferred, origin=origin,
        )
    assert raised.value.primary == origin
