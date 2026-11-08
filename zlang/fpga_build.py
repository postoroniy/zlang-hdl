"""Bounded production Direct-SV to FPGA bitstream flow; no programming side effects."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time

from zlang.backend.systemverilog import emit_target_artifact
from zlang.common import stable_digest
from zlang.common.subprocess import run_bounded_process, subprocess_text
from zlang.compiler import compile_file
from zlang.fpga_boards import board_constraints, load_board
from zlang.fpga_reports import routed_measurement, synthesis_resources, verify_device_inventory
from zlang.fpga_toolchain import load_toolchain
from zlang.common.files import file_digest


STAGE_TIMEOUT = 120
RECIPE = 'zlang-oss-fpga-v2'


class FpgaBuildError(RuntimeError):
    """A required stage failed; its workspace and partial receipt are retained."""


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n')


def build_fpga(source: Path, *, board: str, toolchain_manifest: Path,
               output: Path, top: str | None = None,
               assignments: dict[str, str] | None = None, seed: int = 1,
               architecture: str | None = None, architecture_mode: str | None = None,
               target_evidence_policy: str | None = None, self_test_wrapper: bool = False) -> dict:
    """Compile, synthesize, route and pack one exact board configuration.

    No cache, timing-ignore, unconstrained-pin or fallback route is permitted.
    Each external process has an independent fixed 120s deadline. A failed
    invocation keeps its logs and cannot be resumed/relabelled in this directory.
    """
    source, output = Path(source).resolve(), Path(output).absolute()
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise FpgaBuildError('invalid deterministic route seed')
    if output.exists() or output.is_symlink():
        raise FpgaBuildError('FPGA output directory must be fresh')
    output.mkdir(parents=True)
    policy_files = ('fpga_build.py', 'fpga_boards.py', 'fpga_reports.py', 'fpga_toolchain.py', 'common/files.py', 'common/subprocess.py', 'fpga_probe.py', 'target_fpga_bindings.py', 'target_mapping_storage.py', 'target_mapping_fir.py', 'backend/systemverilog/fpga_dsp.py', 'backend/systemverilog/fpga_primitives.py', 'backend/systemverilog/fpga_scalar_primitives.py', 'target_capabilities.py')
    receipt = {'schema': 'zlang-fpga-build-v1', 'recipe': RECIPE, 'state': 'running',
               'board': board, 'stages': [], 'hardware': 'UNKNOWN',
               'vendor_gowin_eda': 'UNKNOWN', 'seed': seed,
               'stage_timeout_seconds': STAGE_TIMEOUT,
               'orchestration_sources': {name: file_digest(Path(__file__).parent / name)
                                         for name in policy_files}}

    def save():
        _write(output / 'receipt.json', receipt)

    def run(stage, args, expected):
        record = {'name': stage, 'argv': args, 'state': 'running'}
        receipt['stages'].append(record)
        save()
        started = time.monotonic()
        try:
            process = run_bounded_process(args, timeout=STAGE_TIMEOUT,
                                          capture_output=True, text=True,
                                          cwd=output, env=toolchain.environment(output))
            stdout, stderr, code = process.stdout, process.stderr, process.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, code = error.output, error.stderr, None
            record['state'] = 'timeout'
        (output / f'{stage}.stdout.log').write_text(subprocess_text(stdout))
        (output / f'{stage}.stderr.log').write_text(subprocess_text(stderr))
        record.update(seconds=round(time.monotonic() - started, 6), returncode=code)
        if code != 0:
            record['state'] = 'timeout' if code is None else 'failed'
            save()
            raise FpgaBuildError(f'{stage} {record["state"]}; logs retained at {output}')
        for name in expected:
            path = output / name
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                record['state'] = 'missing_artifact'
                save()
                raise FpgaBuildError(f'{stage} omitted {name}')
        record.update(state='passed', artifacts={n: file_digest(output / n) for n in expected})
        save()

    save()
    try:
        definition = load_board(board)
        config = definition.record
        toolchain = load_toolchain(toolchain_manifest)
        receipt.update(board_identity=definition.identity, board_configuration=config,
                       toolchain_identity=toolchain.identity,
                       source_sha256=file_digest(source))
        result = compile_file(source, top=top, target=config['target'], architecture=architecture,
                              architecture_mode=architecture_mode, target_evidence_policy=target_evidence_policy)
        artifact = emit_target_artifact(result.ir, result.implementation_graph,
                                        selected_ir_identity=result.selected_ir_identity)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', artifact.module):
            raise FpgaBuildError('unsupported physical top identifier')
        closure = result.ir.dependency_closure
        receipt['dependency_closure'] = closure.to_data() if closure is not None else None
        physical_top = artifact.module
        rtl_files = ['dut.sv']
        pin_module, pin_artifact = result.ir, artifact
        if self_test_wrapper:
            from zlang.fpga_probe import physical_probe
            pin_module, pin_artifact, wrapper = physical_probe(result.ir, artifact)
            (output / 'board-wrapper.sv').write_text(wrapper)
            physical_top = pin_artifact.module
            rtl_files.append('board-wrapper.sv')
            receipt['board_wrapper'] = {'sha256': file_digest(output / 'board-wrapper.sv'),
                                      'module': physical_top,
                                      'measurement_scope': 'core plus stimulus/checksum fixture; not isolated core QoR'}
        pins = board_constraints(pin_module, pin_artifact, definition, assignments)
        (output / 'dut.sv').write_text(artifact.text)
        (output / 'backend.json').write_text(artifact.to_json() + '\n')
        _write(output / 'constraints.json', pins)
        (output / ('pins.' + pins['pin_format'])).write_text(pins['pin_text'])
        (output / 'clock.sdc').write_text(pins['clock_text'])
        receipt.update(selected_ir_identity=result.selected_ir_identity,
                       rtl_sha256=file_digest(output / 'dut.sv'),
                       backend_artifact_hash=artifact.artifact_hash,
                       backend_build_identity=artifact.build_identity,
                       target_identity=result.target_instance.identity,
                       target_source_sha256=result.target_instance.source_hash,
                       constraint_identity=pins['identity'],
                       backend_implementation_report=result.backend_implementation_report)
        lint_files = list(rtl_files)
        if not result.implementation_graph.is_generic:
            model_artifact = emit_target_artifact(result.ir, result.implementation_graph,
                            simulation_model=True, selected_ir_identity=result.selected_ir_identity)
            (output / 'dut-model.sv').write_text(model_artifact.text)
            receipt['lint_model_sha256'] = file_digest(output / 'dut-model.sv')
            lint_files[0] = 'dut-model.sv'
        run('rtl-validation', [str(toolchain.executable('verilator')),
                               '--lint-only', '--top-module', physical_top, *lint_files], [])
        yosys = str(toolchain.executable('yosys'))
        family = config['family']
        synth = 'synth_gowin -family gw1n' if family == 'gowin' else 'synth_ecp5'
        if self_test_wrapper:
            core_script = f'read_verilog -sv dut.sv; {synth} -top {artifact.module} -json core.json; check -assert'
            (output / 'core-synthesis.ys').write_text(core_script + '\n')
            run('core-synthesis', [yosys, '-s', 'core-synthesis.ys'], ['core.json'])
            receipt['isolated_core_synthesis_cells'] = synthesis_resources(output / 'core.json', artifact.module)
        script = f'read_verilog -sv {" ".join(rtl_files)}; {synth} -top {physical_top} -json synthesized.json; check -assert'
        (output / 'synthesize.ys').write_text(script + '\n')
        run('synthesis', [yosys, '-s', 'synthesize.ys'], ['synthesized.json'])
        receipt['synthesis_cells'] = synthesis_resources(output / 'synthesized.json', physical_top)
        freq = str(config['clock_mhz'])
        if family == 'gowin':
            route_tool, pack_tool = 'nextpnr-himbaechel', 'gowin_pack'
            route = [str(toolchain.executable(route_tool)), '--device', config['device'],
                     '--vopt', 'family=GW1N-9C', '--vopt', 'cst=pins.cst',
                     '--json', 'synthesized.json', '--write', 'routed.json',
                     '--report', 'timing.json', '--freq', freq, '--seed', str(seed)]
            pack = [str(toolchain.executable(pack_tool)), '-d', 'GW1N-9C',
                    '-o', 'design.fs', 'routed.json']
            route_outputs, bitstream = ['routed.json', 'timing.json'], 'design.fs'
        else:
            route_tool, pack_tool = 'nextpnr-ecp5', 'ecppack'
            route = [str(toolchain.executable(route_tool)), f'--{config["density"]}k',
                     '--package', config['package'], '--speed', str(config['speed']),
                     '--json', 'synthesized.json', '--lpf', 'pins.lpf',
                     '--textcfg', 'routed.config', '--report', 'timing.json',
                     '--freq', freq, '--seed', str(seed)]
            pack = [str(toolchain.executable(pack_tool)), '--compress',
                    'routed.config', 'design.bit']
            route_outputs, bitstream = ['routed.config', 'timing.json'], 'design.bit'
        receipt['tools'] = [toolchain.tool_record(n) for n in ('verilator', 'yosys', route_tool, pack_tool)]
        run('routing', route, route_outputs)
        receipt['routing'] = routed_measurement(output / 'timing.json', config['clock_mhz'])
        verify_device_inventory(receipt['routing'], result.target_instance, family)
        run('packing', pack, [bitstream])
        toolchain.verify()
        if file_digest(source) != receipt['source_sha256']:
            raise FpgaBuildError('source changed during physical build')
        receipt.update(state='packed', bitstream=bitstream,
                       bitstream_sha256=file_digest(output / bitstream))
        receipt['build_identity'] = stable_digest({
            k: v for k, v in receipt.items() if k not in {'stages', 'build_identity'}
        })
        save()
        return receipt
    except Exception as error:
        receipt.update(state='failed', error=f'{type(error).__name__}: {error}')
        save()
        raise
