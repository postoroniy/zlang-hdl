# SPDX-License-Identifier: Apache-2.0
"""Source-preserving callbacks for simulation-only blocks."""

from lark import v_args
from zlang.parser.errors import ParseError
from zlang.source import SourceOrigin

from zlang.simulation_io_model import (
    IODrive,
    IOFinish,
    IORead,
    IOWrite,
    IOWhen,
    SimulationBlock,
)


class SimulationRules:
    @v_args(meta=True)
    def simulation_block(self, meta, items):
        return SimulationBlock(
            str(items[0]), str(items[1]), tuple(items[2:]), self._span(meta)
        )

    @v_args(meta=True)
    def simulation_when(self, meta, items):
        return IOWhen(items[0], tuple(items[1:]), self._span(meta))

    def simulation_read_binding(self, items):
        return str(items[0]), items[1]

    def simulation_write_binding(self, items):
        return str(items[0]), items[1]

    def simulation_bindings(self, items):
        return tuple(items)

    def simulation_eof(self, items):
        return tuple(items)

    @v_args(meta=True)
    def simulation_read(self, meta, items):
        return IORead(str(items[0]), items[1], items[2], self._span(meta))

    @v_args(meta=True)
    def simulation_write(self, meta, items):
        return IOWrite(str(items[0]), tuple(items[1:]), self._span(meta))

    @v_args(meta=True)
    def simulation_drive(self, meta, items):
        return IODrive(items[0], items[1], self._span(meta))

    @v_args(meta=True)
    def simulation_finish(self, meta, items):
        if len(items) != 1 or str(items[0]) != "finish":
            raise ParseError(
                "unknown simulation command; expected finish",
                primary=SourceOrigin(self._span(meta), "simulation command"),
            )
        return IOFinish(self._span(meta))
