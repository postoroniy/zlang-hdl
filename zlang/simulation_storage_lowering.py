# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Memory and FIFO reference lowering for the primitive simulation VM."""

from __future__ import annotations

from typing import Any

from zlang.simulation_primitive_model import PrimitiveLowerer, PrimitiveLoweringError


class StorageReferencePrimitiveLowerer(PrimitiveLowerer):
    """Lower compiler-owned storage observations without owning the VM graph."""

    def try_lower(
        self,
        node: dict[str, Any],
        operands: tuple[int, ...],
        origins: list[dict[str, Any]],
    ) -> int | None:
        op = node["op"]
        if op not in {"memory_port_read", "fifo_ref"}:
            return None
        builder = self._builder
        width = int(node["type"]["width"])
        attrs = node["attributes"]
        if op == "memory_port_read":
            memory_name = attrs.get("memory")
            memory = builder.semantic_memories.get(memory_name)
            if memory is None:
                raise PrimitiveLoweringError(
                    f"memory read references unknown memory '{memory_name}'"
                )
            address = operands[0]
            reset_name = memory.get("reset")
            reset = (
                builder.emit(
                    "load_input",
                    1,
                    attributes={"name": f"$reset:{reset_name}"},
                )
                if reset_name is not None
                else builder.constant(0, 1)
            )
            release_clock = (
                builder.reset_release_clocks.get(str(reset_name))
                if reset_name is not None
                else None
            )
            if release_clock is not None:
                release = builder.emit(
                    "load_state",
                    32,
                    attributes={"name": f"$release:{release_clock}"},
                )
                reset = builder.binary("or", reset, builder.truthy(release), 1)
            old = builder.emit(
                "load_memory", width, (address,), {"memory": memory_name}, origins
            )
            result = old
            if memory["collision"] == "write_first":
                ports = {item["name"]: item for item in memory["ports"]}
                priority = memory["write_priority"] or [
                    item["name"]
                    for item in memory["ports"]
                    if item["write_enable"] is not None
                ]
                # Reverse nested selects so the first priority entry wins.
                for writer_name in reversed(priority):
                    writer = ports[writer_name]
                    if writer["write_enable"] is None:
                        continue
                    write_address = builder.lower(writer["write_address"])
                    write_enable = builder.truthy(builder.lower(writer["write_enable"]))
                    write_enable = builder.binary(
                        "and", builder.unary("not", reset, 1), write_enable, 1
                    )
                    same_address = builder.binary("eq", address, write_address, 1)
                    active = builder.binary("and", write_enable, same_address, 1)
                    new = builder.lower(writer["write_data"])
                    mask_id = writer["write_mask"]
                    merged = builder.merge_masked_value(
                        old,
                        new,
                        builder.lower(mask_id) if mask_id is not None else None,
                        width=width,
                        mask_width=memory["write_mask_width"],
                    )
                    result = builder.select(active, merged, result, width)
            if memory["read_data_reset"] == "clear":
                result = builder.select(reset, builder.constant(0, width), result, width)
        elif op == "fifo_ref":
            fifo_name = attrs.get("fifo")
            signal = attrs.get("signal")
            fifo = builder.semantic_fifos.get(fifo_name)
            if fifo is None:
                raise PrimitiveLoweringError(
                    f"FIFO reference names unknown FIFO '{fifo_name}'"
                )
            count = builder.emit(
                "load_state",
                int(fifo["count_width"]),
                attributes={"name": fifo["count"]},
            )
            read_pointer = builder.emit(
                "load_state",
                int(fifo["pointer_width"]),
                attributes={"name": fifo["read_pointer"]},
            )
            empty = builder.binary(
                "eq", count, builder.constant(0, builder.width(count)), 1
            )
            full = builder.binary(
                "eq",
                count,
                builder.constant(int(fifo["depth"]), builder.width(count)),
                1,
            )
            reset = (
                builder.emit(
                    "load_input", 1, attributes={"name": f"$reset:{fifo['reset']}"}
                )
                if fifo["reset"] is not None
                else builder.constant(0, 1)
            )
            not_reset = builder.unary("not", reset, 1)
            if fifo["scheduled"] and signal in {
                "push",
                "pop",
                "overflow",
                "underflow",
            }:
                result = builder.constant(0, 1)
            elif signal in {"front", "data"}:
                front = builder.emit(
                    "load_memory",
                    width,
                    (read_pointer,),
                    {"memory": fifo["memory"]},
                    origins,
                )
                result = builder.select(empty, builder.constant(0, width), front, width)
            elif signal == "count":
                result = builder.resize(count, width)
            elif signal == "empty":
                result = empty
            elif signal == "full":
                result = full
            elif signal == "valid":
                result = builder.binary("and", not_reset, builder.unary("not", empty, 1), 1)
            elif signal == "ready":
                pop = builder.constant(0, 1)
                if not fifo["scheduled"]:
                    pop_request = builder.truthy(builder.lower(int(fifo["pop"])))
                    pop = builder.binary(
                        "and",
                        not_reset,
                        builder.binary(
                            "and", pop_request, builder.unary("not", empty, 1), 1
                        ),
                        1,
                    )
                result = builder.binary(
                    "and",
                    not_reset,
                    builder.binary("or", builder.unary("not", full, 1), pop, 1),
                    1,
                )
            elif signal == "push":
                result = builder.truthy(builder.lower(int(fifo["push"])))
            elif signal == "pop":
                result = builder.truthy(builder.lower(int(fifo["pop"])))
            elif signal == "overflow":
                pop_request = builder.truthy(builder.lower(int(fifo["pop"])))
                pop = builder.binary(
                    "and",
                    not_reset,
                    builder.binary(
                        "and", pop_request, builder.unary("not", empty, 1), 1
                    ),
                    1,
                )
                push_request = builder.truthy(builder.lower(int(fifo["push"])))
                result = builder.binary(
                    "and",
                    not_reset,
                    builder.binary(
                        "and", push_request, builder.binary("and", full, builder.unary("not", pop, 1), 1), 1
                    ),
                    1,
                )
            elif signal == "underflow":
                pop_request = builder.truthy(builder.lower(int(fifo["pop"])))
                result = builder.binary(
                    "and", not_reset, builder.binary("and", pop_request, empty, 1), 1
                )
            else:
                raise PrimitiveLoweringError(
                    f"FIFO '{fifo_name}' has unknown signal '{signal}'"
                )
        return result
