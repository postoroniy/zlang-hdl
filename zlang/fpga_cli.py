"""Narrow command adapter for revision-specific external FPGA builds."""
from __future__ import annotations

import argparse
from pathlib import Path

from zlang.fpga_boards import board_names
from zlang.fpga_build import build_fpga


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='zlang fpga', description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--board', required=True, choices=board_names())
    parser.add_argument('--toolchain-manifest', type=Path)
    parser.add_argument('--flow', choices=('oss', 'gowin-eda-project'), default='oss')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--top')
    parser.add_argument('--pin', action='append', default=[], metavar='MEMBER=BOARD_NET')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--architecture')
    parser.add_argument('--architecture-mode', choices=('generic','preferred','required'))
    parser.add_argument('--target-evidence-policy', choices=('estimate_only','measured_preferred','measured_required'))
    parser.add_argument('--self-test-wrapper', action='store_true', help='explicit stimulus/checksum board fixture; resources/timing include the fixture')
    args = parser.parse_args(argv)
    pins = {}
    for item in args.pin:
        if item.count('=') != 1 or item.split('=')[0] in pins:
            parser.error('--pin requires unique MEMBER=BOARD_NET entries')
        key, value = item.split('=')
        pins[key] = value
    if args.flow == 'gowin-eda-project' and any((args.architecture, args.architecture_mode, args.target_evidence_policy, args.self_test_wrapper)):
        parser.error('architecture options currently require the qualified OSS flow')
    if args.flow == 'gowin-eda-project':
        from zlang.fpga_gowin import export_project
        try:
            export_project(args.source, board=args.board, output=args.output,
                           top=args.top, assignments=pins or None)
        except Exception as error:
            parser.exit(1, f'Gowin project failed: {type(error).__name__}: {error}\n')
        print(f'Gowin EDA project prepared: {args.output}; vendor execution UNKNOWN')
        return 0
    if args.toolchain_manifest is None:
        parser.error('OSS execution requires --toolchain-manifest')
    try:
        receipt = build_fpga(args.source, board=args.board,
                             toolchain_manifest=args.toolchain_manifest, output=args.output,
                             top=args.top, assignments=pins or None, seed=args.seed,
                             architecture=args.architecture, architecture_mode=args.architecture_mode,
                             target_evidence_policy=args.target_evidence_policy,
                             self_test_wrapper=args.self_test_wrapper)
    except Exception as error:
        parser.exit(1, f'FPGA build failed: {type(error).__name__}: {error}\n')
    print(f'Packed {receipt["bitstream"]}; hardware UNKNOWN; receipt: {args.output / "receipt.json"}')
    return 0
