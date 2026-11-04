# SPDX-License-Identifier: Apache-2.0
"""Compiler-owned, plan-bound simulation I/O recipe for native editor hosts."""

import hashlib
import json
from pathlib import Path

from zlang.ir.type_codec import canonical_type_data
from zlang.simulation_io_channels import packed_records, snapshot_input
from zlang.simulation_io_model import (
    IODrive,
    IOFinish,
    IORead,
    IOWrite,
    IOWhen,
    phase_masks,
)
from zlang.simulation_io_product import build_io_product
from zlang.simulation_plan_build import build_simulation_plan
from zlang.simulation_plan_policy import MAX_PLAN_BYTES
from zlang.simulation_values import SimulationRuntimeError
from zlang.simulation_event_runner import MAX_EXPANDED_EVENTS
from zlang.common import stable_digest


def command_data(product):
    """Exact typed command bindings; no source paths or object identities."""
    probes = {id(expression): probe for expression, probe in product.probes}
    locations = {id(expression): location for expression, location in product.locations}
    read_site = 0
    phases = phase_masks(product.block.commands)

    def target(expression):
        value = locations[id(expression)]
        return {
            "name": value.name,
            "offset": value.lsb,
            "width": value.width,
            "container_width": value.container_type.width,
        }

    def commands(items):
        nonlocal read_site
        result = []
        for item in items:
            if isinstance(item, IOWhen):
                result.append(
                    {
                        "op": "when",
                        "guard": probes[id(item.guard)],
                        "phases": phases[id(item)],
                        "body": commands(item.body),
                    }
                )
            elif isinstance(item, IORead):
                site = read_site
                read_site += 1
                result.append(
                    {
                        "op": "read",
                        "site": site,
                        "targets": [target(t) for _, t in item.bindings],
                        "eof": commands(item.eof),
                    }
                )
            elif isinstance(item, IOWrite):
                result.append(
                    {
                        "op": "write",
                        "channel": item.channel,
                        "fields": [
                            {"name": key, "probe": probes[id(expression)]}
                            for key, expression in item.bindings
                        ],
                    }
                )
            elif isinstance(item, IODrive):
                result.append(
                    {
                        "op": "drive",
                        "target": target(item.target),
                        "probe": probes[id(item.value)],
                    }
                )
            elif isinstance(item, IOFinish):
                result.append({"op": "finish"})
        return result

    return commands(product.block.commands)


def recipe_data(product, plan):
    data = {
        "schema": "zlang-simulation-io-recipe-v1",
        "block": product.block.name,
        "clock": product.block.clock,
        "phase": "pre_edge",
        "execution_identity": plan.execution_identity,
        "commands": command_data(product),
        "resets": dict(product.block.reset_bindings),
        "inputs": {
            key: {name: canonical_type_data(value.type) for name, value in bindings}
            for key, bindings in product.inputs
        },
        "outputs": {
            key: {name: canonical_type_data(value.type) for name, value in bindings}
            for key, bindings in product.outputs
        },
    }
    return {"identity": stable_digest(data), **data}


def prepare_debug_io(program, name, inputs, outputs):
    """Freeze typed vectors; the adapter never parses ZLang or JSON value types."""
    from dataclasses import replace
    from zlang import sim

    product = build_io_product(
        program.module, program.simulation_blocks, name, program.simulation_locals
    )
    if set(inputs) != {key for key, _ in product.inputs} or set(outputs) != {
        key for key, _ in product.outputs
    }:
        raise SimulationRuntimeError(
            "simulation channel bindings must exactly match the selected block"
        )
    plan = build_simulation_plan(product.module)
    active = replace(program, plan=plan, _native=sim._compiled_native_program(plan))
    records = []
    sources = {}
    packed_bytes = 0
    for channel, bindings in product.inputs:
        path = Path(inputs[channel])
        text, digest = snapshot_input(path)
        for row in packed_records(text, path, bindings):
            packed_bytes += sum(value.width + 3 for value in row) + 3
            if packed_bytes > MAX_PLAN_BYTES or len(records) >= MAX_EXPANDED_EVENTS:
                raise SimulationRuntimeError(
                    "prepared simulation I/O exceeds native byte/event limit"
                )
            records.append([value.to_bits() for value in row])
        sources[channel] = {
            "sha256": digest,
            "snapshot": "launch",
            "fields": {key: canonical_type_data(e.type) for key, e in bindings},
        }
    spec = {
        "schema": "zlang-simulation-io-program-v1",
        "plan_identity": plan.identity,
        "block": name,
        "clock": product.block.clock,
        "commands": command_data(product),
        "resets": dict(product.block.reset_bindings),
        "records": records,
        "input_sources": sources,
        "recipe": recipe_data(product, plan),
        "outputs": {
            channel: {
                "path": str(outputs[channel]),
                "fields": {key: canonical_type_data(e.type) for key, e in bindings},
            }
            for channel, bindings in product.outputs
        },
        "probes": [probe for _, probe in product.probes],
    }
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode()) > MAX_PLAN_BYTES:
        raise SimulationRuntimeError(
            "prepared simulation I/O exceeds native plan byte limit"
        )
    return active, {
        "json": encoded,
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }
