from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from zlang.compiler import compile_source
from zlang.parser import ParseError
from zlang.public_capabilities import CAPABILITY_REGISTRY
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=None)
def _compile_witness(source_path: str, top: str):
    path = ROOT / source_path
    return compile_source(
        path.read_text(),
        top=top,
        include_clash=False,
        source_unit=str(path),
    )


def test_registry_is_versioned_unique_and_deterministic() -> None:
    assert CAPABILITY_REGISTRY.schema_version == 24
    assert CAPABILITY_REGISTRY.production_backend == "direct_systemverilog"
    surface = CAPABILITY_REGISTRY.editor_surface()
    assert tuple(surface) == ("keywords", "types", "intrinsics", "modes", "operators")
    for category, spellings in surface.items():
        assert spellings, f"public capability category is empty: {category}"
        assert len(spellings) == len(set(spellings)), (
            f"duplicate public spelling in {category}"
        )

    capabilities = CAPABILITY_REGISTRY.capability_matrix()
    assert capabilities == CAPABILITY_REGISTRY.capability_matrix()
    assert [item["name"] for item in capabilities] == [
        item.name for item in CAPABILITY_REGISTRY.capabilities
    ]
    assert len(capabilities) == len({item["name"] for item in capabilities})
    for item in capabilities:
        assert item["context"]
        assert item["status"] in {"supported", "bounded"}
        assert item["simulator"]
        assert item["clash"] == "retired"
        assert item["direct_systemverilog"]
        assert item["formal"]
        assert item["witness"]["source_path"]
        assert item["witness"]["top"]
        assert item["limitations"]


@pytest.mark.parametrize(
    "capability",
    CAPABILITY_REGISTRY.capabilities,
    ids=lambda item: item.name,
)
def test_advertised_capability_witness_is_semantically_executable(
    capability,
) -> None:
    witness = capability.witness
    path = ROOT / witness.source_path
    assert path.is_file(), f"missing witness for {capability.name}: {witness.source_path}"
    result = _compile_witness(witness.source_path, witness.top)
    assert result.ir.name == witness.top


def test_registry_documentation_contract_is_satisfied() -> None:
    seen: set[str] = set()
    for requirement in CAPABILITY_REGISTRY.documentation:
        assert requirement.capability not in seen
        seen.add(requirement.capability)
        path = ROOT / requirement.document
        assert path.is_file(), f"missing capability document: {requirement.document}"
        text = path.read_text()
        for marker in requirement.markers:
            assert marker in text, (
                f"{requirement.capability!r} is missing documentation marker "
                f"{marker!r} in {requirement.document}"
            )


@pytest.mark.parametrize(
    "family,source",
    (
        ("hardware", "module M { in a,b:u8 in s:bit out y:u8 y=mux(s,a,b) }"),
        ("compile-time", "module M { in xs:vec<4,u8> out y:u3 y=length(xs) }"),
        (
            "fixed-conversion",
            "module M { in x:fixed<8,4> out y:fixed<8,2> "
            "y=fixed_truncate_wrap(x) }",
        ),
        ("functional", "module M { in xs:vec<2,u8> out y:u9 y=sum(xs) }"),
        (
            "guard",
            "equiv E { x | zero<x> <=> x when unsigned(x) && width(x) == 8 } "
            "module M { in x:u8 out y:u8 y=x }",
        ),
        (
            "enum",
            "enum Phase { Idle Active } module M { out y:Phase y=Phase.Active }",
        ),
        (
            "tagged-union",
            "union U { Empty Value { x:u1 } } module M { out y:u1 "
            "u:U=U.Value { x=1 } y=match u { U.Empty=>0 U.Value { x }=>x } }",
        ),
        (
            "physical-clock-reset",
            "module M { clock clk { edge falling } reset rst_n @clk { "
            "mode asynchronous polarity active_low power_up unspecified } "
            "out y:u1 reg q:u1=0 y=q }",
        ),
        (
            "packing",
            "module M { in x:u8 out y:u8 "
            "raw:bits<8>=concat(x[7:4],zeros<2>,ones<2>) "
            "y=bitcast<u8>(raw) }",
        ),
        (
            "aggregate-ergonomics",
            "struct Pair { a:u8 b:u8 } module M { in x:u8 out y:Pair "
            "base:Pair=Pair { a=x b=x } y=base with { b=1 } }",
        ),
        (
            "collection-ergonomics",
            "module M { in x:u8 out y:vec<2,u8> y=repeat(x) }",
        ),
    ),
)
def test_representative_registered_intrinsic_families_compile(
    family: str, source: str
) -> None:
    # Capability conformance is a parser/semantic contract.  Some accepted
    # physical contracts intentionally fail closed in backends that cannot yet
    # implement them, so do not make the editor registry depend on Clash.
    assert compile_source(source, include_clash=False).ir.name == "M", family


@pytest.mark.parametrize("name", ("resize", "zero_extend", "sign_extend"))
def test_future_conversion_spellings_are_not_public_intrinsics(name: str) -> None:
    assert name not in CAPABILITY_REGISTRY.intrinsics
    source = f"module M {{ in x:u8 out y:u8 y={name}(x) }}"
    with pytest.raises((ParseError, SemanticError)):
        compile_source(source)


def test_editor_tests_use_only_repository_owned_extension_files() -> None:
    editor_tests = (ROOT / "tests" / "editor").glob("test_*.py")
    forbidden = ("/" + ".vscode" + "/", "\\" + ".vscode" + "\\")
    for path in editor_tests:
        text = path.read_text()
        assert not any(fragment in text for fragment in forbidden), (
            f"{path.relative_to(ROOT)} depends on workspace-local VS Code settings"
        )


def test_initialized_rom_surface_is_registry_owned() -> None:
    surface = CAPABILITY_REGISTRY.editor_surface()
    assert {"rom", "init", "read_latency"} <= set(surface["keywords"])
    assert "rom" in surface["types"]


def test_domain_is_semantic_terminology_not_a_source_keyword() -> None:
    assert "domain" not in CAPABILITY_REGISTRY.keywords
    result = compile_source(
        "module M { in domain:u8 out y:u8 y=domain }",
        include_clash=False,
    )
    assert result.ir.ports[0].name == "domain"


def test_physical_domain_and_tagged_union_spellings_are_registry_owned() -> None:
    assert {
        "union", "match", "async", "edge", "mode", "polarity", "power_up"
    } <= set(
        CAPABILITY_REGISTRY.keywords
    )
    assert {
        "rising", "falling", "synchronous", "asynchronous",
        "active_high", "active_low", "unspecified",
    } <= set(CAPABILITY_REGISTRY.modes)

    reset = next(
        item for item in CAPABILITY_REGISTRY.capabilities
        if item.name == "physical-clock-reset"
    )
    assert "synchronous/raw-asynchronous" in reset.formal
    assert "synchronized-release" in reset.formal
    assert "legacy/default contracts only" not in reset.formal


def test_new_generic_table_and_elastic_surfaces_are_registry_owned() -> None:
    assert "transform" in CAPABILITY_REGISTRY.keywords
    names = {item.name for item in CAPABILITY_REGISTRY.capabilities}
    assert {
        "typed-static-parameters",
        "generic-rom-and-table-gather",
        "runtime-instance-output-projection",
        "elastic-ready-valid-pipeline",
    } <= names


def test_concise_exact_lowering_surface_is_registry_owned() -> None:
    assert "index_width" in CAPABILITY_REGISTRY.intrinsics
    capability = next(
        item for item in CAPABILITY_REGISTRY.capabilities
        if item.name == "concise-exact-lowering"
    )
    result = _compile_witness(
        capability.witness.source_path,
        capability.witness.top,
    )
    assert result.ir.parameters[-1] == ("IW", "value", 2)


def test_recursive_atomic_action_surface_is_registry_owned_and_executable() -> None:
    capability = next(
        item for item in CAPABILITY_REGISTRY.capabilities
        if item.name == "sequential-state"
    )
    assert "one Rule and ActionGroup" in capability.limitations[0]
    assert "without readiness-selected fallback" in capability.limitations[1]
    assert "output writes" in capability.limitations[1]

    result = _compile_witness(
        capability.witness.source_path,
        capability.witness.top,
    )
    rule = next(item for item in result.ir.rules if item.name == "fault_update")
    assert rule.actions
    assert all(action.activation is not None for action in rule.actions)
    assert result.ir.resolved_transition is not None
    group = result.ir.resolved_transition.group("fault_update")
    assert len(group.actions) == len(rule.actions)


def test_exact_literal_and_packed_constant_surface_is_registry_owned() -> None:
    assert {"zeros", "ones"} <= set(CAPABILITY_REGISTRY.intrinsics)
    names = {item.name for item in CAPABILITY_REGISTRY.capabilities}
    assert "exact-literals-and-packed-constants" in names


def test_first_class_verification_surface_is_registry_owned_and_executable() -> None:
    assert {
        "assert", "cover", "contract", "require", "ensure", "assume", "guarantee"
    } <= set(CAPABILITY_REGISTRY.keywords)
    capability = next(
        item for item in CAPABILITY_REGISTRY.capabilities if item.name == "contracts"
    )
    result = _compile_witness(
        capability.witness.source_path,
        capability.witness.top,
    )
    assert [scope.name for scope in result.ir.verification_scopes] == [
        "$module", "public_behavior"
    ]
    assert "per-goal supported clock/reset routing" in capability.limitations[0]


def test_exploration_capability_distinguishes_advisory_and_required_formal() -> None:
    capability = next(
        item for item in CAPABILITY_REGISTRY.capabilities
        if item.name == "exploration"
    )
    assert "`available` is advisory" in capability.formal
    assert "required policies require connected M36" in capability.formal
    assert any("M38 never gates M39" in item for item in capability.limitations)
