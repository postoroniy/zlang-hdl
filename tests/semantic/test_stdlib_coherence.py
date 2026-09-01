from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir import expressions as ir_expr
from zlang.ir.types import FixedType
from zlang.semantic import SemanticError
from zlang.simulate import simulate


WITNESSES = {
    "complex": """
        import std.math.complex
        module Top {
            in a : Complex<fixed<8,4>>
            in b : Complex<fixed<8,4>>
            out y : Complex<fixed<17,8>>
            y = a * b
        }
    """,
    "complex_profile": """
        import std.math.complex_fixed_18_16
        module Top {
            in a : Complex<fixed<18,16>>
            in b : Complex<fixed<18,16>>
            out y : Complex<fixed<18,16>>
            inst mul : ComplexMul18_16
            mul.a = a
            mul.b = b
            y = mul.result
        }
    """,
    "fixed_helpers": """
        import std.math.fixed
        module Top {
            in a : fixed<8,4>
            in b : fixed<8,4>
            in wide : fixed<12,8>
            in accumulator : fixed<16,8>
            out absolute : fixed<9,4>
            out mac : fixed<17,8>
            out minimum : fixed<8,4>
            out maximum : fixed<8,4>
            out limited : fixed<8,4>
            out floor_wrapped : fixed<8,4>
            out zero_wrapped : fixed<8,4>
            out away_saturated : fixed_sat<8,4>
            out quantized : fixed_sat<8,4>
            absolute = fixed_abs(a)
            mac = fixed_mac(a, b, accumulator)
            minimum = fixed_min(a, b)
            maximum = fixed_max(a, b)
            limited = fixed_clamp(a, b, a)
            floor_wrapped = fixed_quantize_floor_wrap<fixed<8,4>>(wide)
            zero_wrapped = fixed_quantize_toward_zero_wrap<fixed<8,4>>(wide)
            away_saturated =
                fixed_quantize_away_zero_saturate<fixed_sat<8,4>>(wide)
            quantized =
                fixed_quantize_nearest_even_saturate<fixed_sat<8,4>>(wide)
        }
    """,
    "stream_core": """
        import std.stream.core
        module Top {
            clock clk
            reset rst
            in input : rv<FrameBeat<u8,u2>>
            out output : rv<FrameBeat<u8,u2>>
            inst queue : RvFifo<T=FrameBeat<u8,u2>,D=4>
            connect input -> queue.input
            connect queue.output -> output
        }
    """,
    "stream_register_slice": """
        import std.stream.core
        module Top {
            clock clk
            reset rst
            in input : rv<u8>
            out output : rv<u8>
            inst slice : RvRegisterSlice<T=u8>
            connect input -> slice.input
            connect slice.output -> output
        }
    """,
    "stream_skid_buffer": """
        import std.stream.core
        module Top {
            clock clk
            reset rst
            in input : rv<u8>
            out output : rv<u8>
            inst skid : RvSkidBuffer<T=u8>
            connect input -> skid.input
            connect skid.output -> output
        }
    """,
    "serializer": """
        import std.stream.serialization
        module Top {
            clock clk
            reset rst
            in input : rv<vec<8,u8>>
            out output : rv<u8>
            inst serializer : RvVectorSerializer<T=u8,N=8>
            connect input -> serializer.input
            connect serializer.output -> output
        }
    """,
    "collector": """
        import std.stream.serialization
        module Top {
            clock clk
            reset rst
            in input : rv<bit>
            out output : rv<bits<8>>
            inst collector : RvBitCollector<N=8>
            connect input -> collector.input
            connect collector.output -> output
        }
    """,
    "complex_stream_profile": """
        import std.stream.complex_fixed
        module Top {
            clock clk
            reset rst
            in input : rv<Complex<fixed<37,32>>>
            out output : rv<Complex<fixed<18,16>>>
            inst quantizer : ComplexQuantizeStream37_32To18_16
            connect input -> quantizer.input
            connect quantizer.output -> output
        }
    """,
    "fft": """
        import std.dsp.fft
        module Top {
            in a : bits<8>
            out y : bits<8>
            y = fft_bit_reverse<N=8>(a)
        }
    """,
    "fft_twiddle": """
        import std.dsp.fft
        module Top {
            out y : vec<4,Complex<fixed<16,14>>>
            out z : vec<4,Complex<fixed<16,14>>>
            y = fft_forward_twiddles<T=fixed<16,14>,N=8>()
            z = fft_inverse_twiddles<T=fixed<16,14>,N=8>()
        }
    """,
    "fft_butterfly": """
        import std.dsp.fft
        module Top {
            in a : Complex<fixed<9,4>>
            in b : Complex<fixed<4,2>>
            in w : Complex<fixed<4,2>>
            out y : FFTButterfly<Complex<fixed<10,4>>,Complex<fixed<10,4>>>
            y = fft_butterfly_exact(a, b, w)
        }
    """,
    "storage": """
        import std.storage
        module Top {
            clock clk
            reset rst
            in input : u8
            out output : u8
            inst pipe : StorageDelay2<T=u8>
            pipe.input = input
            output = pipe.output
        }
    """,
    "storage_delay_one": """
        import std.storage
        module Top {
            clock clk
            reset rst
            in input : u8
            out output : u8
            inst pipe : StorageDelay1<T=u8>
            pipe.input = input
            output = pipe.output
        }
    """,
    "storage_queue": """
        import std.storage
        module Top {
            clock clk
            reset rst
            in push : bit
            in pop : bit
            in data : u8
            out front : u8
            out valid : bit
            out ready : bit
            out count : u3
            inst queue : StorageQueue<T=u8,D=4,CW=3>
            queue.push = push
            queue.pop = pop
            queue.data = data
            front = queue.front
            valid = queue.valid
            ready = queue.ready
            count = queue.count
        }
    """,
    "storage_reorder": """
        import std.storage
        module Top {
            clock clk
            reset rst
            in write : bit
            in write_index : u3
            in write_data : bits<8>
            in read_index : u3
            out read_data : bits<8>
            inst storage : StorageReorderBits<W=8,N=8>
            storage.write = write
            storage.write_index = write_index
            storage.write_data = write_data
            storage.read_index = read_index
            read_data = storage.read_data
        }
    """,
    "storage_ping_pong": """
        import std.storage
        module Top {
            clock clk
            reset rst
            in write : bit
            in write_index : u2
            in write_data : bits<8>
            in commit : bit
            in retire : bit
            in read_index : u2
            out write_ready : bit
            out commit_ready : bit
            out read_valid : bit
            out read_data : bits<8>
            inst storage : StoragePingPongBits<W=8,N=4>
            storage.write = write
            storage.write_index = write_index
            storage.write_data = write_data
            storage.commit = commit
            storage.retire = retire
            storage.read_index = read_index
            write_ready = storage.status.write_ready
            commit_ready = storage.status.commit_ready
            read_valid = storage.status.read_valid
            read_data = storage.status.read_data
        }
    """,
    "coding": """
        import std.coding
        module Top {
            in a : bits<8>
            in taps : bits<8>
            out parity_bit : bit
            out reversed : bits<8>
            out stepped : bits<8>
            parity_bit = coding_parity<N=8>(a)
            reversed = coding_reverse_bits<N=8>(a)
            stepped = coding_lfsr_step<N=8>(a, taps)
        }
    """,
    "coding_dot": """
        import std.coding
        module Top {
            in a : vec<4,u8>
            in b : vec<4,u8>
            out y : u18
            y = coding_convolution_dot(a, b)
        }
    """,
    "coding_lfsr_checked": """
        import std.coding
        module Top {
            in state : bits<8>
            in taps : bits<8>
            out next_state : bits<8>
            inst step : CodingLfsrStep<N=8>
            step.state = state
            step.polynomial_taps = taps
            next_state = step.next_state
        }
    """,
    "coding_table_gather": """
        import std.coding
        module Top {
            in values : vec<8,u8>
            in order : vec<8,u3>
            out gathered : vec<8,u8>
            gathered = table_gather<T=u8,N=8,IW=3>(values, order)
        }
    """,
}


def test_complex_core_is_profile_and_transport_neutral() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "stdlib" / "math" / "complex.zl"
    ).read_text()
    assert "fixed<18,16>" not in source
    assert "AXIStream" not in source
    assert "std.bus" not in source
    assert "butterfly_quantized" in source


def test_fixed_abs_specializes_to_widened_exact_type_and_preserves_raw_magnitude() -> None:
    result = compile_source(
        "import std.math.fixed "
        "module Top { in value:fixed<8,4> out magnitude:fixed<9,4> "
        "magnitude=fixed_abs(value) }",
        top="Top",
        include_clash=False,
    )
    assignment = result.ir.assignments[0]
    assert isinstance(assignment.expression, ir_expr.Call)
    assert assignment.expression.type == FixedType(9, 4)

    specialization = next(
        function
        for function in result.ir.callable_definitions
        if function.metadata.source_name == "fixed_abs"
    )
    assert specialization.parameters[0].type == FixedType(8, 4)
    assert specialization.return_type == FixedType(9, 4)
    assert isinstance(specialization.body, ir_expr.Mux)
    assert specialization.body.type == FixedType(9, 4)

    # Simulator values are exact signed raw fixed-point integers.  Widening is
    # essential here: abs(-128) is representable in fixed<9,4>, but not in the
    # original wrapping fixed<8,4> input type.
    for raw, expected in ((-128, 128), (-17, 17), (-1, 1), (0, 0), (127, 127)):
        assert simulate(result.ir, value=raw) == {"magnitude": expected}


def test_fixed_mac_specializes_to_full_precision_type_and_is_raw_exact() -> None:
    result = compile_source(
        "import std.math.fixed "
        "module Top { in a:fixed<8,4> in b:fixed<8,4> "
        "in accumulator:fixed<16,8> out result:fixed<17,8> "
        "result=fixed_mac(a,b,accumulator) }",
        top="Top",
        include_clash=False,
    )
    assignment = result.ir.assignments[0]
    assert isinstance(assignment.expression, ir_expr.Call)
    assert assignment.expression.type == FixedType(17, 8)

    specialization = next(
        function
        for function in result.ir.callable_definitions
        if function.metadata.source_name == "fixed_mac"
    )
    assert tuple(parameter.type for parameter in specialization.parameters) == (
        FixedType(8, 4),
        FixedType(8, 4),
        FixedType(16, 8),
    )
    assert specialization.return_type == FixedType(17, 8)
    assert isinstance(specialization.body, ir_expr.Add)
    assert isinstance(specialization.body.left, ir_expr.Binary)
    assert specialization.body.left.operator is ir_expr.BinaryOperator.MULTIPLY
    assert specialization.body.left.type == FixedType(16, 8)

    # Raw products retain F=8 and are added once to the exact raw accumulator.
    cases = (
        (-24, 32, 64, -704),  # -1.5 * 2.0 + 0.25 = -2.75
        (-128, -128, 32767, 49151),
        (127, 127, -32768, -16639),
        (0, -128, -32768, -32768),
    )
    for a, b, accumulator, expected in cases:
        assert simulate(
            result.ir,
            a=a,
            b=b,
            accumulator=accumulator,
        ) == {"result": expected}


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "import std.stream.serialization "
            "module Top { clock clk reset rst in i:rv<vec<3,u8>> "
            "out o:rv<u8> inst s:RvVectorSerializer<T=u8,N=3,IW=2> "
            "connect i -> s.input connect s.output -> o }",
            "parameter constraint",
        ),
        (
            "import std.coding module Top { in s:bits<1> in t:bits<1> "
            "out n:bits<1> inst step:CodingLfsrStep<N=1> "
            "step.state=s step.polynomial_taps=t n=step.next_state }",
            "parameter constraint",
        ),
    ),
)
def test_stdlib_module_constraints_fail_at_specialization(
    source: str,
    message: str,
) -> None:
    with pytest.raises(SemanticError, match=message):
        compile_source(source, top="Top", include_clash=False)


def test_generic_storage_fails_but_range_proven_gather_succeeds() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "docs" / "reproducers" / "stdlib_generic_storage_blockers.zl"
    ).read_text()
    with pytest.raises(
        SemanticError,
        match="initializer must specialize to a concrete vec<8,u8> vector",
    ):
        compile_source(source, top="GenericRomTop", include_clash=False)

    gather = compile_source(
        source, top="GenericPermutationTop", include_clash=False
    )
    assert simulate(
        gather.ir,
        values=[10, 20, 30, 40, 50, 60, 70, 80],
        order=[3, 3, 0, 7, 1, 6, 5, 2],
    ) == {"result": [40, 40, 10, 80, 20, 70, 60, 30]}


def test_new_stdlib_families_have_concrete_semantic_witnesses() -> None:
    for name, source in WITNESSES.items():
        result = compile_source(source, top="Top", include_clash=False)
        assert result.ir.name == "Top", name
        assert result.ir.library_dependencies, name


def test_stdlib_table_gather_preserves_source_order_without_assuming_permutation() -> None:
    result = compile_source(
        WITNESSES["coding_table_gather"], top="Top", include_clash=False
    )
    assert simulate(
        result.ir,
        values=[10, 20, 30, 40, 50, 60, 70, 80],
        order=[7, 0, 7, 2, 2, 4, 1, 6],
    ) == {"gathered": [80, 10, 80, 30, 30, 50, 20, 70]}


def test_new_stdlib_witnesses_emit_deterministic_direct_sv() -> None:
    for name, source in WITNESSES.items():
        result = compile_source(source, top="Top", include_clash=False)
        first = emit_sv_artifact(result.ir, selected_ir_identity=f"stdlib:{name}")
        second = emit_sv_artifact(result.ir, selected_ir_identity=f"stdlib:{name}")
        assert first.text == second.text, name
        assert first.artifact_hash == second.artifact_hash, name
        assert first.library_dependencies == result.ir.library_dependencies, name


def test_new_stdlib_witnesses_emit_deterministic_clash() -> None:
    for name, source in WITNESSES.items():
        first = compile_source(source, top="Top")
        second = compile_source(source, top="Top")
        assert first.clash, name
        assert first.clash == second.clash, name


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_new_stdlib_witnesses_are_strict_verilator_clean(tmp_path: Path) -> None:
    for name, source in WITNESSES.items():
        result = compile_source(source, top="Top", include_clash=False)
        artifact = emit_sv_artifact(
            result.ir,
            selected_ir_identity=f"stdlib:{name}",
        )
        rtl = tmp_path / f"{name}.sv"
        rtl.write_text(artifact.text)
        completed = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "-Wall",
                "-Wno-DECLFILENAME",
                "-Wno-UNUSEDSIGNAL",
                "-Wno-UNDRIVEN",
                str(rtl),
            ),
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (
            name,
            completed.stdout,
            completed.stderr,
        )
