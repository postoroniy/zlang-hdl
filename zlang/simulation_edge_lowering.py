# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Clock-edge, storage, FIFO, and verification-probe primitive lowering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zlang.simulation_primitive_model import PrimitiveLoweringError
from zlang.simulation_primitive_model import PrimitiveBuilder


@dataclass
class EdgeProgramLowerer:
    """Own all state transitions evaluated on primitive clock edges."""

    payload: dict[str, Any]
    builder: PrimitiveBuilder
    registers: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    next_by_domain: dict[str, dict[str, int]]
    scheduled_storage_actions: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self._register_by_name = {
            item["name"]: item for item in self.registers
        }
        self._memory_by_name = {item["name"]: item for item in self.memories}
        self._preserve_read_registers = {
            name
            for memory in self.payload["memories"]
            if memory.get("read_data_reset") == "preserve"
            for port in memory.get("ports", [])
            for name in port.get("read_registers", [])
        }
        self._preserve_read_registers.update(
            name
            for memory in self.payload["memories"]
            if memory.get("read_data_reset") == "preserve"
            for name in memory.get("scheduled_read_registers", [])
        )

    def lower(self) -> list[dict[str, Any]]:
        programs: list[dict[str, Any]] = []
        fifos = FifoEdgeLowerer(
            self.payload,
            self.builder,
            self.scheduled_storage_actions,
        )
        finalizer = EdgeProgramFinalizer(
            self.payload,
            self.builder,
            self._register_by_name,
            self._preserve_read_registers,
        )
        for domain in self.payload["domains"]:
            clock = domain["clock"]
            next_values = self.next_by_domain.setdefault(clock, {})
            effective_reset = self._effective_reset(domain)
            effects: list[dict[str, Any]] = []
            invalid = self._lower_memories(
                clock,
                next_values,
                effective_reset,
                effects,
            )
            invalid = fifos.lower(
                clock,
                next_values,
                effective_reset,
                effects,
                invalid,
            )
            finalizer.commit_registers(
                next_values,
                effective_reset,
                effects,
            )
            programs.append({
                "clock": clock,
                "effects": effects,
                "error": invalid,
                "probes": finalizer.instrumentation_probes(
                    clock,
                    effective_reset,
                ),
            })
        return programs

    def _effective_reset(self, domain: dict[str, Any]) -> int | None:
        reset = None
        if domain["reset"] is not None:
            reset = self.builder.emit(
                "load_input",
                1,
                attributes={"name": f"$reset:{domain['reset']}"},
            )
        if domain["reset_release_mode"] != "synchronized":
            return reset
        release = self.builder.truthy(self.builder.emit(
            "load_state",
            32,
            attributes={"name": f"$release:{domain['clock']}"},
        ))
        return (
            release
            if reset is None
            else self.builder.binary("or", reset, release, 1)
        )

    def _lower_memories(
        self,
        clock: str,
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
    ) -> int:
        invalid = self.builder.constant(0, 1)
        for semantic_memory in self.payload["memories"]:
            active_port_domains = {
                port["domain"] for port in semantic_memory.get("ports", [])
            }
            if (
                semantic_memory["domain"] != clock
                and clock not in active_port_domains
            ):
                continue
            if semantic_memory.get("managed_by") == "fifo":
                continue
            memory = self._memory_by_name[semantic_memory["name"]]
            if semantic_memory.get("managed_by") == "scheduled_memory":
                invalid = self._lower_scheduled_memory(
                    semantic_memory,
                    memory,
                    clock,
                    next_values,
                    effective_reset,
                    effects,
                    invalid,
                )
            else:
                invalid = self._lower_ported_memory(
                    semantic_memory,
                    memory,
                    clock,
                    active_port_domains,
                    next_values,
                    effective_reset,
                    effects,
                    invalid,
                )
        return invalid

    def _lower_scheduled_memory(
        self,
        semantic_memory: dict[str, Any],
        memory: dict[str, Any],
        clock: str,
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
        invalid: int,
    ) -> int:
        builder = self.builder
        actions = [
            action
            for action in self.scheduled_storage_actions
            if action["domain"] == clock
            and action["target"] == semantic_memory["name"]
        ]
        address_width = max(
            (
                builder.width(builder.lower(action["operands"][0]))
                for action in actions
            ),
            default=max(1, (int(memory["depth"]) - 1).bit_length()),
        )
        zero_address = builder.constant(0, address_width)
        read_fire = builder.constant(0, 1)
        read_address = zero_address
        write_fire = builder.constant(0, 1)
        write_address = zero_address
        write_data = builder.constant(0, memory["width"])
        write_mask = None
        for action in actions:
            commit = int(action["commit"])
            operands = action["operands"]
            if action["kind"] == "memory_read_request":
                read_fire = builder.binary("or", read_fire, commit, 1)
                read_address = builder.select(
                    commit,
                    builder.resize(builder.lower(operands[0]), address_width),
                    read_address,
                    address_width,
                )
            elif action["kind"] == "memory_write":
                write_fire = builder.binary("or", write_fire, commit, 1)
                write_address = builder.select(
                    commit,
                    builder.resize(builder.lower(operands[0]), address_width),
                    write_address,
                    address_width,
                )
                write_data = builder.select(
                    commit,
                    builder.lower(operands[1]),
                    write_data,
                    memory["width"],
                )
                if len(operands) == 3:
                    candidate_mask = builder.lower(operands[2])
                    if write_mask is None:
                        write_mask = builder.constant(
                            0, builder.width(candidate_mask)
                        )
                    write_mask = builder.select(
                        commit,
                        candidate_mask,
                        write_mask,
                        builder.width(candidate_mask),
                    )
        if effective_reset is not None:
            read_fire = builder.binary(
                "and",
                read_fire,
                builder.unary("not", effective_reset, 1),
                1,
            )
        comparison_width = address_width + 1
        depth = builder.constant(memory["depth"], comparison_width)
        read_valid = builder.binary(
            "ult", builder.resize(read_address, comparison_width), depth, 1
        )
        write_valid = builder.binary(
            "ult", builder.resize(write_address, comparison_width), depth, 1
        )
        safe_read = builder.select(
            read_valid, read_address, zero_address, address_width
        )
        safe_write = builder.select(
            write_valid, write_address, zero_address, address_width
        )
        old_read = builder.emit(
            "load_memory",
            memory["width"],
            (safe_read,),
            {"memory": memory["name"]},
        )
        old_write = builder.emit(
            "load_memory",
            memory["width"],
            (safe_write,),
            {"memory": memory["name"]},
        )
        merged_write = builder.merge_masked_value(
            old_write,
            write_data,
            write_mask,
            width=memory["width"],
            mask_width=semantic_memory["write_mask_width"],
        )
        collision = builder.binary(
            "and",
            write_fire,
            builder.binary("eq", read_address, write_address, 1),
            1,
        )
        captured = (
            builder.select(collision, merged_write, old_read, memory["width"])
            if semantic_memory["collision"] == "write_first"
            else old_read
        )
        read_names = semantic_memory["scheduled_read_registers"]
        if not read_names:
            raise PrimitiveLoweringError(
                f"scheduled memory '{semantic_memory['name']}' has no read state"
            )
        old_reads = [next_values[name] for name in read_names]
        next_values[read_names[0]] = builder.select(
            read_fire, captured, old_reads[0], memory["width"]
        )
        for index in range(1, len(read_names)):
            next_values[read_names[index]] = builder.select(
                read_fire,
                old_reads[index - 1],
                old_reads[index],
                memory["width"],
            )
        store_enable = builder.binary("and", write_fire, write_valid, 1)
        if effective_reset is not None:
            store_enable = builder.binary(
                "and",
                store_enable,
                builder.unary("not", effective_reset, 1),
                1,
            )
        effects.append({
            "op": "store_memory",
            "memory": memory["name"],
            "address": safe_write,
            "node": merged_write,
            "enable": store_enable,
        })
        self._append_memory_reset(
            semantic_memory,
            memory,
            clock,
            effective_reset,
            effects,
            owner_only=False,
        )
        invalid_read = builder.binary(
            "and", read_fire, builder.unary("not", read_valid, 1), 1
        )
        invalid_write = builder.binary(
            "and", write_fire, builder.unary("not", write_valid, 1), 1
        )
        return builder.binary(
            "or",
            invalid,
            builder.binary("or", invalid_read, invalid_write, 1),
            1,
        )

    def _lower_ported_memory(
        self,
        semantic_memory: dict[str, Any],
        memory: dict[str, Any],
        clock: str,
        active_port_domains: set[str | None],
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
        invalid: int,
    ) -> int:
        builder = self.builder
        memory_ports = semantic_memory["ports"]
        port_by_name = {item["name"]: item for item in memory_ports}
        writer_names = semantic_memory["write_priority"] or [
            item["name"]
            for item in memory_ports
            if item["write_enable"] is not None
        ]
        writers: list[dict[str, int | str | None]] = []
        claimed: list[tuple[int, int]] = []
        for writer_name in writer_names:
            port = port_by_name[writer_name]
            local_writer = port["domain"] == clock
            remote_collision_writer = (
                bool(semantic_memory.get("async_memory"))
                and semantic_memory["collision"] != "read_first"
                and clock in active_port_domains
            )
            if not local_writer and not remote_collision_writer:
                continue
            write_address = builder.lower(port["write_address"])
            address_width = builder.width(write_address)
            comparison_width = address_width + 1
            depth = builder.constant(memory["depth"], comparison_width)
            valid_address = builder.binary(
                "ult",
                builder.resize(write_address, comparison_width),
                depth,
                1,
            )
            safe_address = builder.select(
                valid_address,
                write_address,
                builder.constant(0, address_width),
                address_width,
            )
            requested = builder.truthy(builder.lower(port["write_enable"]))
            if not local_writer:
                requested = builder.binary(
                    "and", requested, builder.event(port["domain"]), 1
                )
                writer_reset = semantic_memory.get("reset")
                if writer_reset is not None:
                    reset_active = builder.emit(
                        "load_input",
                        1,
                        attributes={"name": f"$reset:{writer_reset}"},
                    )
                    requested = builder.binary(
                        "and",
                        requested,
                        builder.unary("not", reset_active, 1),
                        1,
                    )
            active = self._unclaimed_writer(
                write_address,
                requested,
                claimed,
            )
            old = builder.emit(
                "load_memory",
                memory["width"],
                (safe_address,),
                {"memory": memory["name"]},
            )
            value = builder.merge_masked_value(
                old,
                builder.lower(port["write_data"]),
                (
                    builder.lower(port["write_mask"])
                    if port["write_mask"] is not None
                    else None
                ),
                width=memory["width"],
                mask_width=semantic_memory["write_mask_width"],
            )
            store_enable = builder.binary("and", active, valid_address, 1)
            if effective_reset is not None:
                store_enable = builder.binary(
                    "and",
                    store_enable,
                    builder.unary("not", effective_reset, 1),
                    1,
                )
            if local_writer:
                effects.append({
                    "op": "store_memory",
                    "memory": memory["name"],
                    "address": safe_address,
                    "node": value,
                    "enable": store_enable,
                })
            writers.append({
                "address": write_address,
                "active": active,
                "value": value,
            })
            claimed.append((write_address, active))
            invalid = builder.binary(
                "or",
                invalid,
                builder.binary(
                    "and",
                    requested,
                    builder.unary("not", valid_address, 1),
                    1,
                ),
                1,
            )

        for port in memory_ports:
            if port["domain"] != clock or not port["read_registers"]:
                continue
            invalid = self._lower_memory_read(
                semantic_memory,
                memory,
                port,
                writers,
                next_values,
                effective_reset,
                invalid,
            )
        self._append_memory_reset(
            semantic_memory,
            memory,
            clock,
            effective_reset,
            effects,
        )
        return invalid

    def _unclaimed_writer(
        self,
        address: int,
        requested: int,
        claimed: list[tuple[int, int]],
    ) -> int:
        if not claimed:
            return requested
        unclaimed = self.builder.constant(1, 1)
        for higher_address, higher_active in claimed:
            same = self.builder.binary("eq", address, higher_address, 1)
            unclaimed = self.builder.binary(
                "and",
                unclaimed,
                self.builder.unary(
                    "not",
                    self.builder.binary("and", higher_active, same, 1),
                    1,
                ),
                1,
            )
        return self.builder.binary("and", requested, unclaimed, 1)

    def _lower_memory_read(
        self,
        semantic_memory: dict[str, Any],
        memory: dict[str, Any],
        port: dict[str, Any],
        writers: list[dict[str, int | str | None]],
        next_values: dict[str, int],
        effective_reset: int | None,
        invalid: int,
    ) -> int:
        builder = self.builder
        reads = port["read_registers"]
        read_address = builder.lower(port["address"])
        address_width = builder.width(read_address)
        comparison_width = address_width + 1
        depth = builder.constant(memory["depth"], comparison_width)
        valid_address = builder.binary(
            "ult", builder.resize(read_address, comparison_width), depth, 1
        )
        safe_address = builder.select(
            valid_address,
            read_address,
            builder.constant(0, address_width),
            address_width,
        )
        old = builder.emit(
            "load_memory",
            memory["width"],
            (safe_address,),
            {"memory": memory["name"]},
        )
        read_enable = (
            builder.constant(1, 1)
            if port["read_enable"] is None
            else builder.truthy(builder.lower(port["read_enable"]))
        )
        if effective_reset is not None:
            read_enable = builder.binary(
                "and",
                read_enable,
                builder.unary("not", effective_reset, 1),
                1,
            )
        collision = None
        write_first_value = old
        for writer in reversed(writers):
            same = builder.binary(
                "eq", read_address, int(writer["address"]), 1
            )
            hit = builder.binary("and", int(writer["active"]), same, 1)
            collision = (
                hit
                if collision is None
                else builder.binary("or", collision, hit, 1)
            )
            write_first_value = builder.select(
                hit,
                int(writer["value"]),
                write_first_value,
                memory["width"],
            )
        current_read = next_values[reads[0]]
        collision_value = {
            "read_first": old,
            "write_first": write_first_value,
            "no_change": current_read,
        }[semantic_memory["collision"]]
        sampled = (
            old
            if collision is None
            else builder.select(
                collision, collision_value, old, memory["width"]
            )
        )
        if port["read_enable"] is not None or effective_reset is not None:
            sampled = builder.select(
                read_enable, sampled, current_read, memory["width"]
            )
        old_reads = [next_values[name] for name in reads]
        next_values[reads[0]] = sampled
        for index in range(1, len(reads)):
            next_values[reads[index]] = old_reads[index - 1]
        return builder.binary(
            "or",
            invalid,
            builder.binary(
                "and",
                read_enable,
                builder.unary("not", valid_address, 1),
                1,
            ),
            1,
        )

    def _append_memory_reset(
        self,
        semantic_memory: dict[str, Any],
        memory: dict[str, Any],
        clock: str,
        effective_reset: int | None,
        effects: list[dict[str, Any]],
        *,
        owner_only: bool = True,
    ) -> None:
        if (
            semantic_memory["contents_reset"] != "clear"
            or (owner_only and semantic_memory["domain"] != clock)
            or effective_reset is None
        ):
            return
        initial = sum(
            int(limb) << (64 * index)
            for index, limb in enumerate(memory["initial_limbs"])
        )
        effects.append({
            "op": "fill_memory",
            "memory": memory["name"],
            "node": self.builder.constant(initial, memory["width"]),
            "enable": effective_reset,
        })


@dataclass
class FifoEdgeLowerer:
    """Own FIFO pointer/count transitions and runtime error checks."""

    payload: dict[str, Any]
    builder: PrimitiveBuilder
    scheduled_storage_actions: list[dict[str, Any]]

    def lower(
        self,
        clock: str,
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
        invalid: int,
    ) -> int:
        for fifo in self.payload.get("fifos", []):
            if fifo["domain"] != clock:
                continue
            invalid = self._lower_fifo(
                fifo,
                clock,
                next_values,
                effective_reset,
                effects,
                invalid,
            )
        return invalid

    def _lower_fifo(
        self,
        fifo: dict[str, Any],
        clock: str,
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
        invalid: int,
    ) -> int:
        builder = self.builder
        count = next_values[fifo["count"]]
        read_pointer = next_values[fifo["read_pointer"]]
        write_pointer = next_values[fifo["write_pointer"]]
        empty = builder.binary(
            "eq", count, builder.constant(0, builder.width(count)), 1
        )
        full = builder.binary(
            "eq",
            count,
            builder.constant(int(fifo["depth"]), builder.width(count)),
            1,
        )
        not_reset = (
            builder.constant(1, 1)
            if effective_reset is None
            else builder.unary("not", effective_reset, 1)
        )
        if fifo["scheduled"]:
            pop, push, push_data = self._scheduled_fifo_requests(
                fifo,
                clock,
            )
            pop = builder.binary("and", not_reset, pop, 1)
            push = builder.binary("and", not_reset, push, 1)
            pop_request = pop
            push_request = push
        else:
            pop_request = builder.truthy(builder.lower(fifo["pop"]))
            push_request = builder.truthy(builder.lower(fifo["push"]))
            pop = builder.binary(
                "and",
                not_reset,
                builder.binary(
                    "and", pop_request, builder.unary("not", empty, 1), 1
                ),
                1,
            )
            push = builder.binary(
                "and",
                not_reset,
                builder.binary(
                    "and",
                    push_request,
                    builder.binary(
                        "or", builder.unary("not", full, 1), pop, 1
                    ),
                    1,
                ),
                1,
            )
            push_data = builder.lower(fifo["data"])
        effects.append({
            "op": "store_memory",
            "memory": fifo["memory"],
            "address": write_pointer,
            "node": push_data,
            "enable": push,
        })

        one_count = builder.constant(1, builder.width(count))
        incremented_count = builder.binary(
            "add", count, one_count, builder.width(count)
        )
        decremented_count = builder.binary(
            "sub", count, one_count, builder.width(count)
        )
        next_values[fifo["count"]] = builder.select(
            builder.binary(
                "and", push, builder.unary("not", pop, 1), 1
            ),
            incremented_count,
            builder.select(
                builder.binary(
                    "and", pop, builder.unary("not", push, 1), 1
                ),
                decremented_count,
                count,
                builder.width(count),
            ),
            builder.width(count),
        )
        next_values[fifo["read_pointer"]] = builder.select(
            pop,
            self._advance_pointer(read_pointer, int(fifo["depth"])),
            read_pointer,
            builder.width(read_pointer),
        )
        next_values[fifo["write_pointer"]] = builder.select(
            push,
            self._advance_pointer(write_pointer, int(fifo["depth"])),
            write_pointer,
            builder.width(write_pointer),
        )
        if fifo["scheduled"]:
            return invalid
        overflow = builder.binary(
            "and",
            not_reset,
            builder.binary(
                "and",
                push_request,
                builder.binary(
                    "and", full, builder.unary("not", pop, 1), 1
                ),
                1,
            ),
            1,
        )
        underflow = builder.binary(
            "and",
            not_reset,
            builder.binary("and", pop_request, empty, 1),
            1,
        )
        return builder.binary(
            "or",
            invalid,
            builder.binary("or", overflow, underflow, 1),
            1,
        )

    def _scheduled_fifo_requests(
        self,
        fifo: dict[str, Any],
        clock: str,
    ) -> tuple[int, int, int]:
        builder = self.builder
        matching = [
            action
            for action in self.scheduled_storage_actions
            if action["domain"] == clock and action["target"] == fifo["name"]
        ]
        pop = builder.constant(0, 1)
        push = builder.constant(0, 1)
        push_data = builder.constant(0, int(fifo["type"]["width"]))
        for action in matching:
            commit = int(action["commit"])
            if action["kind"] == "fifo_pop":
                pop = builder.binary("or", pop, commit, 1)
            elif action["kind"] == "fifo_push":
                push = builder.binary("or", push, commit, 1)
                push_data = builder.select(
                    commit,
                    builder.lower(action["node"]),
                    push_data,
                    int(fifo["type"]["width"]),
                )
        return pop, push, push_data

    def _advance_pointer(self, pointer: int, depth: int) -> int:
        builder = self.builder
        width = builder.width(pointer)
        at_last = builder.binary(
            "eq", pointer, builder.constant(depth - 1, width), 1
        )
        incremented = builder.binary(
            "add", pointer, builder.constant(1, width), width
        )
        return builder.select(
            at_last,
            builder.constant(0, width),
            incremented,
            width,
        )


@dataclass
class EdgeProgramFinalizer:
    """Own register commits and compiler-published verification probes."""

    payload: dict[str, Any]
    builder: PrimitiveBuilder
    _register_by_name: dict[str, dict[str, Any]]
    _preserve_read_registers: set[str]

    def commit_registers(
        self,
        next_values: dict[str, int],
        effective_reset: int | None,
        effects: list[dict[str, Any]],
    ) -> None:
        builder = self.builder
        for name, value in sorted(next_values.items()):
            register = self._register_by_name[name]
            if (
                effective_reset is not None
                and register["resettable"]
                and name not in self._preserve_read_registers
            ):
                initial = sum(
                    int(limb) << (64 * index)
                    for index, limb in enumerate(register["initial_limbs"])
                )
                value = builder.select(
                    effective_reset,
                    builder.constant(initial, register["width"]),
                    value,
                    register["width"],
                )
            effects.append({
                "op": "commit_state",
                "target": name,
                "node": value,
            })

    def instrumentation_probes(
        self,
        clock: str,
        effective_reset: int | None,
    ) -> list[dict[str, Any]]:
        builder = self.builder
        probes: list[dict[str, Any]] = []
        not_reset = (
            builder.constant(1, 1)
            if effective_reset is None
            else builder.unary("not", effective_reset, 1)
        )
        for scope in self.payload.get("instrumentation_scopes", []):
            if scope["clock"] != clock:
                continue
            requirements_hold = builder.constant(1, 1)
            for requirement in scope["requirements"]:
                holds = builder.truthy(builder.lower(requirement["node"]))
                probes.append({
                    "kind": "cover",
                    "event": requirement["event"],
                    "condition": builder.binary(
                        "and", not_reset, builder.unary("not", holds, 1), 1
                    ),
                    "once": False,
                })
                requirements_hold = builder.binary(
                    "and", requirements_hold, holds, 1
                )
            active = builder.binary("and", not_reset, requirements_hold, 1)
            for goal in scope["goals"]:
                holds = builder.truthy(builder.lower(goal["node"]))
                if goal["kind"] == "cover":
                    condition = builder.binary("and", active, holds, 1)
                    kind = "cover"
                    once = True
                else:
                    condition = builder.binary(
                        "or", builder.unary("not", active, 1), holds, 1
                    )
                    kind = "check"
                    once = False
                probes.append({
                    "kind": kind,
                    "event": goal["event"],
                    "condition": condition,
                    "once": once,
                })
        return probes


__all__ = [
    "EdgeProgramFinalizer",
    "EdgeProgramLowerer",
    "FifoEdgeLowerer",
]
