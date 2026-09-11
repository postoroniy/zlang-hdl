"""Version-10 physical clock/reset contract publication regressions."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from zlang.backend.manifest import (
    BackendArtifact,
    MANIFEST_VERSION,
    PHYSICAL_DOMAIN_MANIFEST_VERSION,
)
from zlang.backend.systemverilog import emit_artifact, emit_target_artifact
from zlang.compiler import compile_source
from zlang.ir.equivalence import SignalRole
from zlang.ir.recursive_formal import build_recursive_formal_design


ASYNC = """
module AsyncCounter {
  clock clk
  async reset arst_n @clk { polarity active_low }
  in enable : bit
  out y : u8
  reg q : u8 = 0
  step: when enable { q <- truncate<8>(q + 1) }
  y = q
}
"""


LEGACY = """
module SyncCounter {
  clock clk reset rst
  in enable : bit
  out y : u8
  reg q : u8 = 0
  step: when enable { q <- truncate<8>(q + 1) }
  y = q
}
"""


def _async_artifact() -> BackendArtifact:
    module = compile_source(ASYNC).ir
    return emit_artifact(module)


def test_nonlegacy_domain_publishes_exact_version_10_contract() -> None:
    artifact = _async_artifact()

    assert artifact.manifest_version == PHYSICAL_DOMAIN_MANIFEST_VERSION
    assert len(artifact.physical_domains) == 1
    domain = artifact.physical_domains[0]
    assert domain.clock == "clk"
    assert domain.reset == "arst_n"
    assert domain.rtl_module == "AsyncCounter"
    assert domain.rtl_clock_path == "clk"
    assert domain.rtl_reset_path == "arst_n"
    assert domain.clock_edge == "rising"
    assert domain.reset_mode == "asynchronous"
    assert domain.reset_polarity == "active_low"
    assert domain.reset_release_mode == "synchronized"
    assert domain.reset_release_cycles == 2
    assert domain.power_up == "unspecified"
    assert domain.source_origin is not None

    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.to_json() == artifact.to_json()
    assert restored.physical_domains == artifact.physical_domains
    assert restored.build_identity == artifact.build_identity




def test_legacy_default_domain_preserves_manifest_version_and_shape() -> None:
    module = compile_source(LEGACY).ir
    artifact = emit_artifact(module)
    data = json.loads(artifact.to_json())

    assert artifact.manifest_version == MANIFEST_VERSION
    assert artifact.physical_domains == ()
    assert "physical_domains" not in data


def test_physical_paths_participate_in_build_identity_but_origin_does_not() -> None:
    artifact = _async_artifact()
    domain = artifact.physical_domains[0]
    without_origin = replace(
        artifact,
        physical_domains=(replace(domain, source_origin=None),),
    )
    assert without_origin.build_identity == artifact.build_identity

    data = json.loads(artifact.to_json())
    data["physical_domains"][0]["rtl_clock_path"] = "external_clk"
    clock_binding = next(
        item for item in data["bindings"] if item["role"] == "clock"
    )
    clock_binding["rtl_path"] = "external_clk"
    data.pop("build_identity")
    rebound = BackendArtifact.from_json(data)
    assert rebound.build_identity != artifact.build_identity


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda data: data["physical_domains"][0].update(identity="0" * 64),
            "identity does not match",
        ),
        (
            lambda data: data["physical_domains"][0].update(
                rtl_reset_path="wrong_reset"
            ),
            "reset locator does not match",
        ),
        (
            lambda data: data["physical_domains"][0].update(
                rtl_module="WrongTop"
            ),
            "RTL module does not match its artifact",
        ),
        (
            lambda data: data["physical_domains"][0].update(
                reset_release_cycles=1
            ),
            "exactly two cycles",
        ),
        (
            lambda data: data["physical_domains"][0].update(extra=True),
            "contains unsupported field",
        ),
        (
            lambda data: data.update(physical_domains=[]),
            "requires at least one physical domain",
        ),
    ),
)
def test_malformed_physical_domain_is_rejected(mutate, message: str) -> None:
    data = json.loads(_async_artifact().to_json())
    mutate(data)
    data.pop("build_identity", None)

    with pytest.raises(ValueError, match=message):
        BackendArtifact.from_json(data)


def test_physical_domain_requires_version_10() -> None:
    data = json.loads(_async_artifact().to_json())
    data["manifest_version"] = PHYSICAL_DOMAIN_MANIFEST_VERSION - 1
    data.pop("build_identity")

    with pytest.raises(ValueError, match="requires manifest version 10"):
        BackendArtifact.from_json(data)


def test_named_signature_carries_release_contract() -> None:
    source = """
interface AsyncIfc {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
}
module Async : AsyncIfc { y = x }
"""
    module = compile_source(source).ir
    artifact = emit_artifact(module)
    signature = artifact.module_signature

    assert signature is not None
    domain = signature.signature_data["clock_domains"][0]
    assert domain["reset_release_mode"] == "synchronized"
    assert domain["reset_release_cycles"] == 2
    assert artifact.manifest_version == PHYSICAL_DOMAIN_MANIFEST_VERSION


def test_recursive_components_and_instances_link_one_physical_domain() -> None:
    source = """
module Child {
  clock clk
  async reset arst @clk
  out y : u8
  reg q : u8 = 0
  q <- q
  y = q
}
module Top {
  clock clk
  async reset arst @clk
  out y : u8
  child : Child
  y = child.y
}
"""
    module = compile_source(source, top="Top").ir
    recursive = build_recursive_formal_design(module)
    artifact = emit_artifact(module, recursive_design=recursive)
    domain_identity = artifact.physical_domains[0].identity

    assert artifact.components
    assert artifact.instances
    assert all(
        item.physical_domain_identity == domain_identity
        for item in artifact.components
    )
    assert all(
        item.physical_domain_identity == domain_identity
        for item in artifact.instances
    )
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.components == artifact.components
    assert restored.instances == artifact.instances

    data = json.loads(artifact.to_json())
    data["instances"][0]["physical_domain_identity"] = "0" * 64
    data.pop("build_identity")
    with pytest.raises(ValueError, match="unknown physical domain"):
        BackendArtifact.from_json(data)


def test_public_domain_record_resolves_exact_clock_and_reset_bindings() -> None:
    artifact = _async_artifact()
    domain = artifact.physical_domains[0]
    clock = tuple(item for item in artifact.bindings if item.role is SignalRole.CLOCK)
    reset = tuple(item for item in artifact.bindings if item.role is SignalRole.RESET)

    assert len(clock) == len(reset) == 1
    assert clock[0].rtl_path == domain.rtl_clock_path
    assert reset[0].rtl_path == domain.rtl_reset_path
    assert clock[0].clock_domain == reset[0].clock_domain == domain.clock
    assert clock[0].reset_domain == reset[0].reset_domain == domain.reset


def test_generic_target_publication_preserves_newer_physical_manifest() -> None:
    result = compile_source(
        ASYNC, target="generic"
    )
    artifact = emit_target_artifact(result.ir, result.implementation_graph)

    assert artifact.implementation is not None
    assert artifact.manifest_version == PHYSICAL_DOMAIN_MANIFEST_VERSION
    assert len(artifact.physical_domains) == 1
