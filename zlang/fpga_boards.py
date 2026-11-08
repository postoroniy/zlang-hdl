"""Revision-specific board facts and compiler-ABI-derived pin constraints."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re

from zlang.backend.manifest import BackendArtifact
from zlang.common import stable_digest
from zlang.ir.equivalence import BindingSide
from zlang.platform_constraints import (
    ConstraintFormat, PlatformClockConstraint, build_constraint_artifact,
)


class FpgaBoardError(ValueError):
    """A board revision, pin or physical interface is unsupported."""


@dataclass(frozen=True)
class BoardDefinition:
    """One immutable manufacturer board record; no inferred revision aliases."""

    name: str
    record_json: str

    @property
    def record(self) -> dict:
        return json.loads(self.record_json)

    @property
    def identity(self) -> str:
        return stable_digest({'schema': 'zlang-fpga-board-v1',
                              'name': self.name, 'record': self.record})


def board_names() -> tuple[str, ...]:
    return tuple(sorted(_records()))


def _records() -> dict:
    data = json.loads((Path(__file__).parent / 'data' / 'fpga-boards.json').read_text())
    if data['schema'] != 'zlang-fpga-boards-v1':
        raise FpgaBoardError('unsupported board inventory schema')
    return data['boards']


def load_board(name: str) -> BoardDefinition:
    try:
        record = _records()[name]
    except KeyError as error:
        raise FpgaBoardError(f'unknown board/revision {name!r}; choose {board_names()}') from error
    return BoardDefinition(name, json.dumps(record, sort_keys=True))


def board_constraints(module, artifact: BackendArtifact, board: BoardDefinition,
                      assignments: dict[str, str] | None = None) -> dict:
    """Map each actual physical ABI bit to an explicitly reviewed board net.

    Assignment keys are compiler member paths, optionally with a vector index;
    values select board nets, never arbitrary pad numbers or Tcl text.
    """
    record = board.record
    domain = module.clock_domains
    if len(domain) != 1 or domain[0].clock != record['clock'] or domain[0].reset != record['reset']:
        raise FpgaBoardError('FPGA v1 requires the board clock/reset domain')
    if (domain[0].reset_polarity.value, domain[0].reset_mode.value) != (
        record['reset_polarity'], record['reset_mode'],
    ):
        raise FpgaBoardError('board reset polarity/mode does not match typed contract')
    bindings = [b for b in artifact.bindings if b.side is BindingSide.IMPLEMENTATION]
    bit_ports = {}
    for binding in bindings:
        logical = '.'.join(binding.member_path)
        if not logical or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*', logical):
            raise FpgaBoardError('unsupported physical member path')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', binding.rtl_path):
            raise FpgaBoardError('unsupported physical port identifier')
        if not binding.physical_available or binding.rtl_module != artifact.module:
            raise FpgaBoardError('missing top-level physical binding')
        for i in range(binding.width):
            suffix = f'[{i}]' if binding.width != 1 else ''
            key = logical + suffix
            if key in bit_ports:
                raise FpgaBoardError('duplicate physical bit binding')
            bit_ports[key] = (binding.rtl_path + suffix,
                              'output' if binding.role.value == 'output' else 'input')
    mapping = dict(assignments) if assignments is not None else {k: k for k in bit_ports}
    if set(mapping) != set(bit_ports):
        raise FpgaBoardError('pin assignments must cover exactly every physical ABI bit')
    if len(set(mapping.values())) != len(mapping):
        raise FpgaBoardError('board net assigned to multiple physical bits')
    lines, pins, pads = [], [], set()
    for key, net in sorted(mapping.items()):
        if net not in record['pins']:
            raise FpgaBoardError(f'unverified board net {net!r}')
        pin = record['pins'][net]
        rtl, direction = bit_ports[key]
        if pin['direction'] != direction or pin['io_standard'] != 'LVCMOS33':
            raise FpgaBoardError('pin direction/IO voltage mismatch')
        pad = pin['pad']
        if not re.fullmatch(r'[A-Z]*[0-9]+', pad) or pad in pads:
            raise FpgaBoardError('invalid or duplicate package pad')
        pads.add(pad)
        if record['family'] == 'gowin':
            lines.extend([f'IO_LOC "{rtl}" {pad};',
                          f'IO_PORT "{rtl}" IO_TYPE=LVCMOS33' +
                          (' PULL_MODE=UP' if pin['pull_up'] else '') + ';'])
        elif record['family'] == 'ecp5':
            lines.extend([f'LOCATE COMP "{rtl}" SITE "{pad}";',
                          f'IOBUF PORT "{rtl}" IO_TYPE=LVCMOS33' +
                          (' PULLMODE=UP' if pin['pull_up'] else '') + ';'])
        else:
            raise FpgaBoardError('unsupported board FPGA family')
        pins.append({'member': key, 'rtl': rtl, 'board_net': net, **pin})
    clock = build_constraint_artifact(module, artifact, PlatformClockConstraint(
        record['clock'], Decimal(1000) / Decimal(record['clock_mhz']), board.name,
    ), ConstraintFormat.SDC)
    text = '\n'.join(lines) + '\n'
    return {'board_identity': board.identity, 'pins': pins, 'pin_text': text,
            'pin_format': 'cst' if record['family'] == 'gowin' else 'lpf',
            'clock_text': clock.text, 'clock_identity': clock.identity,
            'identity': stable_digest({'board': board.identity, 'pins': pins,
                                       'clock': clock.identity, 'text': text})}
