from pathlib import Path

from zlang.formal_trace import TraceBinding, decode_vcd_trace
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.types import (
    BitType,
    EnumType,
    FixedType,
    SIntType,
    UIntType,
    VecType,
)


def _vcd(path: Path) -> Path:
    path.write_text("""$date today $end
$version test $end
$timescale 1ns $end
$scope module top $end
$var integer 32 ! smt_step $end
$var wire 8 \" signed_value $end
$var wire 8 # fixed_value $end
$var wire 3 $ state $end
$var wire 8 % lanes $end
$var wire 1 & reset $end
$var wire 3 ' comparison_valid $end
$var wire 1 ( unknown_value $end
$upscope $end
$enddefinitions $end
#0
b00000000000000000000000000000000 !
b00000000 \"
b00000000 #
b000 $
b00000000 %
1&
b000 '
0(
#10
b00000000000000000000000000000101 !
b11111111 \"
b11111000 #
b100 $
b10100011 %
0&
b111 '
x(
""")
    return path


def test_shared_decoder_reports_cycles_window_state_and_typed_values(
    tmp_path: Path,
) -> None:
    state = EnumType(
        "State",
        ("Idle", "Done"),
        "state-declaration",
        3,
        (0, 4),
    )
    decoded = decode_vcd_trace(
        _vcd(tmp_path / "trace.vcd"),
        cycle=5,
        comparison_window=ComparisonWindow.reset_fill(2),
        bindings=(
            TraceBinding("signed", "signed_value", 8, SIntType(8), "signed"),
            TraceBinding("fixed", "fixed_value", 8, FixedType(8, 4), "signed"),
            TraceBinding("state", "state", 3, state, "unsigned"),
            TraceBinding("lanes", "lanes", 8, VecType(2, UIntType(4)), "unsigned"),
            TraceBinding("reset", "reset", 1, BitType(), "bit"),
            TraceBinding("comparison_valid", "comparison_valid", 3, "bits<3>", "bits"),
            TraceBinding("unknown", "unknown_value", 1, BitType(), "bit"),
        ),
    )

    assert decoded.failure_cycle == 5
    assert decoded.sample_cycle == 3
    assert decoded.reset_state == "0"
    assert decoded.comparison_valid_state == "0b111"
    values = dict(decoded.values)
    assert values["signed"] == "-1"
    assert values["fixed"] == (
        '{"raw":-8,"type":"fixed<8,4>","value":"-1/2"}'
    )
    assert values["state"] == '{"code":4,"enum":"State.Done"}'
    assert values["lanes"] == "[10,3]"
    assert values["unknown"] == "x"


def test_decoder_uses_last_solver_step_and_keeps_untyped_values_raw(
    tmp_path: Path,
) -> None:
    decoded = decode_vcd_trace(
        _vcd(tmp_path / "trace.vcd"),
        cycle=None,
        comparison_window=ComparisonWindow.same_cycle(),
        bindings=(TraceBinding("raw", "signed_value", 8),),
    )

    assert decoded.failure_cycle == 5
    assert decoded.sample_cycle == 5
    assert decoded.values == (("raw", "0b11111111"),)


def test_decoder_does_not_guess_between_divergent_duplicate_leaves(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous.vcd"
    path.write_text("""$scope module top $end
$var integer 32 ! smt_step $end
$scope module a $end
$var wire 1 \" value $end
$upscope $end
$scope module b $end
$var wire 1 # value $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b0 !
0\"
1#
""")

    decoded = decode_vcd_trace(
        path,
        cycle=0,
        bindings=(TraceBinding("value", "value", 1, BitType(), "bit"),),
    )

    assert decoded.values == ()

    scoped = decode_vcd_trace(
        path,
        cycle=0,
        bindings=(TraceBinding("value", "a.value", 1, BitType(), "bit"),),
    )
    assert scoped.values == (("value", "0"),)


def test_effective_reset_trace_takes_precedence_over_raw_physical_reset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.vcd"
    path.write_text("""$scope module top $end
$var integer 32 ! smt_step $end
$var wire 1 \" arst_n $end
$var wire 1 # zlang_formal_reset_active $end
$upscope $end
$enddefinitions $end
#0
b00000000000000000000000000000010 !
1\"
1#
""")
    decoded = decode_vcd_trace(
        path,
        cycle=2,
        bindings=(
            TraceBinding("physical_reset", "arst_n", 1, BitType(), "bit"),
            TraceBinding(
                "trace:reset",
                "zlang_formal_reset_active",
                1,
                BitType(),
                "bit",
            ),
        ),
    )
    assert decoded.reset_state == "1"
    assert dict(decoded.values) == {
        "physical_reset": "1",
        "trace:reset": "1",
    }


def test_missing_or_non_solver_vcd_fails_closed(tmp_path: Path) -> None:
    missing = decode_vcd_trace(
        tmp_path / "missing.vcd", cycle=3, bindings=()
    )
    assert missing.values == ()
    assert missing.trace_path is None

    path = tmp_path / "plain.vcd"
    path.write_text("$enddefinitions $end\n")
    plain = decode_vcd_trace(path, cycle=3, bindings=())
    assert plain.values == ()
    assert plain.failure_cycle == 3
