"""Bounded artifact-mutation helpers, independent of optional external tools."""

from pathlib import Path

import pytest

from tools.math_exploration_formal import mutate_output, run


RTL = """module Example(input clk, rst, input [3:0] a, output [7:0] result);
  reg [3:0] pipeline_42_s1;
  always @(posedge clk) begin
    if (rst) begin
      pipeline_42_s1 <= '0;
    end else begin
      pipeline_42_s1 <= a;
    end
  end
  assign result = {{4{1'b0}}, pipeline_42_s1};
endmodule
"""


def test_output_bit_flip_changes_only_exact_output_rhs() -> None:
    mutated = mutate_output(RTL, "result", 8, "output_bit_flip")
    assert "assign result = ({{4{1'b0}}, pipeline_42_s1}) ^ 8'd1;" in mutated
    assert mutated.replace("({{4{1'b0}}, pipeline_42_s1}) ^ 8'd1", "{{4{1'b0}}, pipeline_42_s1}") == RTL


def test_missing_stage_uses_exact_generated_next_state() -> None:
    mutated = mutate_output(RTL, "result", 8, "missing_final_stage")
    assert "assign result = {{4{1'b0}}, (a)};" in mutated
    assert "pipeline_42_s1 <= a;" in mutated


@pytest.mark.parametrize("mutation", ("output_bit_flip", "missing_final_stage"))
def test_missing_or_ambiguous_output_fails_closed(mutation: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        mutate_output(RTL, "missing", 8, mutation)
    with pytest.raises(ValueError, match="exactly one"):
        mutate_output(RTL + "  assign result = a;\n", "result", 8, mutation)


def test_missing_stage_rejects_unknown_register_shape() -> None:
    with pytest.raises(ValueError, match="reset and next-state"):
        mutate_output(RTL.replace("pipeline_42_s1 <= '0;", ""), "result", 8, "missing_final_stage")
    with pytest.raises(ValueError, match="one final"):
        mutate_output(RTL.replace("{{4{1'b0}}, pipeline_42_s1}", "a"), "result", 8, "missing_final_stage")


def test_unknown_mutation_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown mutation"):
        mutate_output(RTL, "result", 8, "bad")


def test_driver_preserves_existing_evidence_and_checks_depth(tmp_path: Path) -> None:
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("existing evidence")
    with pytest.raises(ValueError, match="must not already exist"):
        run(tmp_path)
    assert sentinel.read_text() == "existing evidence"
    with pytest.raises(ValueError, match="depth >= 8"):
        run(tmp_path / "unused", depth=7)
    assert not (tmp_path / "unused").exists()
