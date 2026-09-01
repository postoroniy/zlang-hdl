from __future__ import annotations

import json
from pathlib import Path

from zlang.public_capabilities import CAPABILITY_REGISTRY


ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "editors" / "vscode" / "zlang-vscode"


def test_named_module_interface_is_registry_owned_and_documented() -> None:
    assert CAPABILITY_REGISTRY.schema_version == 21
    assert "interface" in CAPABILITY_REGISTRY.keywords
    requirement = next(
        item
        for item in CAPABILITY_REGISTRY.documentation
        if item.capability == "named-module-interfaces"
    )
    assert requirement.document == "docs/named-module-interfaces.md"
    text = (ROOT / requirement.document).read_text()
    for marker in requirement.markers:
        assert marker in text
    assert "request_response" in text
    assert "does not support" in text


def test_editor_distinguishes_named_interfaces_from_aggregate_ports() -> None:
    grammar = json.loads(
        (EXT / "syntaxes" / "zlang.tmLanguage.json").read_text()
    )
    declarations = grammar["repository"]["declarations"]["patterns"]
    named = next(
        item
        for item in declarations
        if item.get("captures", {}).get("2", {}).get("name")
        == "entity.name.type.interface.zlang"
    )
    assert named["match"].startswith("^\\s*(interface)")
    assert "(?=\\s*(?:<|\\{))" in named["match"]

    generic_parameters = grammar["repository"]["generic-parameters"]["patterns"]
    generic = next(
        item
        for item in generic_parameters
        if item.get("begin", "").startswith("\\b(interface)")
    )
    assert generic["beginCaptures"]["2"]["name"] == (
        "entity.name.type.interface.zlang"
    )

    details = json.dumps(grammar["repository"]["declaration-details"])
    assert "variable.other.definition.hardware.zlang" in details
    assert "clock|reset|reg|fifo|memory|rom|interface|csr" in details


def test_public_guide_links_named_interface_contract() -> None:
    index = (ROOT / "docs" / "language-guide.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "[Named module interfaces](named-module-interfaces.md)" in index
    assert "docs/named-module-interfaces.md" in readme
