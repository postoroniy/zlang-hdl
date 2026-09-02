from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir.module import ModuleSignatureParameter, PortDirection
from zlang.module_resolver import IndexedModuleResolver, load_indexed_module
from zlang.semantic import SemanticError


def test_exact_scalar_interface_has_stable_nominal_and_applied_identity() -> None:
    result = compile_source(
        """
interface PassIfc { in a : u8 out y : u8 }
module Pass : PassIfc { in a : u8 out y : u8 y = a }
""",
        include_clash=False,
        source_unit="design.pass",
    )
    signature = result.ir.module_signature
    assert signature is not None
    assert signature.declaration_identity == "design.pass::interface::PassIfc"
    assert len(signature.nominal_identity) == 64
    assert len(signature.identity) == 64
    assert [item.name for item in signature.ports] == ["a", "y"]
    assert signature.ports[0].direction is PortDirection.INPUT


def test_type_and_value_parameter_application_survives_child_specialization() -> None:
    result = compile_source(
        """
interface PairIfc<type T, N=2> {
    in x : T
    out y : vec<N,T>
}
module Pair<type T, N=2> : PairIfc<T,N> {
    in x : T
    out y : vec<N,T>
    y = generate(i in 0..N) x
}
module Top {
    in x : u8
    out y : vec<3,u8>
    inst pair : Pair<T=u8,N=3> { x = x }
    y = pair.y
}
""",
        include_clash=False,
    )
    signature = result.ir.children[0].module_signature
    assert signature is not None
    assert signature.parameters == (
        ModuleSignatureParameter("T", "type", "u8", None),
        ModuleSignatureParameter("N", "value", 3, 2),
    )
    assert str(signature.ports[1].type) == "vec<3,u8>"


def test_aggregate_interface_compares_role_members_types_and_domains() -> None:
    result = compile_source(
        """
protocol TinyBus<type T> {
    role initiator
    role target
    member request : T initiator -> target
    member response : T target -> initiator
}
interface TargetIfc<type T> {
    clock clk
    reset rst
    interface bus : TinyBus<T>.target @clk
}
module Target<type T> : TargetIfc<T> {
    clock clk
    reset rst
    interface bus : TinyBus<T>.target @clk
    bus.response = bus.request
}
module Top {
    clock clk
    reset rst
    interface bus : TinyBus<u8>.target @clk
    inst target : Target<T=u8>
    connect bus -> target.bus
}
""",
        include_clash=False,
    )
    signature = result.ir.children[0].module_signature
    assert signature is not None
    assert signature.ports == ()  # Aggregate leaves are not double-counted.
    endpoint = signature.aggregate_protocol_endpoints[0]
    assert endpoint.role == "target"
    assert endpoint.domain == "clk"
    assert [str(member.payload_type) for member in endpoint.members] == ["u8", "u8"]
    assert endpoint.members[0].source_role == "initiator"


@pytest.mark.parametrize(
    ("interface_port", "module_port", "message"),
    (
        ("in a : u8", "out a : u8 a=0", "port 'a'"),
        ("out y : u8", "out y : u9 y=0", "port 'y'"),
        ("in a : u8", "in b : u8", "port set differs"),
    ),
)
def test_port_conformance_is_exact(
    interface_port: str, module_port: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        compile_source(
            f"""
interface ExactIfc {{ {interface_port} }}
module Exact : ExactIfc {{ {module_port} }}
""",
            include_clash=False,
        )


def test_parameter_kind_name_and_default_are_exact() -> None:
    with pytest.raises(SemanticError, match="parameter contract"):
        compile_source(
            """
interface ParamIfc<N=4> { in x : uint<N> }
module Bad<N=8> : ParamIfc<N> { in x : uint<N> }
""",
            include_clash=False,
        )


def test_equivalent_parameter_default_spelling_normalizes() -> None:
    result = compile_source(
        """
interface ParamIfc<N=2+2> { in x : uint<N> }
module Good<N=4> : ParamIfc<N> { in x : uint<N> }
""",
        include_clash=False,
    )
    signature = result.ir.module_signature
    assert signature is not None
    assert signature.parameters == (
        ModuleSignatureParameter("N", "value", 4, 4),
    )


def test_port_declaration_order_is_exact() -> None:
    with pytest.raises(SemanticError, match="port order"):
        compile_source(
            """
interface OrderedIfc { in a:u8 in b:u8 }
module Bad : OrderedIfc { in b:u8 in a:u8 }
""",
            include_clash=False,
        )


def test_applied_parameter_value_must_track_effective_specialization() -> None:
    with pytest.raises(SemanticError, match="applied parameter values"):
        compile_source(
            """
interface ParamIfc<N=4> { in x : uint<N> }
module Bad<N=4> : ParamIfc<4> { in x : uint<N> }
module Top { in x : u8 inst bad : Bad<N=8> { x=x } }
""",
            include_clash=False,
        )


def test_declared_default_participates_in_applied_signature_identity() -> None:
    def signature(default: int):
        result = compile_source(
            f"""
interface ParamIfc<N={default}> {{ in x:uint<N> }}
module Child<N={default}> : ParamIfc<N> {{ in x:uint<N> }}
module Top {{ in x:u16 inst child:Child<N=16> {{x=x}} }}
""",
            include_clash=False,
        )
        value = result.ir.children[0].module_signature
        assert value is not None
        return value

    left, right = signature(4), signature(8)
    assert left.parameters[0].value == right.parameters[0].value == 16
    assert left.parameters[0].declared_default == 4
    assert right.parameters[0].declared_default == 8
    assert left.identity != right.identity


def test_explicit_clock_reset_domain_is_not_satisfied_by_inheritance() -> None:
    with pytest.raises(SemanticError, match="clock/reset declarations"):
        compile_source(
            """
interface ClockedIfc { clock clk reset rst in x : u8 @clk out y : u8 @clk }
module Child : ClockedIfc { in x : u8 out y : u8 y=x }
module Top { clock clk reset rst in x:u8 out y:u8 inst c:Child {x=x} y=c.y }
""",
            include_clash=False,
        )


def test_timing_presence_and_value_are_exact() -> None:
    with pytest.raises(SemanticError, match="timing contract"):
        compile_source(
            """
interface TimedIfc {
    clock clk reset rst in x:u8 out y:u8
    timing { latency 2 ii 1 }
}
module Bad : TimedIfc {
    clock clk reset rst in x:u8 out y:u8
    y=delay<1>(x)
    timing { latency 1 ii 1 }
}
""",
            include_clash=False,
        )


def test_aggregate_role_mismatch_is_rejected() -> None:
    with pytest.raises(SemanticError, match="aggregate protocol endpoints"):
        compile_source(
            """
protocol P { role source role sink member data:u8 source -> sink member ready:bit sink -> source }
interface SinkIfc { interface p:P.sink }
module Bad : SinkIfc { interface p:P.source p.data=0 }
""",
            include_clash=False,
        )


def test_aggregate_specialization_type_mismatch_is_rejected() -> None:
    with pytest.raises(SemanticError, match="aggregate protocol endpoints"):
        compile_source(
            """
protocol P<type T> { role source role sink member data:T source -> sink member ready:bit sink -> source }
interface SinkIfc { interface p:P<u8>.sink }
module Bad : SinkIfc { interface p:P<u9>.sink p.ready=1 }
""",
            include_clash=False,
        )


def test_request_response_interface_member_fails_closed() -> None:
    with pytest.raises(SemanticError, match="do not yet accept request_response"):
        compile_source(
            """
interface RRIfc {
    clock clk reset rst
    interface mem : request_response<u8,u8> { max_outstanding 1 ordering in_order }
}
module Bad : RRIfc {
    clock clk reset rst
    interface mem : request_response<u8,u8> { max_outstanding 1 ordering in_order }
}
""",
            include_clash=False,
        )


def test_interface_ports_reject_inline_initializers() -> None:
    with pytest.raises(SemanticError, match="cannot have an initializer"):
        compile_source(
            """
interface BadIfc { out y:u8 = 0 }
module Bad : BadIfc { out y:u8 y=0 }
""",
            include_clash=False,
        )


def test_unused_interface_still_rejects_implementation_syntax() -> None:
    with pytest.raises(SemanticError, match="cannot have an initializer"):
        compile_source(
            "interface BadIfc { out y:u8 = 0 } module Top {}",
            include_clash=False,
        )


def test_unused_request_response_interface_still_fails_closed() -> None:
    with pytest.raises(SemanticError, match="do not yet accept request_response"):
        compile_source(
            """
interface RRIfc {
    interface mem : request_response<u8,u8> {
        max_outstanding 1
        ordering in_order
    }
}
module Top {}
""",
            include_clash=False,
        )


def test_imported_interface_keeps_logical_nominal_identity(tmp_path) -> None:
    source = tmp_path / "interfaces.zhl"
    source.write_text(
        "interface VendorIfc { in x:u8 out y:u8 } module Library {}",
        encoding="utf-8",
    )
    record = load_indexed_module(
        "vendor.interfaces",
        source_root=tmp_path,
        relative_path=source.relative_to(tmp_path),
        package_identity="vendor",
    )
    resolver = IndexedModuleResolver(
        (record,), package_namespaces=("vendor",), include_stdlib=False
    )
    result = compile_source(
        """
import vendor.interfaces
module Pass : VendorIfc { in x:u8 out y:u8 y=x }
""",
        include_clash=False,
        module_resolver=resolver,
        source_unit="app.top",
    )
    signature = result.ir.module_signature
    assert signature is not None
    assert (
        signature.declaration_identity
        == "vendor.interfaces::interface::VendorIfc"
    )
    assert signature.source_origin is not None
    assert signature.source_origin.source_unit == "vendor.interfaces"
    assert signature.source_origin.digest == record.digest


def test_imported_interface_name_conflict_is_rejected(tmp_path) -> None:
    records = []
    for logical in ("vendor.one", "vendor.two"):
        relative = logical.rsplit(".", 1)[1] + ".zhl"
        source = tmp_path / relative
        source.write_text(
            "interface SharedIfc { in x:u8 } module Library" + relative[0] + " {}",
            encoding="utf-8",
        )
        records.append(
            load_indexed_module(
                logical,
                source_root=tmp_path,
                relative_path=source.relative_to(tmp_path),
                package_identity="vendor",
            )
        )
    resolver = IndexedModuleResolver(
        records, package_namespaces=("vendor",), include_stdlib=False
    )
    with pytest.raises(SemanticError, match="conflicting module interface"):
        compile_source(
            "import vendor.one import vendor.two module Top {}",
            include_clash=False,
            module_resolver=resolver,
        )
