"""Packet-arbiter SystemVerilog emission."""

from __future__ import annotations

from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir import module as ir_module
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import rendering as sv_rendering


def _emit_packet_arbiter(module: ir_module.Module) -> str:
    if module.clock is None or module.reset is None or len(module.arbiters) != 1:
        raise SystemVerilogEmissionError(
            "direct packet arbitration requires one clocked arbiter"
        )
    if (
        module.assignments or module.connections or module.registers
        or module.rules or module.fifos or module.memories or module.csr_blocks
        or module.request_responses
    ):
        raise SystemVerilogEmissionError(
            "direct packet arbiter currently requires only arbiter endpoints"
        )
    arbiter = module.arbiters[0]
    endpoints = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if {port.name for port in module.ports} != endpoints:
        raise SystemVerilogEmissionError(
            "direct packet arbiter currently requires only arbiter endpoints"
        )

    sources = tuple(sv_rendering._identifier(source.name) for source in arbiter.sources)
    destination = sv_rendering._identifier(arbiter.destination.name)
    count = len(sources)
    owner_width = max(1, (count - 1).bit_length())
    ports = [
        f"input wire logic {sv_rendering._identifier(module.clock)}",
        f"input wire logic {sv_rendering._identifier(module.reset)}",
    ]
    for source, name in zip(arbiter.sources, sources, strict=True):
        ports.extend((
            sv_boundary._logic_port("input", f"{name}_payload", source.type),
            f"input wire logic {name}_valid",
            f"input wire logic {name}_last",
            f"output logic {name}_ready",
        ))
    ports.extend((
        sv_boundary._logic_port(
            "output", f"{destination}_payload", arbiter.destination.type
        ),
        f"output logic {destination}_valid",
        f"output logic {destination}_last",
        f"input wire logic {destination}_ready",
    ))

    lines = [
        f"  logic [{owner_width - 1}:0] zlang_candidate;",
        "  logic zlang_candidate_valid;",
        f"  logic [{owner_width - 1}:0] zlang_selected;",
        "  logic zlang_grant_active;",
        f"  logic [{owner_width - 1}:0] zlang_grant_owner;",
        "  logic zlang_transfer, zlang_grant_complete;",
    ]
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.append(f"  logic [{owner_width - 1}:0] zlang_next_priority;")

    lines.extend((
        "  always_comb begin",
        "    zlang_candidate = '0;",
        "    zlang_candidate_valid = 1'b0;",
    ))
    if arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
        for index, name in enumerate(sources):
            keyword = "if" if index == 0 else "else if"
            lines.extend((
                f"    {keyword} ({name}_valid) begin",
                f"      zlang_candidate = {owner_width}'d{index};",
                "      zlang_candidate_valid = 1'b1;",
                "    end",
            ))
    else:
        lines.append("    case (zlang_next_priority)")
        for start in range(count):
            lines.append(f"      {owner_width}'d{start}: begin")
            for offset in range(count):
                index = (start + offset) % count
                keyword = "if" if offset == 0 else "else if"
                lines.extend((
                    f"        {keyword} ({sources[index]}_valid) begin",
                    f"          zlang_candidate = {owner_width}'d{index};",
                    "          zlang_candidate_valid = 1'b1;",
                    "        end",
                ))
            lines.append("      end")
        lines.extend(("      default: begin end", "    endcase"))
    lines.extend((
        "  end",
        "  always_comb begin",
        "    zlang_selected = zlang_grant_active ? zlang_grant_owner : zlang_candidate;",
        f"    {destination}_payload = '0;",
        f"    {destination}_valid = 1'b0;",
        f"    {destination}_last = 1'b0;",
        *(f"    {name}_ready = 1'b0;" for name in sources),
        "    case (zlang_selected)",
    ))
    for index, name in enumerate(sources):
        lines.extend((
            f"      {owner_width}'d{index}: begin",
            f"        {destination}_payload = {name}_payload;",
            f"        {destination}_valid = {name}_valid;",
            f"        {destination}_last = {name}_last;",
            "      end",
        ))
    lines.extend((
        "      default: begin end",
        "    endcase",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) {destination}_valid = 1'b0;",
        f"    if ({sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && {destination}_valid && {destination}_ready) begin",
        "      case (zlang_selected)",
        *(f"        {owner_width}'d{index}: {name}_ready = 1'b1;"
          for index, name in enumerate(sources)),
        "        default: begin end",
        "      endcase",
        "    end",
        "  end",
        f"  assign zlang_transfer = {destination}_valid && {destination}_ready;",
        (
            "  assign zlang_grant_complete = zlang_transfer;"
            if arbiter.grant_scope is GrantScope.BEAT
            else f"  assign zlang_grant_complete = zlang_transfer && {destination}_last;"
        ),
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        "      zlang_grant_active <= 1'b0;",
        "      zlang_grant_owner <= '0;",
    ))
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.append("      zlang_next_priority <= '0;")
    lines.extend((
        "    end else begin",
        "      if (!zlang_grant_active && zlang_candidate_valid) begin",
        "        zlang_grant_owner <= zlang_selected;",
        "        zlang_grant_active <= !zlang_grant_complete;",
        "      end else if (zlang_grant_active && zlang_grant_complete) begin",
        "        zlang_grant_active <= 1'b0;",
        "      end",
    ))
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.extend((
            "      if (zlang_grant_complete) begin",
            f"        zlang_next_priority <= (zlang_selected == {owner_width}'d{count - 1}) "
            "? '0 : zlang_selected + 1'b1;",
            "      end",
        ))
    lines.extend(("    end", "  end"))
    return sv_module_rendering._module(module, ports, lines)
