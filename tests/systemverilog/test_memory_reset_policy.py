from __future__ import annotations

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source


def _global_memory(
    *,
    latency: int,
    collision: str = "read_first",
    contents: str | None = None,
    read_data: str | None = None,
    masked: bool = False,
) -> str:
    reset = (
        ""
        if contents is None or read_data is None
        else (
            "reset { "
            f"contents {contents} read_data {read_data} "
            "}"
        )
    )
    mask_port = "in mask:bits<2>" if masked else ""
    mask_binding = "table.write_mask=mask" if masked else ""
    return f"""
module MemoryProfile {{
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16 {mask_port}
  out q:u16
  memory table:mem<u16,4> {{
    read_latency {latency}
    collision {collision}
    {reset}
  }}
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  {mask_binding}
  q=table.read_data
}}
"""


def _emit(source: str) -> str:
    module = compile_source(source, include_clash=False).ir
    first = emit_experimental(module)
    assert emit_experimental(module) == first
    return first


def test_default_synchronous_memory_keeps_the_legacy_reset_shape() -> None:
    text = _emit(_global_memory(latency=1))

    assert "  initial begin" not in text
    assert "  always_ff @(posedge clk) begin" in text
    assert "      zlang_table_read_data <= '0;" in text
    assert (
        "zlang_table_cells[zlang_memory_reset_index] <= '0;" in text
    )


def test_zero_latency_masked_write_first_has_one_combinational_read_path() -> None:
    text = _emit(
        _global_memory(
            latency=0,
            collision="write_first",
            contents="preserve",
            read_data="clear",
            masked=True,
        )
    )

    read_driver = next(
        line
        for line in text.splitlines()
        if line.startswith("  assign zlang_table_read_data =")
    )
    assert "rst ? '0" in read_driver
    assert "!rst && write_enable" in read_driver
    assert "zlang_table_write_merged" in read_driver
    assert "zlang_table_cells[address]" in read_driver
    assert text.count("assign zlang_table_read_data =") == 1
    assert "zlang_table_read_data <=" not in text
    assert "zlang_table_cells[zlang_memory_reset_index] = '0;" in text
    assert "zlang_table_cells[zlang_memory_reset_index] <= '0;" not in text
    assert "  always @(posedge clk) begin" in text


def test_registered_preserve_profile_initializes_once_and_holds_on_reset() -> None:
    text = _emit(
        _global_memory(
            latency=1,
            contents="preserve",
            read_data="preserve",
        )
    )

    assert "    zlang_table_read_data = '0;" in text
    assert "zlang_table_cells[zlang_memory_reset_index] = '0;" in text
    assert "zlang_table_read_data <= '0;" not in text
    assert "zlang_table_cells[zlang_memory_reset_index] <= '0;" not in text
    assert "Memory contents and read result hold across reset" in text
    assert "  always @(posedge clk) begin" in text


def test_scheduled_memory_uses_the_same_independent_reset_policies() -> None:
    source = """
module ScheduledMemoryProfile {
  clock clk reset rst
  in address:u2 in read_enable:bit in write_enable:bit in data:u8
  out q:u8
  memory table:mem<u8,4> {
    read_latency 1
    collision write_first
    reset { contents preserve read_data clear }
  }
  fetch: when read_enable { table.read(address) }
  store: when write_enable { table.write(address,data) }
  q=table.read_data
}
"""
    text = _emit(source)

    assert "zlang_table_cells[zlang_table_reset_index] = '0;" in text
    assert "zlang_table_cells[zlang_table_reset_index] <= '0;" not in text
    assert "      zlang_table_read_data <= '0;" in text
    assert "  always @(posedge clk) begin" in text
    assert "if (zlang_table_write_fire)" in text
    assert "if (zlang_table_read_fire) begin" in text
