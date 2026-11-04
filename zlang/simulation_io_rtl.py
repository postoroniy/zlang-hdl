# SPDX-License-Identifier: Apache-2.0
"""Direct-SV testbench I/O phases, derived from checked commands and top ABI."""

import json
from zlang.backend.identifiers import rtl_identifier
from zlang.simulation_io_abi import input_groups, leaf_slices, sv_leaf
from zlang.simulation_io_model import IODrive, IOFinish, IORead, IOWrite, IOWhen
from zlang.simulation_plan_policy import MAX_PLAN_BYTES


class SimulationIOTestbench:
    """Own emitted harness statements, never DUT modules or inferred semantics."""

    def __init__(self, product, original, record_count):
        self.product = product
        self.original = original
        self.record_count = record_count
        self.output_names = {p.external_name for p in original.top_physical_abi.outputs}
        self.probes = {id(e): rtl_identifier(n) for e, n in product.probes}
        self.locations = {
            id(expression): location for expression, location in product.locations
        }
        self.groups = input_groups(original)
        self.targets = {location.name: location for _, location in product.locations}
        used = {
            rtl_identifier(p.external_name)
            for p in product.module.top_physical_abi.leaves
        }
        self.prefix = "zlang_io_"
        while any(n.startswith(self.prefix) for n in used):
            self.prefix += "_"
        self.stage = {
            n: self.prefix + "stage_" + str(i) for i, n in enumerate(self.targets)
        }
        self.writes = []
        self.reads = []

        def collect(items):
            for item in items:
                if isinstance(item, IOWhen):
                    collect(item.body)
                elif isinstance(item, IOWrite):
                    self.writes.append(item)
                elif isinstance(item, IORead):
                    self.reads.append(item)
                    collect(item.eof)

        collect(product.block.commands)
        self.log_ids = {id(item): n for n, item in enumerate(self.writes)}
        self.read_ids = {id(item): n for n, item in enumerate(self.reads)}
        self.width = sum(
            value.type.width for _, bindings in product.inputs for _, value in bindings
        )

    def declarations(self):
        p = self.prefix
        lines = [
            f"integer {p}cursor=0, {p}next_cursor=0;",
            f"integer {p}bytes=0, {p}next_bytes=0;",
            f"string {p}event_text;",
            f"logic {p}active=0, {p}finish=0, {p}read=0, {p}attempted=0;",
            f"logic [{max(1, self.width) - 1}:0] {p}vectors [0:{max(1, self.record_count) - 1}];",
        ]
        for name, location in self.targets.items():
            lines.append(
                f"logic [{location.container_type.width - 1}:0] {self.stage[name]};"
            )
        lines += [f"logic {p}eof_{n}=0;" for n in range(len(self.reads))]
        for n, item in enumerate(self.writes):
            lines.append(f"logic {p}log_{n}=0;")
            for i, (_, value) in enumerate(item.bindings):
                lines.append(f"logic [{value.type.width - 1}:0] {p}log_{n}_{i};")
        return lines

    def initialize(self):
        return (
            [f'$readmemb("simulation-input.bin", {self.prefix}vectors);']
            if self.record_count
            else []
        )

    def _guard(self, item, body):
        guard = self.probes[id(item.guard)]
        return [
            f"if (({guard} !== 1'b0) && ({guard} !== 1'b1)) $fatal(1,\"unresolved simulation guard\");",
            f"if ({guard}) begin",
            *body,
            "end",
        ]

    def _prepare(self, items):
        p = self.prefix
        result = []
        for item in items:
            lines = []
            if isinstance(item, IOWhen):
                body = self._prepare(item.body)
                if body:
                    lines.extend(self._guard(item, body))
            elif isinstance(item, IORead):
                site = self.read_ids[id(item)]
                lines.extend(
                    [
                        f'if ({p}attempted) $fatal(1,"only one simulation read per event");',
                        f"{p}attempted=1;",
                    ]
                )
                lines.append(f"if ({p}cursor < {self.record_count}) begin")
                offset = 0
                for _, target in item.bindings:
                    loc = self.locations[id(target)]
                    lines.append(
                        f"{self.stage[loc.name]}[{loc.lsb} +: {loc.width}] = "
                        f"{p}vectors[{p}cursor][{offset} +: {loc.width}];"
                    )
                    offset += loc.width
                lines.extend(
                    [
                        f"{p}next_cursor = {p}cursor + 1;",
                        f"{p}read=1;",
                        "end else begin",
                        f"{p}eof_{site}=1;",
                        *self._prepare(item.eof),
                        "end",
                    ]
                )
            elif isinstance(item, IODrive):
                loc = self.locations[id(item.target)]
                lines.append(
                    f"{self.stage[loc.name]}[{loc.lsb} +: {loc.width}] = {self.probes[id(item.value)]};"
                )
            elif isinstance(item, IOFinish):
                lines.append(f"{p}finish=1;")
            if lines:
                result.extend([f"if (!{p}finish) begin", *lines, "end"])
        return result

    def _capture(self, items):
        p = self.prefix
        lines = []
        for item in items:
            if isinstance(item, IOWhen):
                body = self._capture(item.body)
                if body:
                    lines.extend(self._guard(item, body))
            elif isinstance(item, IOWrite):
                n = self.log_ids[id(item)]
                lines.append(f"{p}log_{n}=1;")
                for i, (_, value) in enumerate(item.bindings):
                    lines.append(f"{p}log_{n}_{i}={self.probes[id(value)]};")
            elif isinstance(item, IORead):
                lines += [
                    f"if ({p}eof_{self.read_ids[id(item)]}) begin",
                    *self._capture(item.eof),
                    "end",
                ]
        return lines

    def before_edge(self, data, index, edge_offsets, resets):
        p = self.prefix
        edge = f"{data}[{index}][{edge_offsets[self.product.block.clock]}]"
        lines = [
            f"{p}active={edge};",
            f"{p}finish=0;",
            f"{p}read=0;",
            f"{p}attempted=0;",
        ]
        lines += [f"{p}eof_{n}=0;" for n in range(len(self.reads))]
        lines += [f"{p}log_{n}=0;" for n in range(len(self.writes))]
        for reset, name in self.product.block.reset_bindings:
            external = next(
                leaf.external_name
                for leaf in self.product.module.top_physical_abi.inputs
                if leaf.member_path == (name,)
            )
            lines.append(
                f"{rtl_identifier(external)} = ({rtl_identifier(reset)} == 1'b{resets[reset][0]});"
            )
        lines.extend(["#1;", f"if ({p}active) begin", f"{p}next_cursor={p}cursor;"])
        for name in self.targets:
            for leaf in self.groups[name]:
                offset = 0
                for lsb, width in leaf_slices(leaf):
                    source = rtl_identifier(leaf.external_name)
                    if leaf.width > 1:
                        source += f"[{offset} +: {width}]"
                    lines.append(f"{self.stage[name]}[{lsb} +: {width}] = {source};")
                    offset += width
        lines += self._prepare(self.product.block.commands)
        lines.append(
            f'if ({p}finish) begin $display("io_finish %0d",{index}); $finish; end'
        )
        for name in self.targets:
            for leaf in self.groups[name]:
                lines.append(
                    f"{rtl_identifier(leaf.external_name)} = "
                    f"{sv_leaf(leaf, self.stage[name])};"
                )
        lines += [
            "#1;",
            *self._capture(self.product.block.commands),
            f"{p}next_bytes={p}bytes;",
            f'{p}event_text=$sformatf("%0d",{index});',
        ]
        for n, item in enumerate(self.writes):
            record = {
                "epoch": 0,
                "event": 0,
                "values": {
                    key: {"bits": "0" * value.type.width}
                    for key, value in item.bindings
                },
            }
            base = len(json.dumps(record, sort_keys=True, separators=(",", ":")))
            # The newline adds one byte; replace the single-digit event index.
            lines.append(
                f"if ({p}log_{n}) {p}next_bytes += {base} + {p}event_text.len();"
            )
        lines += [
            f'if ({p}next_bytes > {MAX_PLAN_BYTES}) $fatal(1,"simulation log byte limit");',
            "end",
        ]
        return lines

    def after_edge(self, index):
        p = self.prefix
        lines = [
            f"if ({p}active) begin",
            f'if ({p}read) $display("io_read %0d %0d",{index},{p}cursor);',
            f"{p}cursor={p}next_cursor;",
        ]
        lines.append(f"{p}bytes={p}next_bytes;")
        for name in self.targets:
            for leaf in self.groups[name]:
                offset = 0
                for lsb, width in leaf_slices(leaf):
                    source = rtl_identifier(leaf.external_name)
                    if leaf.width > 1:
                        source += f"[{offset} +: {width}]"
                    lines.append(f"{self.stage[name]}[{lsb} +: {width}] = {source};")
                    offset += width
            lines.append(
                f'$display("io_input %0d {name} %b",{index},{self.stage[name]});'
            )
        for n, item in enumerate(self.writes):
            fmt = " ".join("%b" for _ in item.bindings)
            args = ",".join(f"{p}log_{n}_{i}" for i in range(len(item.bindings)))
            lines.append(
                f'if ({p}log_{n}) $display("io_record %0d {item.channel} {fmt}",{index},{args});'
            )
        return [*lines, "end"]
