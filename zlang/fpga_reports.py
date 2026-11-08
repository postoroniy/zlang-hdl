"""Strict adapters for executed Yosys and nextpnr measurements."""
from __future__ import annotations

import json
import math
from pathlib import Path


class FpgaReportError(ValueError):
    """A required physical measurement is missing or inconsistent."""


def synthesis_resources(path: Path, top: str) -> dict[str, int]:
    data = json.loads(path.read_text())
    if top not in data['modules']:
        raise FpgaReportError('synthesis report has no exact top module')
    modules = data['modules']
    counts: dict[str, int] = {}

    def walk(name, active):
        if name in active:
            raise FpgaReportError('recursive synthesized module hierarchy')
        for cell in modules[name]['cells'].values():
            kind = cell['type']
            if kind == '$scopeinfo':
                if cell['connections'] or cell.get('port_directions'):
                    raise FpgaReportError('invalid synthesized scope metadata')
                continue
            model = modules.get(kind, {})
            opaque = any(int(str(model.get('attributes', {}).get(k, '0')), 2)
                         for k in ('blackbox', 'whitebox'))
            if kind in modules and not opaque:
                walk(kind, active | {name})
            else:
                # Yosys 0.70 retains typed wiring aliases; these have no
                # independent physical cell and are not counted as LUTs.
                if kind == '$buf':
                    ports = cell['connections']
                    if set(ports) != {'A', 'Y'} or len(ports['A']) != len(ports['Y']):
                        raise FpgaReportError('invalid synthesized wiring alias')
                    continue
                if kind.startswith('$'):
                    raise FpgaReportError(f'unmapped synthesis cell {kind!r}')
                counts[kind] = counts.get(kind, 0) + 1
    walk(top, set())
    if not counts:
        raise FpgaReportError('empty synthesized design')
    return dict(sorted(counts.items()))


def routed_measurement(path: Path, frequency_mhz: int) -> dict:
    data = json.loads(path.read_text())
    clocks = data['fmax']
    if len(clocks) != 1:
        raise FpgaReportError('exactly one constrained routed clock required')
    name, value = next(iter(clocks.items()))
    for field in ('achieved', 'constraint'):
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise FpgaReportError('invalid routed clock measurement')
    if not math.isclose(value['constraint'], frequency_mhz, rel_tol=0, abs_tol=1e-6):
        raise FpgaReportError('routed timing constraint differs from board clock')
    if value['achieved'] < value['constraint']:
        raise FpgaReportError('routed design fails requested clock frequency')
    utilization = data['utilization']
    if not utilization:
        raise FpgaReportError('missing routed resource inventory')
    for resource, entry in utilization.items():
        if set(entry) != {'available', 'used'} or any(
            type(entry[k]) is not int or entry[k] < 0 for k in ('used', 'available')
        ) or (entry['used'] > entry['available'] and resource != 'DUMMY_CELL'):
            raise FpgaReportError(f'invalid routed utilization {resource!r}')
    return {'clock': name, 'constraint_mhz': value['constraint'],
            'achieved_mhz': value['achieved'], 'timing_kind': 'routed_tool_model',
            'primitive_utilization': utilization,
            'pseudo_cells': {k: v for k, v in utilization.items() if k == 'DUMMY_CELL'},
            'note': 'Modes may overlap physical sites; do not sum DSP modes. '
                    'DUMMY_CELL is nextpnr placement bookkeeping, not physical capacity.'}


def verify_device_inventory(measurement: dict, target, family: str) -> None:
    """Bind anonymous nextpnr reports to the compiler-owned device inventory.

    These are independent physical capacities, not overlapping DSP modes. A
    stale report from another density must not qualify the requested part.
    """
    mapping = {
        'gowin': {'GowinLUT4': 'LUT4', 'GowinFF': 'DFF',
                  'GowinMult18': 'MULT18X18', 'GowinBSRAM': 'BSRAM', 'GowinPLL': 'rPLL'},
        'ecp5': {'Ecp5LUT4': 'TRELLIS_COMB', 'Ecp5FF': 'TRELLIS_FF',
                 'Ecp5Mult18': 'MULT18X18D', 'Ecp5EBR': 'DP16KD', 'Ecp5PLL': 'EHXPLLL'},
    }[family]
    reported = measurement['primitive_utilization']
    capacities = {name.rsplit('.', 1)[-1]: count for name, count in target.inventory}
    for semantic, physical in mapping.items():
        if semantic not in capacities or physical not in reported:
            raise FpgaReportError(f'missing device capacity {physical}')
        if reported[physical]['available'] != capacities[semantic]:
            raise FpgaReportError(f'routed report does not match device capacity {physical}')
