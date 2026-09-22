"""Software-readable CSR artifacts emitted from typed IR."""

from __future__ import annotations

import json

from zlang.ir.module import Module


def emit_csr_json(module: Module) -> str:
    document = {
        "module": module.name,
        "data_width": 32,
        "address_width": 32,
        "blocks": [
            {
                "name": block.name,
                "base_address": block.base_address,
                "registers": [
                    {
                        "name": register.name,
                        **(
                            {"logical_path": list(register.projection_path)}
                            if register.projection_path
                            else {}
                        ),
                        "offset": register.offset,
                        "address": block.base_address + register.offset,
                        "fields": [
                            {
                                "name": field.name,
                                "type": str(field.type),
                                "access": field.access.value,
                                "msb": field.msb,
                                "lsb": field.lsb,
                                "width": field.width,
                                "reset": field.reset,
                                **(
                                    {
                                        "hardware": {
                                            "kind": field.binding.kind.value,
                                            "signal": field.binding.signal,
                                            "priority": (
                                                field.binding.priority.value
                                                if field.binding.priority is not None
                                                else None
                                            ),
                                        }
                                    }
                                    if field.binding is not None
                                    else {}
                                ),
                            }
                            for field in register.fields
                        ],
                        "events": [
                            {
                                "name": event.name,
                                "kind": event.kind.value,
                                "type": str(event.canonical_type),
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
                        "type": str(view.canonical_type),
                        "order": "low_first" if view.low_first else "high_first",
                        "physical_registers": [
                            next(
                                register.name
                                for register in block.registers
                                if any(field.identity == view.low_field_id for field in register.fields)
                            ),
                            next(
                                register.name
                                for register in block.registers
                                if any(field.identity == view.high_field_id for field in register.fields)
                            ),
                        ],
                    }
                    for view in block.split_views
                ],
            }
            for block in module.csr_blocks
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def emit_csr_markdown(module: Module) -> str:
    lines = [f"# {module.name} CSR map", "", "Bus width: 32 bits.", ""]
    has_hardware_bindings = any(
        field.binding is not None
        for block in module.csr_blocks
        for register in block.registers
        for field in register.fields
    )
    for block in module.csr_blocks:
        lines.extend(
            (
                f"## {block.name}",
                "",
                f"Base address: `0x{block.base_address:08x}`",
                "",
            )
        )
        for register in block.registers:
            address = block.base_address + register.offset
            logical_name = (
                ".".join(register.projection_path)
                if register.projection_path
                else register.name
            )
            lines.extend(
                (
                    f"### {logical_name}",
                    "",
                    f"Offset `0x{register.offset:02x}`, address `0x{address:08x}`.",
                    "",
                    (
                        "| Field | Bits | Type | Access | Reset | Hardware | Priority |"
                        if has_hardware_bindings
                        else "| Field | Bits | Type | Access | Reset |"
                    ),
                    (
                        "|---|---:|---|---|---:|---|---|"
                        if has_hardware_bindings
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
                    f"| {field.name} | {bits} | `{field.type}` | "
                    f"`{field.access.value}` | `0x{field.reset:x}` |"
                )
                if has_hardware_bindings:
                    hardware = (
                        f"`{field.binding.kind.value}:{field.binding.signal}`"
                        if field.binding is not None
                        else "—"
                    )
                    priority = (
                        f"`{field.binding.priority.value}`"
                        if field.binding is not None
                        and field.binding.priority is not None
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
                        f"- `{event.name}`: `{event.kind.value}` bits {bits} -> "
                        f"`{event.signal}`"
                    )
            lines.append("")
        if block.split_views:
            lines.extend(("### Logical split values", ""))
            for view in block.split_views:
                lines.append(
                    f"- `{view.name}.{view.field_name}`: `{view.canonical_type}` "
                    f"(`{'low_first' if view.low_first else 'high_first'}`)"
                )
            lines.append("")
    return "\n".join(lines)
