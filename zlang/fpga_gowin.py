"""Gowin EDA project export using normal production RTL and board constraints.

Export is not evidence of vendor synthesis/route execution. The Tcl contract
is documented in Gowin SUG1220-2.1E; no proprietary tool/license is bundled.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_file
from zlang.fpga_boards import FpgaBoardError, board_constraints, load_board
from zlang.common.files import file_digest


def export_project(source: Path, *, board: str, output: Path,
                   top: str | None = None, assignments=None) -> dict:
    """Prepare a fresh relocatable batch project; never execute ambient gw_sh."""
    definition = load_board(board)
    config = definition.record
    if config['family'] != 'gowin':
        raise FpgaBoardError('Gowin EDA project requires a Gowin board')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FpgaBoardError('Gowin project output must be fresh')
    output.mkdir(parents=True)
    receipt = {'schema': 'zlang-gowin-eda-project-v1', 'state': 'preparing',
               'vendor_execution': 'UNKNOWN', 'hardware': 'UNKNOWN',
               'board_identity': definition.identity, 'device': config['device'],
               'reference': 'https://cdn.gowinsemi.com.cn/SUG1220E.pdf',
               'reference_version': 'SUG1220-2.1E',
               'reference_sha256': 'c8ee7e8cb8486220a8639424f71debe9f4c3dff0d4c365860474c24782ded08c'}
    try:
        result = compile_file(source, top=top, target=config['target'])
        artifact = emit_target_artifact(result.ir, result.implementation_graph,
                                        selected_ir_identity=result.selected_ir_identity)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', artifact.module):
            raise FpgaBoardError('unsupported Gowin Tcl top identifier')
        if not re.fullmatch(r'GW1NR-LV9QN88PC[67]/I[56]', config['device']):
            raise FpgaBoardError('unqualified Gowin device/revision')
        pins = board_constraints(result.ir, artifact, definition, assignments)
        (output / 'dut.sv').write_text(artifact.text)
        (output / 'pins.cst').write_text(pins['pin_text'])
        (output / 'clock.sdc').write_text(pins['clock_text'])
        (output / 'backend.json').write_text(artifact.to_json() + '\n')
        (output / 'build.tcl').write_text(
            '# Run from this directory with licensed Gowin EDA: gw_sh build.tcl\n'
            '# No force/overwrite or programming commands.\n'
            f'create_project -name zlang_dut -dir ./vendor-project -pn {config["device"]} -device_version C\n'
            'add_file [file normalize ./dut.sv]\n'
            'add_file -type cst [file normalize ./pins.cst]\n'
            'add_file -type sdc [file normalize ./clock.sdc]\n'
            'set_option -synthesis_tool GowinSynthesis\n'
            'set_option -verilog_std sysv2017\n'
            f'set_option -top_module {artifact.module}\n'
            'set_option -output_base_name zlang_dut\n'
            'run all\n')
        receipt.update(state='project_prepared', selected_ir_identity=result.selected_ir_identity,
                       constraint_identity=pins['identity'],
                       source_sha256=file_digest(Path(source)),
                       artifacts={n: file_digest(output / n) for n in
                                  ('dut.sv', 'pins.cst', 'clock.sdc', 'backend.json', 'build.tcl')})
    except Exception as error:
        receipt.update(state='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    return receipt
