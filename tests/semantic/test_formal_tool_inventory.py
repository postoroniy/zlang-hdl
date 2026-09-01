from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from zlang.common.tool_inventory import (
    ToolInventory,
    discover_tool_inventory,
)
from zlang.equivalence import formal_tools_available
from zlang.compilation_session import CompilationSession
from zlang.formal import (
    FormalToolchainContext,
    _FORMAL_TOOLCHAIN_OVERRIDE,
    run_verilog_formal,
    tool_versions,
)
from zlang.formal_candidate import (
    M36ClashCandidateVerifier,
    PreparedCandidateEquivalence,
    _proof_bundle_tool_route,
)
from zlang.formal_exploration import (
    FormalExplorationConfig,
    FormalPolicy,
    proof_cache_key,
)
from zlang.formal_tooling import ClashToolContext, FormalToolResolver
from zlang.ir.formal import FormalStatus


VERSION_COMMANDS = (
    ("yosys", ("yosys", "-V")),
    ("sby", ("sby", "--version")),
    ("z3", ("z3", "-version")),
)


def test_available_inventory_is_ordered_versioned_and_immutable() -> None:
    outputs = {
        "yosys": "Yosys 0.68\nextra",
        "sby": "SBY v0.68",
        "z3": "Z3 version 4.8.12",
    }

    def runner(command, **_):
        return SimpleNamespace(stdout=outputs[command[0]], stderr="")

    inventory = discover_tool_inventory(
        ("yosys", "sby", "z3"),
        version_commands=VERSION_COMMANDS,
        which=lambda name: f"/tools/{name}",
        runner=runner,
    )

    assert inventory.available == ("yosys", "sby", "z3")
    assert inventory.missing == ()
    assert inventory.versions == (
        ("yosys", "Yosys 0.68"),
        ("sby", "SBY v0.68"),
        ("z3", "Z3 version 4.8.12"),
    )
    with pytest.raises(FrozenInstanceError):
        inventory.available = ()


def test_unavailable_inventory_retains_requested_order() -> None:
    inventory = discover_tool_inventory(
        ("yosys", "sby", "z3"), which=lambda _: None
    )

    assert inventory == ToolInventory(
        ("yosys", "sby", "z3"), (), ()
    )
    assert inventory.missing == ("yosys", "sby", "z3")


def test_partial_inventory_records_probe_failure_as_available() -> None:
    def locate(name):
        return None if name == "sby" else f"/tools/{name}"

    def runner(command, **_):
        if command[0] == "z3":
            raise OSError("version probe failed")
        return SimpleNamespace(stdout="Yosys 0.68", stderr="")

    inventory = discover_tool_inventory(
        ("yosys", "sby", "z3"),
        version_commands=VERSION_COMMANDS,
        which=locate,
        runner=runner,
    )

    assert inventory.available == ("yosys", "z3")
    assert inventory.missing == ("sby",)
    assert inventory.versions == (
        ("yosys", "Yosys 0.68"),
        ("z3", "available"),
    )


def test_existing_m35_and_m36_discovery_surfaces_remain_compatible() -> None:
    def formal_locate(name):
        return f"/tools/{name}" if name in {"yosys", "z3"} else None

    with patch("zlang.formal.shutil.which", side_effect=formal_locate), patch(
        "zlang.formal.subprocess.run",
        return_value=SimpleNamespace(stdout="version line\nignored", stderr=""),
    ):
        assert tool_versions() == (
            ("yosys", "version line"),
            ("z3", "version line"),
        )
        result = run_verilog_formal(
            "module top; endmodule\n", top="top", property_id="missing.partial"
        )

    assert result.status is FormalStatus.SKIPPED
    assert result.engine == "sby"
    assert result.solver == "z3"
    assert result.reason == "missing formal tools: sby, yosys-smtbmc"
    assert result.tool_versions == (
        ("yosys", "version line"),
        ("z3", "version line"),
    )

    with patch(
        "zlang.equivalence.shutil.which",
        side_effect=lambda name: (
            f"/tools/{name}" if name != "sby" else None
        ),
    ):
        assert formal_tools_available() == ("yosys", "yosys-smtbmc")


def test_truthy_path_compatibility_is_explicit() -> None:
    not_none = discover_tool_inventory(("yosys",), which=lambda _: "")
    truthy = discover_tool_inventory(
        ("yosys",), which=lambda _: "", require_truthy_path=True
    )

    assert not_none.available == ("yosys",)
    assert truthy.available == ()


def test_m35_inventory_versions_yosys_smtbmc_explicitly() -> None:
    available = {"yosys", "sby", "yosys-smtbmc", "z3"}
    with patch(
        "zlang.formal.shutil.which",
        side_effect=lambda name: f"/tools/{name}" if name in available else None,
    ), patch(
        "zlang.formal.subprocess.run",
        side_effect=lambda command, **_: SimpleNamespace(
            stdout=f"{command[0]} version", stderr=""
        ),
    ):
        versions = dict(tool_versions())

    assert versions["yosys-smtbmc"] == "yosys-smtbmc version"


def test_compilation_without_formal_policy_does_not_probe_formal_tools(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "zlang.formal_tooling.FormalToolchainContext.discover",
        lambda **_kwargs: pytest.fail("ordinary compilation probed formal tools"),
    )
    monkeypatch.setattr(
        "zlang.formal_tooling._discover_clash_context",
        lambda: pytest.fail("ordinary compilation probed Clash"),
    )
    session = CompilationSession(
        "module NoFormalProbe { in a:u8 out y:u8 y=a }",
        include_clash=False,
    )

    session.materialize()

    assert session.formal_tool_resolver.discovered_formal_routes == ()
    assert not session.formal_tool_resolver.clash_was_discovered


def test_m39_reuses_one_session_tool_snapshot_for_recipes_keys_and_execution(
    monkeypatch,
) -> None:
    calls = {"formal": 0, "clash": 0}

    def discover(*, engine: str, solver: str) -> FormalToolchainContext:
        calls["formal"] += 1
        assert (engine, solver) == ("sby", "z3")
        return FormalToolchainContext(
            engine,
            solver,
            ToolInventory(
                ("yosys", "sby", "yosys-smtbmc", "z3"),
                ("yosys", "sby", "yosys-smtbmc", "z3"),
                (
                    ("yosys", "Yosys test"),
                    ("sby", "SBY test"),
                    ("yosys-smtbmc", "smtbmc test"),
                    ("z3", "Z3 test"),
                ),
            ),
        )

    def clash() -> ClashToolContext:
        calls["clash"] += 1
        return ClashToolContext("/tools/clash", "Clash test")

    monkeypatch.setattr(
        "zlang.formal_tooling.FormalToolchainContext.discover",
        discover,
    )
    monkeypatch.setattr("zlang.formal_tooling._discover_clash_context", clash)
    resolver = FormalToolResolver()
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        tool_resolver=resolver,
    )
    candidate = SimpleNamespace(
        implementation_identity="candidate",
        semantic_identity="semantic",
        timing_relation=None,
    )
    common = dict(
        property_identity="property",
        artifact_hash="a" * 64,
        harness_hash="b" * 64,
        assumptions_identity="c" * 64,
        config=config,
    )

    first_key = proof_cache_key(candidate, **common)
    second_key = proof_cache_key(candidate, **common)
    first_route = _proof_bundle_tool_route(config)
    second_route = _proof_bundle_tool_route(config)
    bundle = PreparedCandidateEquivalence(
        object(),
        "module proof; endmodule\n",
        "proof",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "property",
        "assumptions",
        "backend",
    )
    verifier = M36ClashCandidateVerifier(SimpleNamespace(identity="reference"))
    monkeypatch.setattr(verifier, "_bundle", lambda *_args: bundle)
    execution_contexts = []

    def execute(*_args, **keywords):
        execution_contexts.append(_FORMAL_TOOLCHAIN_OVERRIDE.get())
        return SimpleNamespace(
            status=SimpleNamespace(value="bounded_pass"),
            depth=keywords["depth"],
            engine="sby",
            solver="z3",
            source_origin=None,
            selected_origin=None,
            reason=None,
            counterexample=None,
        )

    monkeypatch.setattr("zlang.formal_candidate.run_equivalence_formal", execute)
    first_result = verifier(candidate, config)
    second_result = verifier(candidate, config)
    assert first_key == second_key
    assert first_route == second_route
    assert first_route["formal_versions"][-1] == ["z3", "Z3 test"]
    assert "boolector" not in repr(first_route)
    assert first_result["status"] is FormalStatus.BOUNDED_PASS
    assert second_result["status"] is FormalStatus.BOUNDED_PASS
    assert execution_contexts == [
        resolver.formal_context(engine="sby", solver="z3"),
        resolver.formal_context(engine="sby", solver="z3"),
    ]
    assert calls == {"formal": 1, "clash": 1}


@pytest.mark.parametrize(
    "requested, available, versions, detail",
    (
        (
            ("a",),
            ("a", "a"),
            (),
            "available tool names",
        ),
        (("a", "b"), ("b", "a"), (), "requested order"),
        (
            ("a",),
            ("a",),
            (("a", "v1"), ("a", "v2")),
            "versioned tool names",
        ),
    ),
)
def test_inventory_rejects_ambiguous_order_or_duplicate_records(
    requested, available, versions, detail
) -> None:
    with pytest.raises(ValueError, match=detail):
        ToolInventory(requested, available, versions)
