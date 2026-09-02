"""Fail-closed accounting for typed entities before backend dispatch."""

from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path

import pytest

from zlang.backend.module_features import (
    ModuleFeatureAccountingError,
    ModuleFeatureClaim,
    ModuleFeatureGroup,
    ModuleFeatureKind,
    claims_for_groups,
    claims_for_kinds,
    module_feature_groups,
    module_feature_inventory,
    validate_feature_claims,
)
from zlang.backend.clash import ClashEmissionError
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_experimental,
)
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


MIXED_SOURCES = {
    "globally controlled FIFO": """
module Mixed {
    clock clk reset rst
    in d:u8 in push:bit in pop:bit in en:bit out y:u8
    fifo q:fifo<u8,2>
    q.data=d q.push=push q.pop=pop
    reg r:u8=0
    when en { r <- d }
    y=r
}
""",
    "credit": """
module Mixed {
    clock clk reset rst
    in en:bit out tx:credit<u8,2> out y:u8
    reg r:u8=0
    when en { r <- 1 }
    tx.payload=0 tx.send=0 y=r
}
""",
    "vc credit": """
module Mixed {
    clock clk reset rst
    in en:bit out tx:vc_credit<u8,2,2> out y:u8
    reg r:u8=0
    when en { r <- 1 }
    tx.payload=0 tx.vc=0 tx.send=0 y=r
}
""",
}


@pytest.mark.parametrize(("engine", "source"), MIXED_SOURCES.items())
def test_legacy_specialized_emitters_reject_unclaimed_user_state(
    engine: str,
    source: str,
) -> None:
    module = compile_source(source, include_clash=False).ir
    inventory = module_feature_inventory(module)
    assert inventory.of_kind(ModuleFeatureKind.REGISTER)
    assert inventory.of_kind(ModuleFeatureKind.RULE)

    with pytest.raises(SystemVerilogEmissionError, match=engine) as caught:
        emit_experimental(module)
    assert caught.value.code == "ZL-BACKEND-SYSTEMVERILOG-UNCLAIMED-STATE"
    assert caught.value.semantic_path == ("Mixed",)
    assert "before artifact publication" in caught.value.notes[0]


def test_inventory_is_deterministic_and_distinguishes_entity_kinds() -> None:
    module = compile_source(MIXED_SOURCES["globally controlled FIFO"], include_clash=False).ir
    first = module_feature_inventory(module)
    second = module_feature_inventory(module)
    assert first == second
    assert {item.kind for item in first.features} >= {
        ModuleFeatureKind.ASSIGNMENT,
        ModuleFeatureKind.REGISTER,
        ModuleFeatureKind.RULE,
        ModuleFeatureKind.FIFO,
    }


def test_emission_claims_require_every_entity_exactly_once() -> None:
    module = compile_source(
        "module Accounted { in a:u8 out y:u8 local=a y=local }",
        include_clash=False,
    ).ir
    inventory = module_feature_inventory(module)
    complete = claims_for_kinds(
        inventory,
        "combinational",
        frozenset({ModuleFeatureKind.ASSIGNMENT, ModuleFeatureKind.LOCAL}),
    )
    validate_feature_claims(
        inventory, complete, backend="test", plan="combinational"
    )

    with pytest.raises(ModuleFeatureAccountingError, match="unclaimed=assignment"):
        validate_feature_claims(
            inventory,
            (),
            backend="test",
            plan="missing-assignment",
        )
    with pytest.raises(ModuleFeatureAccountingError, match="multiply_claimed"):
        validate_feature_claims(
            inventory,
            (*complete, ModuleFeatureClaim(complete[0].feature, "second-owner")),
            backend="test",
            plan="duplicate",
        )


def _request_response_hierarchy():
    return compile_source(
        (ROOT / "examples/hierarchical_request_response_m40.zhl").read_text(),
        include_clash=False,
    ).ir


def test_inventory_tracks_protocol_endpoints_and_request_response_ledger() -> None:
    module = _request_response_hierarchy()
    inventory = module_feature_inventory(module)
    groups = module_feature_groups(module)

    ledgers = inventory.of_kind(ModuleFeatureKind.REQUEST_RESPONSE_LEDGER)
    endpoints = inventory.of_kind(ModuleFeatureKind.PROTOCOL_ENDPOINT)
    assert tuple(item.identity for item in ledgers) == (
        module.request_response_connections[0].semantic_id,
    )
    assert len(endpoints) == len(module.protocol_endpoints)
    assert groups[ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS] == ledgers

    without_ledger = module_feature_inventory(
        replace(module, request_response_connections=())
    )
    assert without_ledger != inventory
    assert not without_ledger.of_kind(ModuleFeatureKind.REQUEST_RESPONSE_LEDGER)


def test_inventory_tracks_aggregate_endpoint_and_each_member_endpoint() -> None:
    module = compile_source(
        (ROOT / "examples/all_syntax.zhl").read_text(),
        top="AggregateProtocolSyntax",
        include_clash=False,
    ).ir
    inventory = module_feature_inventory(module)
    aggregate = inventory.of_kind(ModuleFeatureKind.AGGREGATE_ENDPOINT)
    member_endpoints = tuple(
        item
        for item in inventory.of_kind(ModuleFeatureKind.PROTOCOL_ENDPOINT)
        if item.identity.startswith("aggregate:")
    )
    assert len(aggregate) == len(module.aggregate_protocol_endpoints)
    assert len(member_endpoints) == sum(
        len(endpoint.members) for endpoint in module.aggregate_protocol_endpoints
    )


@pytest.mark.parametrize(
    ("backend_module", "error_type", "duplicate"),
    [
        ("zlang.backend.systemverilog.emitter", SystemVerilogEmissionError, False),
        ("zlang.backend.systemverilog.emitter", SystemVerilogEmissionError, True),
        ("zlang.backend.clash.emitter", ClashEmissionError, False),
        ("zlang.backend.clash.emitter", ClashEmissionError, True),
    ],
)
def test_artifact_publication_rejects_omitted_or_duplicate_rr_ledger_claim(
    monkeypatch: pytest.MonkeyPatch,
    backend_module: str,
    error_type: type[Exception],
    duplicate: bool,
) -> None:
    backend = importlib.import_module(backend_module)
    module = _request_response_hierarchy()
    real_claims = backend.claims_for_groups
    publication_attempted = False

    def broken_claims(module, contributor, groups):
        claims = real_claims(module, contributor, groups)
        ledger = next(
            claim
            for claim in claims
            if claim.feature.kind is ModuleFeatureKind.REQUEST_RESPONSE_LEDGER
        )
        if duplicate:
            return (*claims, ModuleFeatureClaim(ledger.feature, "duplicate-ledger"))
        return tuple(
            claim
            for claim in claims
            if claim.feature.kind is not ModuleFeatureKind.REQUEST_RESPONSE_LEDGER
        )

    def forbidden_publish(*args, **kwargs):
        nonlocal publication_attempted
        publication_attempted = True
        raise AssertionError("invalid emission plan reached artifact publication")

    monkeypatch.setattr(backend, "claims_for_groups", broken_claims)
    monkeypatch.setattr(backend, "publish_artifact", forbidden_publish)
    expected = "multiply_claimed" if duplicate else "unclaimed=request_response_ledger"
    with pytest.raises(error_type, match=expected):
        backend.emit_artifact(module)
    assert not publication_attempted


def test_group_claims_reject_repeated_concrete_contributor() -> None:
    module = _request_response_hierarchy()
    inventory = module_feature_inventory(module)
    claims = claims_for_groups(
        module,
        "hierarchy",
        (
            ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS,
            ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS,
        ),
    )
    with pytest.raises(ModuleFeatureAccountingError, match="multiply_claimed"):
        validate_feature_claims(
            inventory,
            claims,
            backend="test",
            plan="duplicate-contributor",
        )
