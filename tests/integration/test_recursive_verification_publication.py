"""Compiler-bundle publication for existing recursive M35 properties.

These tests intentionally exercise the public compilation/publication path,
not the lower-level recursive-formal backend helpers.  Descendant M35
properties already exist in ``RecursiveFormalDesign``; the compiler bundle
must publish each concrete property exactly once with its physical instance
scope and an all-or-nothing observation route.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)


REGISTER_SOURCE = """
module CounterChild {
    clock clk reset rst
    in enable : bit
    out value : u3
    reg count : u3 = 0
    count <- mux(enable, truncate<3>(count + 1), count)
    value = count
}

module RegisterFormalTop {
    clock clk reset rst
    in enable : bit
    out value : u3
    child : CounterChild { enable = enable }
    value = child.value

    // Keep one root goal so this fixture remains valid while descendant-only
    // bundle publication is introduced independently.
    assert public_value @ clk { value == value }
}
"""


FIFO_SOURCE = """
module FifoChild {
    clock clk reset rst
    in data : u8
    in push : bit
    in pop : bit
    out count : u3
    fifo queue : fifo<u8,4>
    queue.data = data
    queue.push = push
    queue.pop = pop
    count = queue.count
}

module FifoFormalTop {
    clock clk reset rst
    in data : u8
    in push : bit
    in pop : bit
    out count : u3
    child : FifoChild { data = data push = push pop = pop }
    count = child.count
    assert public_count @ clk { count <= 4 }
}
"""


SIBLING_SOURCE = """
module CounterChild {
    clock clk reset rst
    in enable : bit
    out value : u3
    reg count : u3 = 0
    count <- mux(enable, truncate<3>(count + 1), count)
    value = count
}

module SiblingFormalTop {
    clock clk reset rst
    in enable : bit
    out value : u3
    child0 : CounterChild { enable = enable }
    child1 : CounterChild { enable = enable }
    value = child0.value
    assert public_value @ clk { value == value }
}
"""


ASSUMED_CHILD_SOURCE = """
module AssumedChild {
    clock clk reset rst
    in enable : bit
    out value : u3
    reg count : u3 = 0
    count <- mux(enable, truncate<3>(count + 1), count)
    value = count
    assume enabled_environment @ clk disable iff rst { enable }
}

module AssumedChildTop {
    clock clk reset rst
    in enable : bit
    out value : u3
    child : AssumedChild { enable = enable }
    value = child.value
    assert public_value @ clk { value == value }
}
"""


INTERNAL_READY_VALID_SOURCE = """
module InternalProducer {
    clock clk reset rst
    out tx : rv<u8>
    tx.payload = 7
    tx.valid = 1
}

module InternalConsumer {
    clock clk reset rst
    in allow : bit
    in rx : rv<u8>
    out count : u4
    reg accepted : u4 = 0
    rx.ready = allow
    accepted <- mux(rx.transfer, truncate<4>(accepted + 1), accepted)
    count = accepted
}

module InternalReadyValidTop {
    clock clk reset rst
    in allow : bit
    out count : u4
    producer : InternalProducer
    consumer : InternalConsumer { allow = allow }
    producer.tx -> consumer.rx
    count = consumer.count
    assert public_count @ clk { count == count }
}
"""


INTERNAL_USER_ASSUMPTION_SOURCE = INTERNAL_READY_VALID_SOURCE.replace(
    "    count = accepted\n}",
    "    count = accepted\n"
    "    assume input_present @ clk disable iff rst { rx.valid }\n}",
)


BUFFERED_READY_VALID_SOURCE = INTERNAL_READY_VALID_SOURCE.replace(
    "    producer.tx -> consumer.rx\n",
    "    producer.tx -> consumer.rx { buffer 1 }\n",
)


ROOT_SEQUENTIAL_READY_VALID_SOURCE = """
module RootSequentialReadyValid {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    reg held : u8 = 0
    reg valid : bit = 0

    input.ready = !valid | output.ready
    output.payload = held
    output.valid = valid
    held <- input.transfer ? input.payload : held
    valid <- input.ready ? input.valid : valid
}
"""


def _compile(source: str, top: str):
    return compile_source(source, top=top, include_clash=False)


def _payload(directory: Path) -> dict[str, object]:
    return json.loads(
        (directory / "verification-ir.json").read_text(encoding="utf-8")
    )["payload"]


def _descendant_properties(compilation):
    design = compilation.recursive_formal_design
    assert design is not None
    return tuple(
        item
        for item in design.properties
        if len(item.physical_instance_path) > 1
    )


def test_root_automatic_requirement_is_declared_for_every_physical_domain_goal(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        ROOT_SEQUENTIAL_READY_VALID_SOURCE,
        "RootSequentialReadyValid",
    )
    directory = tmp_path / "root-ready-valid"
    manifest = publish_compilation_verification_bundle(compilation, directory)
    payload = _payload(directory)

    (assumption,) = tuple(
        item for item in compilation.formal_design.properties
        if item.kind.value == "assumption"
    )
    module_scope = next(
        item for item in payload["scopes"] if item["name"] == "$module"
    )
    assert (module_scope["clock"], module_scope["reset"]) == ("clk", "rst")
    assert [item["id"] for item in module_scope["requirements"]] == [
        assumption.id
    ]

    root_jobs = tuple(
        item for item in manifest.jobs
        if item.physical_instance_path == ("RootSequentialReadyValid",)
    )
    assert root_jobs
    assert all(item.assumption_ids == (assumption.id,) for item in root_jobs)
    assert all((item.clock_domain, item.reset_domain) == ("clk", "rst")
               for item in root_jobs)


def _assert_descendants_are_published_once(
    compilation,
    directory: Path,
) -> None:
    descendants = _descendant_properties(compilation)
    assert descendants
    expected_ids = tuple(item.concrete_property_id for item in descendants)
    expected = set(expected_ids)

    manifest = publish_compilation_verification_bundle(compilation, directory)
    payload = _payload(directory)
    jobs = tuple(item for item in manifest.jobs if item.property_id in expected)
    properties = tuple(
        item for item in payload["properties"] if item["id"] in expected
    )
    plans = tuple(
        item
        for item in payload["execution_plan"]["goals"]
        if item["property_identity"] in expected
    )

    assert len(expected_ids) == len(expected), "concrete property IDs must be unique"
    published_job_ids = [item.property_id for item in jobs]
    assert all(published_job_ids.count(item) == 1 for item in expected_ids)
    assert {item.property_id for item in jobs} == expected
    assert {item["id"] for item in properties} == expected
    assert {item["property_identity"] for item in plans} == expected
    assert len(jobs) == len(properties) == len(plans) == len(expected)

    scopes = {item["id"]: item for item in payload["scopes"]}
    binding_sets = {item["route"]: item for item in payload["binding_sets"]}
    by_id = {item.concrete_property_id: item for item in descendants}
    plan_by_id = {item["property_identity"]: item for item in plans}
    for job in jobs:
        concrete = by_id[job.property_id]
        plan = plan_by_id[job.property_id]
        expected_scope = f"recursive-scope:{concrete.instance_identity}"
        assert job.physical_instance_path == concrete.physical_instance_path
        assert job.scope_id == expected_scope
        assert expected_scope in scopes
        assert [
            item["id"] for item in scopes[expected_scope]["goals"]
        ].count(job.property_id) == 1
        assert job.executable
        assert job.route is not None
        assert plan["route"] is not None
        assert plan["skip_reason"] is None

        # Executability is earned only by one exact backend-local binding set.
        route_bindings = {
            item["semantic_signal_id"]
            for item in binding_sets[job.route]["bindings"]
        }
        assert set(plan["required_observations"]) <= route_bindings


def test_nested_register_properties_publish_once_with_path_scope_and_bindings(
    tmp_path: Path,
) -> None:
    compilation = _compile(REGISTER_SOURCE, "RegisterFormalTop")
    descendants = _descendant_properties(compilation)
    assert len(descendants) == 2
    assert {
        item.property.generated_from for item in descendants
    } == {"register:count", "register:count:reset"}

    _assert_descendants_are_published_once(compilation, tmp_path / "bundle")


def test_nested_async_reset_epoch_uses_the_conditioned_root_reset(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        REGISTER_SOURCE.replace("reset rst", "async reset rst @clk"),
        "RegisterFormalTop",
    )
    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, directory)

    # Property identities are hashes, so identify the concrete reset-epoch goal
    # through the recursive semantic record and then inspect its exact harness.
    recursive = compilation.recursive_formal_design
    assert recursive is not None
    reset_property_id = next(
        item.concrete_property_id
        for item in recursive.properties
        if item.physical_instance_path == ("RegisterFormalTop", "child")
        and item.property.generated_from == "register:count:reset"
    )
    reset_epoch = next(
        item for item in manifest.jobs if item.property_id == reset_property_id
    )
    assert reset_epoch.executable
    harness = next(
        directory / item
        for item in reset_epoch.source_files
        if item.startswith("harness/")
    ).read_text(encoding="utf-8")
    assert "$past(zlang_formal_reset_active)" in harness
    assert "$past(rst)" not in harness
    assert "end else if (zlang_formal_reset_active) begin" in harness


def test_nested_fifo_properties_publish_once_with_path_scope_and_bindings(
    tmp_path: Path,
) -> None:
    compilation = _compile(FIFO_SOURCE, "FifoFormalTop")
    descendants = _descendant_properties(compilation)
    assert len(descendants) == 5
    assert all(
        item.property.generated_from == "fifo:queue" for item in descendants
    )

    _assert_descendants_are_published_once(compilation, tmp_path / "bundle")


def test_missing_descendant_observation_skips_only_affected_physical_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compile(SIBLING_SOURCE, "SiblingFormalTop")
    design = compilation.recursive_formal_design
    assert design is not None
    artifact = emit_formal_artifact(
        compilation.ir,
        design,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    child1_count = next(
        item
        for item in artifact.recursive_bindings
        if item.physical_instance_path == ("SiblingFormalTop", "child1")
        and item.local_semantic_id == "register:count"
    )
    partial = replace(
        artifact,
        formal_observations=tuple(
            item
            for item in artifact.formal_observations
            if item.semantic_binding_id != child1_count.semantic_binding_id
        ),
    )
    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        lambda *_args, **_kwargs: partial,
    )
    monkeypatch.setattr(
        "zlang.verification_publication._try_clash_formal_fallback",
        lambda *_args, **_kwargs: (None, "forced Clash outage"),
    )

    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, directory)
    payload = _payload(directory)
    descendants = _descendant_properties(compilation)
    expected = {item.concrete_property_id for item in descendants}
    jobs = tuple(item for item in manifest.jobs if item.property_id in expected)
    plans = {
        item["property_identity"]: item
        for item in payload["execution_plan"]["goals"]
        if item["property_identity"] in expected
    }
    by_path: dict[tuple[str, ...], list[object]] = {}
    for item in jobs:
        by_path.setdefault(item.physical_instance_path, []).append(item)

    assert {item.property_id for item in jobs} == expected
    assert set(by_path) == {
        ("SiblingFormalTop", "child0"),
        ("SiblingFormalTop", "child1"),
    }
    assert all(item.executable for item in by_path[("SiblingFormalTop", "child0")])
    assert all(not item.executable for item in by_path[("SiblingFormalTop", "child1")])
    for job in by_path[("SiblingFormalTop", "child1")]:
        assert "observation_unavailable" in (job.reason or "")
        assert plans[job.property_id]["route"] is None
        skip = plans[job.property_id]["skip_reason"]
        assert skip["code"] == "observation_unavailable"
        assert child1_count.semantic_binding_id in skip["related_ids"]


def test_root_owned_recursive_assumption_gets_one_deduplicated_feasibility_cover(
    tmp_path: Path,
) -> None:
    compilation = _compile(ASSUMED_CHILD_SOURCE, "AssumedChildTop")
    design = compilation.recursive_formal_design
    assert design is not None
    child_records = tuple(
        item
        for item in design.properties
        if item.physical_instance_path == ("AssumedChildTop", "child")
    )
    assumptions = tuple(
        item for item in child_records if item.property.kind.value == "assumption"
    )
    assertions = tuple(
        item for item in child_records if item.property.kind.value == "assertion"
    )
    assert assumptions and assertions

    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, directory)
    payload = _payload(directory)
    assertion_ids = {item.concrete_property_id for item in assertions}
    jobs = tuple(
        item for item in manifest.jobs if item.property_id in assertion_ids
    )
    plans = {
        item["property_identity"]: item
        for item in payload["execution_plan"]["goals"]
        if item["property_identity"] in assertion_ids
    }

    assert {item.property_id for item in jobs} == assertion_ids
    assert all(item.executable for item in jobs)
    assert len({item.scope_id for item in jobs}) == 1
    assert len({item.assumption_ids for item in jobs}) == 1
    scoped_assumption_ids = jobs[0].assumption_ids
    assert len(scoped_assumption_ids) == 1
    covers = tuple(
        item
        for item in manifest.jobs
        if item.kind == "cover"
        and item.physical_instance_path == ("AssumedChildTop", "child")
    )
    assert len(covers) == 1
    cover = covers[0]
    assert cover.executable
    assert cover.assumption_ids == scoped_assumption_ids
    assert {
        payload["vacuity_dependencies"][item]
        for item in assertion_ids
    } == {cover.property_id}

    binding_sets = {item["route"]: item for item in payload["binding_sets"]}
    for job in jobs:
        plan = plans[job.property_id]
        assert plan["route"] is not None
        assert plan["skip_reason"] is None
        bound = {
            item["semantic_signal_id"]
            for item in binding_sets[job.route]["bindings"]
        }
        assert "port:enable" in bound
        assert not any(
            item.endswith(":port:enable") for item in bound
        )
        harness = next(
            directory / item
            for item in job.source_files
            if item.startswith("harness/")
        ).read_text(encoding="utf-8")
        assert "assume (enable)" in harness

    cover_harness = next(
        directory / item
        for item in cover.source_files
        if item.startswith("harness/")
    ).read_text(encoding="utf-8")
    assert "cover (enable)" in cover_harness
    assert "assume (enable)" not in cover_harness


def test_internally_driven_ready_valid_requirement_is_not_a_harness_assumption(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        INTERNAL_READY_VALID_SOURCE,
        "InternalReadyValidTop",
    )
    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, directory)
    payload = _payload(directory)
    child_jobs = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path
        == ("InternalReadyValidTop", "consumer")
        and item.kind == "safety"
    )
    assert child_jobs
    assert all(item.executable for item in child_jobs)
    assert all(not item.assumption_ids for item in child_jobs)
    producer_guarantees = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path
        == ("InternalReadyValidTop", "producer")
        and item.kind == "safety"
    )
    assert producer_guarantees
    assert all(item.executable for item in producer_guarantees)
    assert all(not item.assumption_ids for item in producer_guarantees)
    assert not any(
        item.kind == "cover"
        and item.physical_instance_path
        == ("InternalReadyValidTop", "consumer")
        for item in manifest.jobs
    )
    plans = {
        item["property_identity"]: item
        for item in payload["execution_plan"]["goals"]
    }
    assert all(
        plans[item.property_id]["route"] is not None
        and plans[item.property_id]["skip_reason"] is None
        for item in child_jobs
    )
    for job in child_jobs:
        harness = next(
            directory / item
            for item in job.source_files
            if item.startswith("harness/")
        ).read_text(encoding="utf-8")
        # The internal sink requirement is an assertion dependency on the
        # root implementation, never an environment constraint.
        assert "assume (" not in harness
        assert harness.count("assert (") == 2
        assert "InternalReadyValidTop__formal dut" in harness


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_internal_ready_valid_upstream_stall_mutation_fails_dependent_job(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        INTERNAL_READY_VALID_SOURCE,
        "InternalReadyValidTop",
    )
    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, directory)
    job = next(
        item for item in manifest.jobs
        if item.physical_instance_path == ("InternalReadyValidTop", "consumer")
        and item.kind == "safety"
    )
    assert job.executable
    assert not job.assumption_ids
    implementation_path = next(
        directory / item
        for item in job.source_files
        if item.startswith("implementation/") and item.endswith(".sv")
    )
    harness_path = next(
        directory / item
        for item in job.source_files
        if item.startswith("harness/")
    )
    implementation = implementation_path.read_text(encoding="utf-8")
    harness = harness_path.read_text(encoding="utf-8")
    assert "assume (" not in harness
    assert harness.count("assert (") == 2

    passed = run_verilog_formal(
        implementation + "\n" + harness,
        top=job.top,
        property_id=job.property_id,
        depth=6,
        systemverilog=True,
        source_origin=job.source_origin,
        timeout_seconds=60,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    producer_start = implementation.index("module InternalProducer_s")
    producer_end = implementation.index("endmodule", producer_start)
    producer = implementation[producer_start:producer_end]
    valid_assignment = next(
        line for line in producer.splitlines()
        if line.strip().startswith("assign tx_valid = ")
    )
    mutated_producer = producer.replace(
        valid_assignment,
        "  assign tx_valid = ~(tx_ready);",
        1,
    )
    mutated = (
        implementation[:producer_start]
        + mutated_producer
        + implementation[producer_end:]
    )
    assert mutated != implementation
    failed = run_verilog_formal(
        mutated + "\n" + harness,
        top=job.top,
        property_id=job.property_id,
        depth=6,
        systemverilog=True,
        source_origin=job.source_origin,
        timeout_seconds=60,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.source_origin == job.source_origin
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace


def test_user_recursive_assumption_is_never_reinterpreted_as_internal_guarantee(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        INTERNAL_USER_ASSUMPTION_SOURCE,
        "InternalReadyValidTop",
    )
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    child_jobs = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path
        == ("InternalReadyValidTop", "consumer")
        and item.kind == "safety"
    )
    assert child_jobs
    assert all(not item.executable for item in child_jobs)
    assert all(
        "user-authored assumptions cannot be discharged" in (item.reason or "")
        for item in child_jobs
    )


def test_buffered_internal_ready_valid_ownership_remains_explicitly_incomplete(
    tmp_path: Path,
) -> None:
    compilation = _compile(
        BUFFERED_READY_VALID_SOURCE,
        "InternalReadyValidTop",
    )
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    child_jobs = tuple(
        item
        for item in manifest.jobs
        if item.physical_instance_path
        == ("InternalReadyValidTop", "consumer")
        and item.kind == "safety"
    )
    assert child_jobs
    assert all(not item.executable for item in child_jobs)
    assert all(
        "connection-owned buffering" in (item.reason or "")
        for item in child_jobs
    )
