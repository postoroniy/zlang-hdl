from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conformance.catalog import LANGUAGE_TOUR_PATH
from zlang.compiler import compile_source
from zlang.parser import ParseError, parse
from zlang.public_capabilities import CAPABILITY_REGISTRY


ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "editors" / "vscode" / "zlang-vscode"
SOURCE_PATH = LANGUAGE_TOUR_PATH
ELASTIC_SOURCE_PATH = ROOT / "examples" / "elastic_pipeline_auto.zl"
RECOMMENDED_SETTINGS = EXT / "recommended-settings.json"

def test_extension_json_and_registration_are_valid() -> None:
    package = json.loads((EXT / "package.json").read_text())
    configuration = json.loads((EXT / "language-configuration.json").read_text())
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    surface = json.loads((EXT / "supported-surface.json").read_text())
    recommended_settings = json.loads(RECOMMENDED_SETTINGS.read_text())
    assert package["contributes"]["languages"][0]["extensions"] == [".zl", ".zlang"]
    assert package["contributes"]["grammars"][0]["path"].endswith("zlang.tmLanguage.json")
    assert configuration["comments"]["blockComment"] == ["/*", "*/"]
    assert ["<", ">"] not in configuration["brackets"]
    assert grammar["scopeName"] == "source.zlang"
    assert surface == CAPABILITY_REGISTRY.editor_surface()
    assert "editor.tokenColorCustomizations" in recommended_settings


def test_editor_surface_has_no_known_phantom_claims() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    surface = CAPABILITY_REGISTRY.editor_surface()
    tokens = {
        str(value)
        for key in ("keywords", "types", "intrinsics", "modes", "operators")
        for value in surface[key]
    }
    phantom = (
        "namespace", "function", "property", "case", "return",
        "let", "const", "var", "bool", "int", "Tuple", "Vec", "comb", "seq",
        "formal", "eventually", "ready_valid", "credit_based",
        "resize", "zero_extend", "sign_extend", "domain", ":=", "::", "%",
    )
    for word in phantom:
        assert word not in tokens, f"phantom editor token remains: {word}"
    intrinsic_patterns = json.dumps(grammar["repository"]["intrinsics"])
    generic_call_patterns = json.dumps(grammar["repository"]["generic-calls"])
    for word in ("resize", "zero_extend", "sign_extend"):
        assert word not in intrinsic_patterns
        assert word not in generic_call_patterns


def test_editor_surface_tokens_are_in_grammar_and_corpus() -> None:
    grammar_text = (EXT / "syntaxes" / "zlang.tmLanguage.json").read_text()
    surface = CAPABILITY_REGISTRY.editor_surface()
    corpus = SOURCE_PATH.read_text()
    for token in surface["keywords"]:
        assert token in grammar_text, f"surface keyword is not highlighted: {token}"
    for token in surface["modes"]:
        assert token in grammar_text, f"surface mode is not highlighted: {token}"
    for token in surface["intrinsics"]:
        assert token in grammar_text, f"surface intrinsic is not highlighted: {token}"
    type_needles = {
        "uN": "u[1-9]", "sN": "s[1-9]", "SF": "SF", "UF": "UF",
        "SF_Sat": "SF_Sat", "UF_Sat": "UF_Sat",
    }
    for token in surface["types"]:
        assert type_needles.get(token, token) in grammar_text, (
            f"surface type is not highlighted: {token}"
        )
    for token in (
        "if", "priority", "when", "quantize", "generate", "target", "resource",
        "concat", "zeros", "ones", "reshape", "bitcast", "parity", "pack", "unpack",
        "with", "repeat", "char", "string",
    ):
        assert token in corpus, f"conformance corpus misses required lexeme: {token}"
    assert "enum" in surface["keywords"]
    assert "\\b(enum|struct|type|protocol" in grammar_text
    assert "CorpusPair9 { left, right } = plain" in corpus
    assert "original with { right = left }" in corpus
    assert "literal_vector : vec<2,u8> = [a, b]" in corpus
    assert "repeated_vector : vec<2,u8> = repeat(a)" in corpus
    assert "explicit_repeat : vec<2,u8> = repeat<2>(b)" in corpus
    assert "fsm state = CorpusPhase.Idle" in corpus
    assert "clock clk reset rst" in corpus
    assert "async reset arst_n @clk" in corpus
    assert "module TextTupleSyntax" in corpus
    assert "out letter : char = 'A'" in corpus
    assert 'out tag : string<4> = "OFDM"' in corpus
    assert "value : (u8, bit) = (data, last)" in corpus
    assert "(payload, final) = value" in corpus
    for operator in ("<=>", "<-", "->", "=>", "?", ":"):
        assert operator in grammar_text
    assert "\\\\.\\\\." in grammar_text  # escaped `..` range operator


def test_editor_scopes_distinguish_neighboring_syntax_categories() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    repository = grammar["repository"]

    # Exact decimals must win before their integral prefix and generic type
    # arguments must retain their own number/type/punctuation scopes.
    number_patterns = repository["numbers"]["patterns"]
    assert number_patterns[0]["name"] == "constant.numeric.exact-decimal.zlang"
    assert "u[1-9][0-9]*" in repository["types"]["patterns"][-2]["match"]
    for name in (
        "meta.type.fixed.zlang",
        "meta.type.vector.zlang",
        "meta.type.storage.zlang",
        "meta.type.protocol.zlang",
        "meta.type.nominal-generic.zlang",
    ):
        pattern = next(item for item in repository["types"]["patterns"] if item.get("name") == name)
        assert pattern["beginCaptures"]["3"]["name"] == "punctuation.definition.type.begin.zlang"
        assert pattern["endCaptures"]["1"]["name"] == "punctuation.definition.type.end.zlang"

    declaration_scopes = json.dumps(repository["declarations"])
    detail_scopes = json.dumps(repository["declaration-details"])
    assert "entity.name.function.zlang" in declaration_scopes
    assert "entity.name.type.parameter.zlang" in declaration_scopes
    assert "variable.parameter.port.zlang" in detail_scopes
    assert repository["declaration-details"]["patterns"][0]["begin"].startswith("^\\s*")
    assert "meta.declaration.parameters.zlang" in json.dumps(repository["generic-parameters"])
    assert "variable.parameter.iteration.zlang" in json.dumps(repository["range-binders"])
    assert "meta.pattern-constant.zlang" in json.dumps(repository["pattern-constants"])
    packed_constants = json.dumps(repository["packed-constants"])
    assert "meta.packed-constant.zlang" in packed_constants
    assert "support.function.builtin.hardware.zlang" in packed_constants
    top_includes = [item["include"] for item in grammar["patterns"]]
    assert top_includes.index("#equiv-blocks") < top_includes.index(
        "#packed-constants"
    )
    equiv_patterns = repository["equiv-blocks"]["patterns"][0]["patterns"]
    assert equiv_patterns[0] == {"include": "#pattern-constants"}
    assert "zero|ones" in repository["pattern-constants"]["patterns"][0]["begin"]
    assert "variable.other.definition.csr.zlang" in json.dumps(repository["csr-layout"])
    assert "meta.resource.declaration.zlang" in json.dumps(repository["resource-declarations"])
    assert "entity.name.label.rule.zlang" in json.dumps(repository["labels"])
    assert "meta.enum.declaration.zlang" in json.dumps(repository["enum-declarations"])
    assert "constant.other.enum.member.zlang" in json.dumps(repository["enum-members"])
    union_declarations = json.dumps(repository["union-declarations"])
    union_arms = json.dumps(repository["union-match-arms"])
    assert "meta.union.declaration.zlang" in union_declarations
    assert "constant.other.union.variant.zlang" in union_declarations
    assert "variable.other.definition.union-field.zlang" in union_declarations
    assert "variable.parameter.pattern.union.zlang" in union_arms
    assert "meta.bit-slice.zlang" in json.dumps(repository["bit-slices"])
    assert "punctuation.separator.slice.zlang" in json.dumps(repository["bit-slices"])
    assert "variable.other.member.zlang" in json.dumps(repository["members"])
    assert "entity.name.function.call.zlang" in json.dumps(repository["calls"])
    generic_parameter_body = json.dumps(repository["generic-parameter-body"])
    static_function_references = json.dumps(repository["static-function-references"])
    assert "storage.type.function.zlang" in generic_parameter_body
    assert "storage.type.function.reference.zlang" in static_function_references
    assert "entity.name.function.reference.zlang" in static_function_references
    assert all(
        any(
            pattern.get("include") == "#static-function-references"
            for pattern in generic_call["patterns"]
        )
        for generic_call in repository["generic-calls"]["patterns"]
    )
    assert "variable.other.readwrite.zlang" in json.dumps(repository["identifiers"])
    storage_types = json.dumps(repository["types"])
    storage_directives = json.dumps(repository["keywords"])
    declaration_details = json.dumps(repository["declaration-details"])
    assert "fifo|mem|rom" in storage_types
    assert "read_latency|init|collision" in storage_directives
    assert "fifo|memory|rom|interface" in declaration_details
    assert "with" in json.dumps(repository["keywords"])
    assert "repeat" in json.dumps(repository["intrinsics"])
    top_includes = [item["include"] for item in grammar["patterns"]]
    assert top_includes.index("#strings") < top_includes.index("#comments")
    string_scopes = json.dumps(repository["strings"])
    assert "string.quoted.double.zlang" in string_scopes
    assert "constant.character.zlang" in string_scopes
    assert "constant.character.escape.zlang" in string_scopes
    assert "invalid.illegal.escape.zlang" in string_scopes
    assert "invalid.illegal.empty-string.zlang" in string_scopes
    assert "invalid.illegal.character.zlang" in string_scopes
    assert "invalid.illegal.raw-character.zlang" in string_scopes
    assert "meta.type.string.zlang" in json.dumps(repository["types"])
    assert "char" in json.dumps(repository["types"])
    assert "keyword.other.directive.clock-reset.zlang" in json.dumps(
        repository["keywords"]
    )
    assert "constant.language.clock-reset.zlang" in json.dumps(
        repository["modes"]
    )
    generic_calls = repository["generic-calls"]["patterns"]
    hardware_generics = next(
        item for item in generic_calls
        if item.get("beginCaptures", {}).get("1", {}).get("name")
        == "support.function.builtin.hardware.zlang"
    )
    assert "repeat|delay|pipeline" in hardware_generics["begin"]


def test_character_string_and_tuple_editor_surface_is_executable() -> None:
    configuration = json.loads((EXT / "language-configuration.json").read_text())
    surface = CAPABILITY_REGISTRY.editor_surface()
    source = SOURCE_PATH.read_text()

    assert {"char", "string"} <= set(surface["types"])
    assert {pair["open"] for pair in configuration["autoClosingPairs"]} >= {"'", '"'}
    result = compile_source(
        source,
        top="TextTupleSyntax",
        include_clash=False,
        source_unit=str(SOURCE_PATH),
    )
    assert result.ir.name == "TextTupleSyntax"
    assert tuple(port.name for port in result.ir.ports) == (
        "data", "last", "letter", "tag", "pair", "restored", "selected",
        "packed", "same",
    )


def test_character_editor_pattern_accepts_only_one_exact_source_byte() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    patterns = grammar["repository"]["strings"]["patterns"]
    character = next(
        item for item in patterns if item["name"] == "constant.character.zlang"
    )
    valid = ("'A'", "'~'", r"'\0'", r"'\''", r"'\"'", r"'\xff'")
    invalid = ("''", "'AB'", r"'\q'", r"'\x0'", "'é'", "'\t'")
    for literal in valid:
        assert re.fullmatch(character["match"], literal), literal
    for literal in invalid:
        assert not re.fullmatch(character["match"], literal), literal

    empty_string = next(
        item for item in patterns
        if item["name"] == "invalid.illegal.empty-string.zlang"
    )
    assert re.fullmatch(empty_string["match"], '""')
    raw_invalid = next(
        item for item in patterns[1]["patterns"]
        if item["name"] == "invalid.illegal.raw-character.zlang"
    )
    for value in ("\t", "\n", "\x7f", "é"):
        assert re.fullmatch(raw_invalid["match"], value)


def test_elastic_transform_keyword_is_highlighted_and_executable() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    source = ELASTIC_SOURCE_PATH.read_text()
    assert "transform" in CAPABILITY_REGISTRY.keywords
    assert "transform" in json.dumps(grammar["repository"]["keywords"])
    assert "transform pipeline(auto" in source
    assert compile_source(
        source,
        top="ElasticPipelineAuto",
        include_clash=False,
        source_unit=str(ELASTIC_SOURCE_PATH),
    ).ir.name == "ElasticPipelineAuto"


def test_first_class_verification_words_are_contextually_highlighted() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    repository = grammar["repository"]
    source_path = EXT / "examples" / "verification.zl"
    source = source_path.read_text()
    result = compile_source(
        source,
        top="VerificationUxSyntax",
        include_clash=False,
        source_unit=str(source_path),
    )

    assert {"assert", "cover", "contract", "require", "ensure"} <= set(
        CAPABILITY_REGISTRY.keywords
    )
    declarations = json.dumps(repository["verification-declarations"])
    for word in ("assert", "cover", "contract", "require", "ensure"):
        assert word in declarations
    # Contextual declaration words do not appear in the catch-all keyword
    # expressions, so the second module's same-spelled ports remain variables.
    catch_all = tuple(
        pattern["match"] for pattern in repository["keywords"]["patterns"]
    )
    for word in ("assert", "cover", "contract", "require", "ensure"):
        assert all(re.fullmatch(pattern, word) is None for pattern in catch_all)
    assert result.ir.name == "VerificationUxSyntax"
    assert [scope.name for scope in result.ir.verification_scopes] == [
        "$module", "public_behavior"
    ]
    identifiers = compile_source(
        source,
        top="VerificationWordsRemainIdentifiers",
        include_clash=False,
        source_unit=str(source_path),
    )
    assert tuple(port.name for port in identifiers.ir.ports[:5]) == (
        "assert", "cover", "contract", "require", "ensure"
    )


def test_screenshot_surface_uses_specific_scopes_not_one_catch_all() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    repository_text = json.dumps(grammar["repository"])
    for token in (
        "round", "overflow", "provides", "require_resource", "read_latency",
        "collision", "floor_log2", "is_power_of_two", "fixed_raw", "quantize",
        "max_outstanding", "match_by", "crossing", "assume", "guarantee",
        "async", "edge", "mode", "polarity", "power_up", "falling", "asynchronous",
        "active_low", "unspecified", "union", "match",
    ):
        assert token in repository_text, f"screenshot token has no editor scope: {token}"
    for scope in (
        "storage.type.module.zlang",
        "entity.name.type.module.zlang",
        "support.type.scalar.zlang",
        "support.function.builtin.hardware.zlang",
        "support.function.builtin.conversion.zlang",
        "support.function.builtin.compile-time.zlang",
        "constant.language.quantization.zlang",
        "variable.language.metric.zlang",
        "keyword.operator.shift.zlang",
        "keyword.operator.connection.zlang",
        "keyword.operator.choice.zlang",
        "keyword.operator.annotation.zlang",
        "keyword.operator.range.zlang",
    ):
        assert scope in repository_text


def test_function_styles_are_three_scoped_theme_preserving_groups() -> None:
    grammar = json.loads((EXT / "syntaxes" / "zlang.tmLanguage.json").read_text())
    settings = json.loads(RECOMMENDED_SETTINGS.read_text())
    grammar_text = json.dumps(grammar)
    rules = settings["editor.tokenColorCustomizations"]["textMateRules"]
    styles = {
        rule["name"]: (tuple(rule["scope"]), rule["settings"])
        for rule in rules
        if rule["name"].startswith("ZLang ")
    }
    assert set(styles) == {
        "ZLang user functions",
        "ZLang hardware and conversion built-ins",
        "ZLang compile-time functions and guards",
        "ZLang operators",
        "ZLang signal and state symbols",
    }
    assert styles["ZLang user functions"][1] == {"fontStyle": ""}
    assert styles["ZLang hardware and conversion built-ins"][1] == {"fontStyle": "bold"}
    assert styles["ZLang compile-time functions and guards"][1] == {"fontStyle": "italic"}
    assert styles["ZLang operators"][1] == {
        "fontStyle": "bold",
        "foreground": "#F44747",
    }
    assert styles["ZLang signal and state symbols"][0] == (
        "source.zlang variable.parameter.port.zlang",
        "source.zlang variable.other.definition.hardware.zlang",
        "source.zlang variable.other.definition.zlang",
        "source.zlang variable.other.readwrite.zlang",
    )
    assert styles["ZLang signal and state symbols"][1] == {
        "fontStyle": "",
        "foreground": "#E5C07B",
    }
    for name in (
        "ZLang user functions",
        "ZLang hardware and conversion built-ins",
        "ZLang compile-time functions and guards",
    ):
        assert "foreground" not in styles[name][1]
    for scopes, _ in styles.values():
        for scope in scopes:
            lexical_scope = scope.removeprefix("source.zlang ")
            assert lexical_scope in grammar_text
    grammar_operator_scopes = {
        pattern["name"]
        for pattern in grammar["repository"]["operators"]["patterns"]
        if pattern["name"].startswith("keyword.operator.")
    }
    styled_operator_scopes = {
        scope.removeprefix("source.zlang ")
        for scope in styles["ZLang operators"][0]
    }
    assert styled_operator_scopes == grammar_operator_scopes


def test_all_syntax_clash_mangles_prelude_colliding_port_names() -> None:
    source = SOURCE_PATH.read_text()
    credit = compile_source(source, top="CreditSyntax").clash
    arbitration = compile_source(source, top="ArbitrationSyntax").clash
    assert "circuit data_zlang send" in credit
    assert "circuit high_zlang low_zlang" in arbitration
    # External HDL port annotations remain the original ZLang API names.
    assert 'PortName "data"' in credit
    assert 'PortProduct "high"' in arbitration
    assert 'PortProduct "low"' in arbitration


def test_binary_literals_share_typed_value_and_unterminated_comment_is_diagnostic() -> None:
    decimal = compile_source("module M { out y:u8 y=172 }").ir
    binary = compile_source("module M { out y:u8 y=0b1010_1100 }").ir
    hexadecimal = compile_source("module M { out y:u8 y=0xAC }").ir
    assert decimal == binary == hexadecimal
    with pytest.raises(ParseError, match="syntax error"):
        parse("/* unterminated\nmodule M { out y:u8 y=0 }")
