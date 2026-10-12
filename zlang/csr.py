"""Software-readable CSR artifacts emitted from typed IR."""

from __future__ import annotations

import json

from zlang.csr_documentation import build_csr_documentation
from zlang.ir.module import Module


def emit_csr_json(module: Module) -> str:
    csr = build_csr_documentation(module)
    document = {
        "module": csr.module_name,
        "data_width": csr.data_width,
        "address_width": csr.address_width,
        "blocks": [
            {
                "name": block.name,
                "base_address": block.base_address,
                "registers": [
                    {
                        "name": register.name,
                        **(
                            {"logical_path": list(register.logical_path)}
                            if register.logical_path
                            else {}
                        ),
                        "offset": register.offset,
                        "address": block.base_address + register.offset,
                        "fields": [
                            {
                                "name": field.name,
                                "type": field.type_name,
                                "access": field.access,
                                "msb": field.msb,
                                "lsb": field.lsb,
                                "width": field.width,
                                "reset": field.reset,
                                **(
                                    {
                                        "hardware": {
                                            "kind": field.hardware.kind,
                                            "signal": field.hardware.signal,
                                            "priority": field.hardware.priority,
                                        }
                                    }
                                    if field.hardware is not None
                                    else {}
                                ),
                            }
                            for field in register.fields
                        ],
                        "events": [
                            {
                                "name": event.name,
                                "kind": event.kind,
                                "phase": event.phase,
                                "type": event.type_name,
                                "msb": event.msb,
                                "lsb": event.lsb,
                                "signal": event.signal,
                            }
                            for event in register.events
                        ],
                    }
                    for register in block.registers
                ],
                "split_views": [
                    {
                        "name": view.name,
                        "field": view.field_name,
                        "type": view.type_name,
                        "order": view.order,
                        "physical_registers": list(view.physical_registers),
                    }
                    for view in block.split_views
                ],
            }
            for block in csr.blocks
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def emit_csr_markdown(module: Module) -> str:
    csr = build_csr_documentation(module)
    lines = [
        f"# {csr.module_name} CSR map",
        "",
        f"Bus width: {csr.data_width} bits.",
        "",
    ]
    for block in csr.blocks:
        lines.extend(
            (
                f"## {block.name}",
                "",
                f"Base address: `0x{block.base_address:08x}`",
                "",
            )
        )
        for register in block.registers:
            lines.extend(
                (
                    f"### {register.logical_name}",
                    "",
                    f"Offset `0x{register.offset:02x}`, address "
                    f"`0x{register.address:08x}`.",
                    "",
                    (
                        "| Field | Bits | Type | Access | Reset | Hardware | Priority |"
                        if csr.has_hardware_bindings
                        else "| Field | Bits | Type | Access | Reset |"
                    ),
                    (
                        "|---|---:|---|---|---:|---|---|"
                        if csr.has_hardware_bindings
                        else "|---|---:|---|---|---:|"
                    ),
                )
            )
            for field in register.fields:
                bits = (
                    str(field.lsb)
                    if field.msb == field.lsb
                    else f"{field.msb}:{field.lsb}"
                )
                row = (
                    f"| {field.name} | {bits} | `{field.type_name}` | "
                    f"`{field.access}` | `0x{field.reset:x}` |"
                )
                if csr.has_hardware_bindings:
                    hardware = (
                        f"`{field.hardware.kind}:{field.hardware.signal}`"
                        if field.hardware is not None
                        else "—"
                    )
                    priority = (
                        f"`{field.hardware.priority}`"
                        if field.hardware is not None
                        and field.hardware.priority is not None
                        else "—"
                    )
                    row += f" {hardware} | {priority} |"
                lines.append(row)
            if register.events:
                lines.extend(("", "Access events:", ""))
                for event in register.events:
                    bits = (
                        str(event.lsb)
                        if event.msb == event.lsb
                        else f"{event.msb}:{event.lsb}"
                    )
                    lines.append(
                        f"- `{event.name}`: `{event.kind}` `{event.phase}` bits "
                        f"{bits} -> `{event.signal}`"
                    )
            lines.append("")
        if block.split_views:
            lines.extend(("### Logical split values", ""))
            for view in block.split_views:
                lines.append(
                    f"- `{view.name}.{view.field_name}`: `{view.type_name}` "
                    f"(`{view.order}`)"
                )
            lines.append("")
    return "\n".join(lines)
