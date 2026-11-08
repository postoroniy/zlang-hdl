#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute the same five portable designs on every supported board profile."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from zlang.fpga_boards import board_names
from zlang.fpga_build import build_fpga


WITNESSES = {'counter': 'FpgaCounter', 'gpio': 'FpgaGpio',
             'fixed_dsp': 'FpgaFixedDsp', 'memory': 'FpgaMemory',
             'hierarchy': 'FpgaHierarchy'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain-manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        parser.error('qualification output must be fresh')
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    rows = []
    failed = False
    for board in board_names():
        for name, top in WITNESSES.items():
            try:
                receipt = build_fpga(root / 'examples' / 'fpga' / f'{name}.zhl',
                                     board=board, toolchain_manifest=args.toolchain_manifest,
                                     output=args.output / board / name, top=top)
                row = {'board': board, 'witness': name, 'state': receipt['state'],
                       'build_identity': receipt['build_identity'],
                       'synthesis_cells': receipt['synthesis_cells'],
                       'routing': receipt['routing'],
                       'receipt': f'{board}/{name}/receipt.json'}
            except Exception as error:
                failed = True
                row = {'board': board, 'witness': name, 'state': 'failed',
                       'error': f'{type(error).__name__}: {error}',
                       'receipt': f'{board}/{name}/receipt.json'}
            rows.append(row)
            print(board, name, row['state'], flush=True)
            (args.output / 'summary.json').write_text(json.dumps({
                'schema': 'zlang-fpga-qualification-v1', 'results': rows,
                'complete': len(rows) == len(board_names()) * len(WITNESSES),
                'passed': not failed, 'hardware': 'UNKNOWN', 'vendor_gowin_eda': 'UNKNOWN',
            }, indent=2, sort_keys=True) + '\n')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
