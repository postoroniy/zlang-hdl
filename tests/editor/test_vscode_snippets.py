"""Compile real snippet defaults in their documented insertion scopes.

This tests the shipped declarative snippets, not a completion engine or proof
runner. No extra .zhl fixture is added to the repository's source census.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from zlang.compiler import compile_source
from zlang.ir.expressions import Pipeline
from zlang.simulate import simulate, simulate_cycles


EXT = Path(__file__).resolve().parents[2] / "editors" / "vscode" / "zlang-hdl"
SNIPPETS = json.loads((EXT / "snippets" / "zlang-hdl.json").read_text())
BY_PREFIX = {snippet["prefix"]: snippet for snippet in SNIPPETS.values()}

# Deliberately support only the small VS Code subset used by this package:
# numbered defaults, linked tab stops and one final $0. Unsupported variables,
# transforms, nested placeholders, or unexpanded dollars must fail this gate.
TAB_STOP = re.compile(r"\$(?:(0|[1-9][0-9]*)|\{([1-9][0-9]*):([^${}\n]+)\})")


def _expand(prefix: str, replacements: dict[int, str] | None = None) -> str:
    body = "\n".join(BY_PREFIX[prefix]["body"])
    values: dict[int, str] = {}
    final_stops = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal final_stops
        index = int(match[1] or match[2])
        if index == 0:
            final_stops += 1
            return ""
        if match[2] is not None:
            assert index == len(values) + 1, "defaults must be unique and ordered"
            values[index] = (replacements or {}).get(index, match[3])
        assert index in values, "a linked tab stop must follow its default"
        return values[index]

    expanded = TAB_STOP.sub(substitute, body)
    assert final_stops == 1
    assert values
    assert "$" not in expanded, "unsupported or unexpanded snippet syntax"
    assert set(replacements or {}) <= values.keys()
    return expanded


# These are insertion contexts, not alternative hard-coded snippet bodies.
# Each expression/type in the context documents what the shipped default needs.
CONTEXTS = {
    "zmodule": ("", "", "ModuleName", ("u8",)),
    "zfn": (
        "",
        "\nmodule SnippetTop { in a, b : u8 out y : u9 = add(a, b) }",
        "SnippetTop",
        ("u9",),
    ),
    "zreg": (
        "module SnippetTop { clock clk reset rst\n",
        "\nout y : u8 = count }",
        "SnippetTop",
        ("u8",),
    ),
    "zwhen": (
        "module SnippetTop { clock clk reset rst "
        "in enable : bit reg count : u8 = 0\n",
        "\nout y : u8 = count }",
        "SnippetTop",
        ("u8",),
    ),
    "zpipeline": (
        "module SnippetTop { clock clk reset rst "
        "in a, b : u8 out y : u9 =\n",
        "\n}",
        "SnippetTop",
        ("u9",),
    ),
    "zassert": (
        "module SnippetTop { clock clk reset rst in ok : bit out y : bit = ok\n",
        "\n}",
        "SnippetTop",
        ("bit",),
    ),
    "zcontract": (
        "module SnippetTop { clock clk reset rst "
        "in legal : bit out ok : bit = legal\n",
        "\n}",
        "SnippetTop",
        ("bit",),
    ),
    "zrv": ("module SnippetTop {\n", "\n}", "SnippetTop", ("u8",)),
}


def _compile(prefix: str):
    before, after, top, _ = CONTEXTS[prefix]
    return compile_source(before + _expand(prefix) + after, top=top)


def test_snippet_inventory_and_registration_are_bounded() -> None:
    package = json.loads((EXT / "package.json").read_text())
    assert package["contributes"]["snippets"] == [
        {"language": "zlang-hdl", "path": "./snippets/zlang-hdl.json"}
    ]
    assert len(SNIPPETS) == len(BY_PREFIX) == len(CONTEXTS) == 8
    assert BY_PREFIX.keys() == CONTEXTS.keys()
    for name, snippet in SNIPPETS.items():
        assert name and set(snippet) == {"prefix", "description", "body"}
        assert re.fullmatch(r"z[a-z]+", snippet["prefix"])
        assert isinstance(snippet["description"], str) and snippet["description"]
        assert isinstance(snippet["body"], list) and snippet["body"]
        assert all(isinstance(line, str) for line in snippet["body"])
        assert not re.search(r"\b(let|const|return)\b", _expand(snippet["prefix"]))


@pytest.mark.parametrize("prefix", CONTEXTS)
def test_default_expansion_compiles_in_documented_scope(prefix: str) -> None:
    result = _compile(prefix)
    _, _, top, output_types = CONTEXTS[prefix]
    assert result.ir.name == top
    assert tuple(str(port.type) for port in result.ir.outputs) == output_types
    assert "topEntity" in result.clash


def test_linked_tab_stops_follow_user_edits() -> None:
    module = _expand("zmodule", {4: "data", 5: "u16", 6: "result"})
    assert "out result : u16 = data" in module
    assert str(compile_source(module).ir.outputs[0].type) == "u16"

    function = _expand("zfn", {1: "sum8", 2: "left", 4: "right"})
    assert "left + right" in function
    result = compile_source(
        function + "\nmodule T { in x : u8 out y : u9 = sum8(x, x) }"
    )
    assert simulate(result.ir, x=255) == {"y": 510}

    ready_valid = _expand("zrv", {1: "source", 2: "u16", 3: "sink"})
    assert "out sink : rv<u16>" in ready_valid
    assert "connect source -> sink" in ready_valid
    assert compile_source("module T {\n" + ready_valid + "\n}").ir.name == "T"


def test_defaults_preserve_carry_state_and_fixed_pipeline_semantics() -> None:
    function = _compile("zfn").ir
    assert str(function.functions[0].return_type) == "u9"
    assert simulate(function, a=255, b=255) == {"y": 510}

    register = _compile("zreg").ir
    assert simulate_cycles(register, [{}, {}]) == [{"y": 0}, {"y": 0}]

    rule = _compile("zwhen").ir
    assert tuple(item.name for item in rule.rules) == ("step",)
    cycles = [{"enable": 1}, {"enable": 0}, {"enable": 1}, {"enable": 0}]
    assert [item["y"] for item in simulate_cycles(rule, cycles)] == [0, 1, 1, 2]

    pipeline = _compile("zpipeline").ir
    expression = pipeline.assignments[0].expression
    assert isinstance(expression, Pipeline) and expression.stages == 2
    outputs = simulate_cycles(pipeline, [{"a": 255, "b": 255}] * 3)
    assert [item["y"] for item in outputs] == [0, 0, 510]


def test_ready_valid_default_has_no_added_storage_or_latency() -> None:
    module = _compile("zrv").ir
    for valid in (0, 1):
        for ready in (0, 1):
            outputs = simulate(
                module,
                rx={"payload": 165, "valid": valid},
                tx={"ready": ready},
            )
            assert outputs == {
                "rx": {"ready": ready, "transfer": valid & ready},
                "tx": {"payload": 165, "valid": valid, "transfer": valid & ready},
            }
