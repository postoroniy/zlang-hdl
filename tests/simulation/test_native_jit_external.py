"""Typed external models disappear before the primitive runtime boundary."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.parser.test_external_modules import SOURCE
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.external import ExternalModuleContract
from zlang.sim import compile as compile_simulation
from zlang.simulate import simulate
from zlang.simulation_external import (
    ExternalModelSimulationLoweringError,
    lower_external_model,
)
from zlang.simulation_lowering import PRIMITIVE_OPS


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "external_model.zhl"
    path.write_text(SOURCE, encoding="utf-8")
    return path


@pytest.mark.parametrize("top", ("VendorAdd", "Top"))
def test_external_model_has_reference_native_and_semantic_parity(
    tmp_path: Path,
    top: str,
) -> None:
    path = _source(tmp_path)
    semantic = compile_source(SOURCE, top=top).ir
    vectors = ((0, 0), (1, 2), (127, 129), (255, 1), (255, 255))
    expected = tuple(simulate(semantic, a=a, b=b)["y"] for a, b in vectors)
    observed = {}
    identities = set()
    for engine in ("reference", "native"):
        program = compile_simulation(path, top=top, engine=engine)
        identities.add(program.plan.identity)
        values = []
        with program.create() as instance:
            for a, b in vectors:
                instance.set("a", a)
                instance.set("b", b)
                instance.eval()
                values.append(instance.get("y"))
        observed[engine] = tuple(values)
    assert len(identities) == 1
    assert observed == {"reference": expected, "native": expected}


def test_external_model_plan_is_only_the_existing_primitive_machine(
    tmp_path: Path,
) -> None:
    program = compile_simulation(_source(tmp_path), top="Top", engine="reference")
    operations = {node["op"] for node in program.plan.payload["nodes"]}
    assert operations <= PRIMITIVE_OPS
    assert not operations & {"call", "parameter", "external", "model"}
    assert program.plan.payload["memories"] == []
    assert program.plan.payload["registers"] == []
    assert program.plan.payload["edge_programs"] == []
    assert program.plan.to_bytes() == compile_simulation(
        _source(tmp_path), top="Top", engine="reference"
    ).plan.to_bytes()


def test_external_model_contract_is_erased_only_after_exact_validation() -> None:
    external = compile_source(SOURCE).ir.children[0]
    lowered = lower_external_model(external)
    assert lowered.external_contract is None
    assert lowered.functions == ()
    assert lowered.callable_definitions == ()
    assert not isinstance(lowered.assignments[0].expression, expr.Call)

    contract = external.external_contract
    assert contract is not None
    wrong_contract = ExternalModuleContract(
        contract.logical_name,
        contract.signature,
        "missing-model-identity",
    )
    with pytest.raises(
        ExternalModelSimulationLoweringError,
        match="exactly one declared model",
    ):
        lower_external_model(replace(external, external_contract=wrong_contract))

    assignment = external.assignments[0]
    call = assignment.expression
    assert isinstance(call, expr.Call)
    malformed = replace(
        external,
        assignments=(
            replace(
                assignment,
                expression=replace(call, arguments=tuple(reversed(call.arguments))),
            ),
        ),
    )
    with pytest.raises(
        ExternalModelSimulationLoweringError,
        match="does not match its inputs",
    ):
        lower_external_model(malformed)


def test_external_model_expansion_is_bounded() -> None:
    external = compile_source(SOURCE).ir.children[0]
    with pytest.raises(
        ExternalModelSimulationLoweringError,
        match="exceeds 1 expression nodes",
    ):
        lower_external_model(external, max_nodes=1)


def test_external_model_expands_a_nested_typed_callable_closure(
    tmp_path: Path,
) -> None:
    source = SOURCE.replace(
        "fn add_model(a : u8, b : u8) -> u9 { a + b }",
        """fn add_core(a : u8, b : u8) -> u9 { a + b }
fn add_model(a : u8, b : u8) -> u9 { add_core(a, b) }""",
    )
    path = tmp_path / "nested_external.zhl"
    path.write_text(source, encoding="utf-8")
    for engine in ("reference", "native"):
        with compile_simulation(path, top="Top", engine=engine).create() as instance:
            instance.set("a", 201)
            instance.set("b", 99)
            instance.eval()
            assert instance.get("y") == 300


def test_native_runtime_has_no_external_module_vocabulary() -> None:
    root = Path(__file__).resolve().parents[2] / "native-runtime" / "src"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.rs"))
    for token in (
        "ExternalModuleContract",
        "model_callee_identity",
        "VendorAdd",
        "external_model",
    ):
        assert token not in source
