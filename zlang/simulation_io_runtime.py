# SPDX-License-Identifier: Apache-2.0
"""Host I/O orchestration around the existing native snapshot transition."""

from dataclasses import replace

from zlang.simulation_io_channels import SimulationChannels
from zlang.simulation_io_model import (
    IODrive,
    IOFinish,
    IORead,
    IOWrite,
    IOWhen,
    phase_masks,
)
from zlang.simulation_io_product import build_io_product
from zlang.simulation_logic import LogicVector
from zlang.simulation_plan_build import build_simulation_plan
from zlang.simulation_primitives import int_from_limbs, int_to_limbs
from zlang.simulation_values import SimulationRuntimeError
from zlang.simulation_event_runner import MAX_EXPANDED_EVENTS

MAX_IO_EVENTS = MAX_EXPANDED_EVENTS


def create_io_instance(program, name, inputs, outputs, initial_registers=None):
    from zlang import sim

    product = build_io_product(
        program.module, program.simulation_blocks, name, program.simulation_locals
    )
    plan = build_simulation_plan(product.module)
    active = replace(program, plan=plan, _native=sim._compiled_native_program(plan))
    instance = active.create(initial_registers=initial_registers)
    try:
        instance._io = SimulationIOSession(instance, product, inputs, outputs)
        instance._io.manifest["dut_plan_identity"] = program.plan.identity
    except BaseException:
        instance.close()
        raise
    return instance


class SimulationIOSession:
    """Own staged host effects; hardware expressions execute only in native."""

    def __init__(self, instance, product, inputs, outputs):
        self.instance = instance
        self.product = product
        self.probes = {id(expression): name for expression, name in product.probes}
        self.phases = phase_masks(product.block.commands)
        self.locations = {
            id(expression): location for expression, location in product.locations
        }
        self.channels = SimulationChannels(product, inputs, outputs)
        from zlang.simulation_io_encoding import recipe_data

        self.channels.manifest["recipe"] = recipe_data(product, instance.program.plan)
        self.channels.manifest.update(
            plan_identity=instance.program.plan.identity,
            selected_ir_identity=instance.program.selected_ir_identity,
        )
        self.index = 0
        self.finished = False
        self.failure = None
        self.closed = False
        self.last_read = None
        self.read_count = 0
        for _, name in product.block.reset_bindings:
            self._set(name, LogicVector.known(0, 1))

    @property
    def manifest(self):
        return self.channels.manifest

    def _get(self, name, width):
        native = self.instance._native
        if self.instance.program.logic_state:
            value, unknown, actual = native.get_logic(name)
            if actual != width:
                raise SimulationRuntimeError("simulation observation width mismatch")
            return LogicVector(width, int_from_limbs(value), int_from_limbs(unknown))
        return LogicVector.known(int_from_limbs(native.get_limbs(name)), width)

    def _set(self, name, value):
        native = self.instance._native
        if self.instance.program.logic_state:
            native.set_logic(
                name,
                int_to_limbs(value.value, value.width),
                int_to_limbs(value.unknown, value.width),
                value.width,
            )
        else:
            if not value.is_binary:
                raise SimulationRuntimeError("U/X file input requires logic_state=True")
            native.set_limbs(name, int_to_limbs(value.value, value.width))

    def observe(self, expression):
        return self._get(self.probes[id(expression)], expression.type.width)

    def guard(self, expression):
        return bool(self.observe(expression).require_binary(name="simulation guard"))

    def reset(self, name, asserted):
        for reset, mirror in self.product.block.reset_bindings:
            if reset == name:
                self._set(mirror, LogicVector.known(int(asserted), 1))

    def check_external(self, name, value=None):
        from zlang.ir.interfaces import ready_valid_field_name, ReadyValidSignal

        names = {name}
        if isinstance(value, dict):
            names.update(
                ready_valid_field_name(name, ReadyValidSignal(field))
                for field in value
                if field in {s.value for s in ReadyValidSignal}
            )
        if names & {location.name for location in self.locations.values()}:
            raise SimulationRuntimeError(
                f"external write conflicts with simulation-owned input '{name}'"
            )

    def edge(self, clocks, execute):
        if self.closed or self.failure is not None:
            raise SimulationRuntimeError("simulation I/O session is closed or failed")
        if self.finished:
            return self.instance._step_outputs()
        if self.index >= MAX_IO_EVENTS:
            raise SimulationRuntimeError(
                "simulation I/O exceeds the expanded-event limit"
            )
        if self.product.block.clock not in clocks:
            result = execute()
            self.index += 1
            return result
        old = {}
        updates = []
        records = []
        did_read = False
        attempted_read = False
        eof_sites = set()
        self.last_read = None
        self.channels.begin()

        def prepare(items):
            nonlocal did_read, attempted_read
            for item in items:
                if self.finished:
                    break
                if isinstance(item, IOWhen):
                    if self.phases[id(item)] & 1 and self.guard(item.guard):
                        prepare(item.body)
                elif isinstance(item, IORead):
                    if attempted_read:
                        raise SimulationRuntimeError(
                            "only one simulation read may execute per event"
                        )
                    attempted_read = True
                    record = self.channels.read(item.bindings)
                    if record is None:
                        eof_sites.add(id(item))
                        prepare(item.eof)
                    else:
                        did_read = True
                        updates.extend(record)
                elif isinstance(item, IODrive):
                    updates.append((item.target, self.observe(item.value)))
                elif isinstance(item, IOFinish):
                    self.finished = True

        def capture(items):
            for item in items:
                if isinstance(item, IOWhen):
                    if self.phases[id(item)] & 2 and self.guard(item.guard):
                        capture(item.body)
                elif isinstance(item, IOWrite):
                    records.append(
                        (
                            item.channel,
                            {
                                name: {"bits": self.observe(value).to_bits()}
                                for name, value in item.bindings
                            },
                        )
                    )
                elif isinstance(item, IORead) and id(item) in eof_sites:
                    capture(item.eof)

        try:
            self.instance._native.eval()
            prepare(self.product.block.commands)
            if self.finished:
                self.channels.rollback()
                return self.instance._step_outputs()
            merged = {}
            for target, value in updates:
                location = self.locations[id(target)]
                if location.name not in old:
                    old[location.name] = self._get(
                        location.name, location.container_type.width
                    )
                base = merged.get(location.name, old[location.name])
                mask = ((1 << location.width) - 1) << location.lsb
                merged[location.name] = LogicVector(
                    base.width,
                    (base.value & ~mask) | (value.value << location.lsb),
                    (base.unknown & ~mask) | (value.unknown << location.lsb),
                )
            for name, value in merged.items():
                self._set(name, value)
            self.instance._native.eval()
            capture(self.product.block.commands)
            rendered = self.channels.stage(self.index, records)
            result = execute()
            self.channels.commit(rendered)
            if did_read:
                self.last_read = (self.index, self.read_count)
                self.read_count += 1
            self.index += 1
            return result
        except BaseException as error:
            self.failure = error
            self.channels.rollback()
            for name, value in old.items():
                self._set(name, value)
            raise

    def empty_event(self):
        if self.index >= MAX_IO_EVENTS:
            raise SimulationRuntimeError(
                "simulation I/O exceeds the expanded-event limit"
            )
        self.index += 1

    def close(self):
        if not self.closed:
            try:
                self.channels.close(
                    "failed"
                    if self.failure is not None
                    else "finished"
                    if self.finished
                    else "closed",
                    self.failure,
                )
            except BaseException as error:
                if self.failure is None:
                    self.failure = error
                self.manifest.update(
                    status="failed", error=str(self.failure), close_error=str(error)
                )
                raise
            finally:
                self.closed = True
