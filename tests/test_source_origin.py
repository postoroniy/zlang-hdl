import json
from dataclasses import fields, is_dataclass
import hashlib

import pytest

from zlang.backend.manifest import (
    BackendArtifact,
    InstanceManifest,
    RECURSIVE_MANIFEST_VERSION,
    RecursiveBindingManifest,
)
from zlang.ir.equivalence import (
    BindingSide,
    EquivalenceBinding,
    SignalRole,
)
from zlang.ir import Call
from zlang.source import SourceOrigin, SourceSpan
from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact


DIGEST = "a" * 64


def _origin() -> SourceOrigin:
    return SourceOrigin(
        SourceSpan(3, 5, 3, 17),
        "output y",
        "examples/add.zhl",
        DIGEST,
    )


def test_source_origin_old_constructor_and_render_remain_compatible() -> None:
    origin = SourceOrigin(SourceSpan(1, 2, 1, 8), "add")
    qualified = SourceOrigin(
        SourceSpan(1, 2, 1, 8), "add", "examples/add.zhl", DIGEST
    )

    assert origin.source_unit is None
    assert origin.digest is None
    assert origin.render() == "1:2-1:8:add"
    assert sorted((qualified, origin)) == [origin, qualified]


def test_source_origin_structured_data_is_deterministic_and_lossless() -> None:
    origin = _origin()
    expected = {
        "construct": "output y",
        "digest": DIGEST,
        "source_unit": "examples/add.zhl",
        "span": {
            "start_line": 3,
            "start_column": 5,
            "end_line": 3,
            "end_column": 17,
        },
    }

    assert origin.to_data() == expected
    assert SourceOrigin.from_data(expected) == origin
    assert SourceOrigin.from_data(origin.render()) == SourceOrigin(
        origin.span, origin.construct
    )
    assert json.dumps(origin.to_data(), sort_keys=True) == json.dumps(
        SourceOrigin.from_data(origin.to_data()).to_data(), sort_keys=True
    )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"source_unit": ""}, "source unit"),
        ({"digest": "ABC"}, "SHA-256"),
        ({"digest": "f" * 63}, "SHA-256"),
    ],
)
def test_source_origin_rejects_invalid_identity_fields(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        SourceOrigin(SourceSpan(1, 1, 1, 1), "value", **kwargs)


def test_backend_artifact_round_trip_preserves_all_structured_origins() -> None:
    origin = _origin()
    artifact_hash = "b" * 64
    binding = EquivalenceBinding(
        RECURSIVE_MANIFEST_VERSION,
        BindingSide.IMPLEMENTATION,
        "port:y",
        "selected",
        "Top",
        "y",
        8,
        "unsigned",
        SignalRole.OUTPUT,
        "clk",
        "rst",
        "direct_systemverilog",
        artifact_hash,
        origin,
        canonical_type="uint<8>",
    )
    instance = InstanceManifest(
        "instance",
        ("Top", "child"),
        "child",
        None,
        "definition",
        "Child",
        "source",
        "c" * 64,
        "specialization",
        "clk",
        "rst",
        origin,
        "component",
    )
    recursive = RecursiveBindingManifest(
        "binding",
        "instance",
        "register:q",
        "specialization",
        ("Top", "child"),
        "state",
        "uint<8>",
        8,
        "unsigned",
        "internal",
        "clk",
        "rst",
        None,
        origin,
        None,
        (),
        None,
        "direct_systemverilog",
        artifact_hash,
    )
    artifact = BackendArtifact(
        "direct_systemverilog",
        "Top",
        "selected",
        artifact_hash,
        "module Top; endmodule\n",
        (binding,),
        RECURSIVE_MANIFEST_VERSION,
        instances=(instance,),
        recursive_bindings=(recursive,),
    )

    encoded = artifact.to_json()
    data = json.loads(encoded)
    assert data["bindings"][0]["source_origin"] == origin.to_data()
    assert data["instances"][0]["source_origin"] == origin.to_data()
    assert data["recursive_bindings"][0]["source_origin"] == origin.to_data()

    restored = BackendArtifact.from_json(encoded)
    assert restored.bindings[0].source_origin == origin
    assert restored.instances[0].source_origin == origin
    assert restored.recursive_bindings[0].source_origin == origin


def test_backend_artifact_reads_legacy_rendered_binding_origin() -> None:
    origin = _origin()
    artifact_hash = "b" * 64
    payload = {
        "manifest_version": 2,
        "backend": "clash",
        "module": "Top",
        "selected_ir_identity": "selected",
        "artifact_hash": artifact_hash,
        "bindings": [
            {
                "map_version": 2,
                "side": "implementation",
                "semantic_signal_id": "port:y",
                "selected_ir_identity": "selected",
                "rtl_module": "Top",
                "rtl_path": "y",
                "width": 8,
                "signedness": "unsigned",
                "role": "output",
                "clock_domain": None,
                "reset_domain": None,
                "backend": "clash",
                "artifact_hash": artifact_hash,
                "source_origin": origin.render(),
            }
        ],
    }

    restored = BackendArtifact.from_json(payload)
    assert restored.bindings[0].source_origin == SourceOrigin(
        origin.span, origin.construct
    )


def _origins_in(value: object) -> tuple[SourceOrigin, ...]:
    found: list[SourceOrigin] = []

    def visit(item: object) -> None:
        if is_dataclass(item):
            origin = getattr(item, "origin", None)
            if isinstance(origin, SourceOrigin):
                found.append(origin)
            for descriptor in fields(item):
                if descriptor.name != "origin":
                    visit(getattr(item, descriptor.name))
        elif isinstance(item, (tuple, list)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)


def test_compile_source_attaches_logical_unit_and_content_digest() -> None:
    source = "module Add { in a : u8 in b : u8 out y : u9 y = a + b }\n"
    result = compile_source(
        source,
        include_clash=False,
        source_unit="examples/add.zhl",
    )
    origin = result.ir.assignments[0].expression.origin

    assert origin is not None
    assert origin.source_unit == "examples/add.zhl"
    assert origin.digest == hashlib.sha256(source.encode()).hexdigest()


def test_source_unit_and_digest_do_not_change_hardware_or_artifact_identity() -> None:
    source = "module Add { in a : u8 in b : u8 out y : u9 y = a + b }\n"
    anonymous = compile_source(source, include_clash=False)
    located = compile_source(
        source,
        include_clash=False,
        source_unit="examples/add.zhl",
    )
    anonymous_artifact = emit_systemverilog_artifact(anonymous.ir)
    located_artifact = emit_systemverilog_artifact(located.ir)

    assert anonymous.ir == located.ir
    assert anonymous_artifact.text == located_artifact.text
    assert anonymous_artifact.artifact_hash == located_artifact.artifact_hash
    assert (
        anonymous_artifact.selected_ir_identity
        == located_artifact.selected_ir_identity
    )


def test_imported_generic_body_keeps_stdlib_unit_and_digest() -> None:
    source = """
    import std.math.complex
    module ComplexAddOrigins {
        in a : Complex<u8>
        in b : Complex<u8>
        out y : Complex<u9>
        y = a + b
    }
    """
    result = compile_source(
        source,
        include_clash=False,
        source_unit="examples/complex_add.zhl",
    )
    call = result.ir.assignments[0].expression
    assert isinstance(call, Call)
    assert call.origin is not None
    assert call.origin.source_unit == "examples/complex_add.zhl"
    definition = next(
        item
        for item in result.ir.callable_definitions
        if item.callee_identity == call.callee_identity
    )
    origins = _origins_in(definition.body)
    stdlib = tuple(item for item in origins if item.source_unit == "std.math.complex")

    assert stdlib
    assert all(item.digest is not None and len(item.digest) == 64 for item in stdlib)
    assert any(item.construct == "operator +" for item in stdlib)
