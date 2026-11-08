"""Public regression coverage for bounded FPGA orchestration, not board proof."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_file, compile_source
from zlang.fpga_boards import BoardDefinition, FpgaBoardError, board_constraints, board_names, load_board
from zlang.fpga_build import FpgaBuildError, build_fpga
from zlang.fpga_reports import FpgaReportError, routed_measurement, synthesis_resources
from zlang.fpga_toolchain import FpgaToolchainError, inventory_tree, load_toolchain
from zlang.targets import load_target


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'examples/fpga/counter.zhl').read_text()


@pytest.fixture(scope='module')
def product():
    result = compile_source(SOURCE)
    return result, emit_target_artifact(result.ir, result.implementation_graph,
                                       selected_ir_identity=result.selected_ir_identity)


@pytest.mark.parametrize('target,logic,ff,dsp,bram,pll', [
    ('gw1nr_lv9qn88pc6', 8640, 6480, 20, 26, 2),
    ('gw1nr_lv9qn88pc7', 8640, 6480, 20, 26, 2),
    ('lfe5u_25f_8mg285c', 24288, 24288, 28, 56, 2),
    ('lfe5u_85f_8mg285', 83640, 83640, 156, 208, 4),
])
def test_source_defined_targets_keep_generic_rtl(product, target, logic, ff, dsp, bram, pll):
    device, _, resources = load_target(target)
    counts = dict(device.inventory)
    by_class = {r.resource_class: counts.get(r.identity) for r in resources}
    assert by_class['logic'] == logic
    assert by_class['register'] == ff
    assert by_class['multiplier'] == dsp
    assert by_class['block_memory'] == bram
    assert max(counts[r.identity] for r in resources if r.name.endswith('PLL')) == pll
    for resource in resources:
        binding=dict(resource.backend_bindings)['systemverilog']
        if resource.resource_class in {'logic','register','arithmetic_chain','distributed_memory','shift_register'}:
            assert binding=='generic_rtl'
        elif resource.resource_class=='dsp_mac':
            assert binding==('gowin_dsp_fabric' if target.startswith('gw') else 'ecp5_dsp_fabric')
        elif resource.resource_class=='block_memory':
            assert binding==('gowin_bram_inference' if target.startswith('gw') else 'ecp5_bram_inference')
        else:
            assert binding=='unsupported'
    result = compile_source(SOURCE, target=target)
    artifact = emit_target_artifact(result.ir, result.implementation_graph,
                                    selected_ir_identity=result.selected_ir_identity)
    assert result.selected_ir_identity == product[0].selected_ir_identity
    assert artifact.text == product[1].text


@pytest.mark.parametrize('board', board_names())
def test_verified_board_ports_are_compiler_owned(product, board):
    result, artifact = product
    definition = load_board(board)
    pins = board_constraints(result.ir, artifact, definition)
    assert [p['member'] for p in pins['pins']] == ['clk', 'led', 'rst_n']
    assert len({p['pad'] for p in pins['pins']}) == 3
    assert pins['board_identity'] == definition.identity
    assert 'create_clock' in pins['clock_text']
    assert definition.record['hardware_status'] == 'UNKNOWN'
    assert definition.record['sources']


@pytest.mark.parametrize('name', ['orangecrab', 'orangecrab-r0.1-25f', 'epm11', 'brs'])
def test_no_implicit_board_revision_alias(name):
    with pytest.raises(FpgaBoardError, match='unknown board/revision'):
        load_board(name)


@pytest.mark.parametrize('assignments', [
    {'clk': 'clk', 'rst_n': 'rst_n'},
    {'clk': 'clk', 'rst_n': 'rst_n', 'led': 'unverified'},
    {'clk': 'clk', 'rst_n': 'rst_n', 'led': 'rst_n'},
    {'clk': 'led', 'rst_n': 'rst_n', 'led': 'clk'},
])
def test_bad_pin_coverage_direction_and_aliases_fail_closed(product, assignments):
    with pytest.raises(FpgaBoardError):
        board_constraints(product[0].ir, product[1], load_board('epm11-r1.0'), assignments)


def test_duplicate_abi_binding_and_wrong_reset_rejected(product):
    result, artifact = product
    with pytest.raises(FpgaBoardError, match='duplicate'):
        board_constraints(result.ir, replace(artifact, bindings=artifact.bindings * 2),
                          load_board('epm11-r1.0'))
    wrong = compile_source(SOURCE.replace('active_low', 'active_high'))
    emitted = emit_target_artifact(wrong.ir, wrong.implementation_graph,
                                   selected_ir_identity=wrong.selected_ir_identity)
    with pytest.raises(FpgaBoardError, match='polarity'):
        board_constraints(wrong.ir, emitted, load_board('epm11-r1.0'))


def test_board_identity_includes_revision_and_pin_layout():
    board = load_board('epm11-r1.0')
    data = board.record
    data['pins']['led']['pad'] = '16'
    assert BoardDefinition(board.name, json.dumps(data)).identity != board.identity


@pytest.mark.parametrize('name,top', [('counter','FpgaCounter'), ('gpio','FpgaGpio'),
    ('fixed_dsp','FpgaFixedDsp'), ('memory','FpgaMemory'), ('hierarchy','FpgaHierarchy')])
def test_all_witnesses_compile_and_bind_without_reinterpreting_types(name, top):
    result = compile_file(ROOT / 'examples/fpga' / f'{name}.zhl', top=top,
                          target='lfe5u_25f_8mg285c')
    artifact = emit_target_artifact(result.ir, result.implementation_graph,
                                    selected_ir_identity=result.selected_ir_identity)
    assert len(board_constraints(result.ir, artifact, load_board('orangecrab-r0.2-25f'))['pins']) == 3


def _report(tmp_path, **changes):
    data = {'fmax': {'clk': {'achieved': 100, 'constraint': 48}},
            'utilization': {'LUT4': {'used': 3, 'available': 24}}}
    data.update(changes)
    p = tmp_path / 'timing.json'
    p.write_text(json.dumps(data))
    return p


def test_routed_measurement_preserves_pseudo_and_mode_counts(tmp_path):
    p = _report(tmp_path, utilization={'DUMMY_CELL': {'used': 4, 'available': 0},
                                      'MULT18X18': {'used': 1, 'available': 20}})
    result = routed_measurement(p, 48)
    assert result['achieved_mhz'] == 100
    assert result['pseudo_cells']['DUMMY_CELL']['used'] == 4
    assert result['primitive_utilization']['MULT18X18']['used'] == 1
    assert result['timing_kind'] == 'routed_tool_model'


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), True, 47])
def test_invalid_or_failing_timing_is_not_qualified(tmp_path, value):
    p = _report(tmp_path, fmax={'clk': {'achieved': value, 'constraint': 48}})
    with pytest.raises(FpgaReportError):
        routed_measurement(p, 48)


def test_wrong_constraint_missing_clock_and_overcapacity_fail(tmp_path):
    for change in ({'fmax': {}}, {'fmax': {'clk': {'achieved': 100, 'constraint': 27}}},
                   {'utilization': {'LUT4': {'used': 25, 'available': 24}}}):
        with pytest.raises(FpgaReportError):
            routed_measurement(_report(tmp_path, **change), 48)


def test_synthesis_counts_technology_boundaries_not_simulation_internals(tmp_path):
    modules = {'Top': {'cells': {'x': {'type': 'Child'}, 'metadata': {
        'type': '$scopeinfo', 'connections': {}, 'port_directions': {}}}},
        'Child': {'cells': {'cell': {'type': 'LUT4'}}},
        'LUT4': {'attributes': {'blackbox': '1'},
                 'cells': {'simulation': {'type': '$specify2'}}}}
    p = tmp_path / 'netlist.json'
    p.write_text(json.dumps({'modules': modules}))
    assert synthesis_resources(p, 'Top') == {'LUT4': 1}
    modules['Child']['cells']['cell']['type'] = '$add'
    p.write_text(json.dumps({'modules': modules}))
    with pytest.raises(FpgaReportError, match='unmapped'):
        synthesis_resources(p, 'Top')


def test_tool_manifest_cannot_substitute_ambient_tools(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text('{}')
    with pytest.raises(FpgaToolchainError):
        load_toolchain(p)
    p.write_bytes(b'\xff')
    with pytest.raises(FpgaToolchainError):
        load_toolchain(p)
    outside = tmp_path / 'outside'
    outside.mkdir()
    inside = tmp_path / 'tools'
    inside.mkdir()
    (inside / 'dependency').symlink_to(outside, target_is_directory=True)
    with pytest.raises(FpgaToolchainError, match='escapes'):
        inventory_tree(inside)


def test_failed_preparation_keeps_receipt_and_will_not_overwrite(tmp_path):
    output = tmp_path / 'build'
    with pytest.raises(FpgaBoardError):
        build_fpga(ROOT / 'examples/fpga/counter.zhl', board='unknown',
                   toolchain_manifest=tmp_path / 'missing', output=output)
    receipt = (output / 'receipt.json').read_bytes()
    assert json.loads(receipt)['state'] == 'failed'
    with pytest.raises(FpgaBuildError, match='fresh'):
        build_fpga(ROOT / 'examples/fpga/counter.zhl', board='unknown',
                   toolchain_manifest=tmp_path / 'missing', output=output)
    assert (output / 'receipt.json').read_bytes() == receipt


def test_stage_timeout_retains_partial_stdout_stderr_and_receipt(tmp_path, monkeypatch):
    import zlang.fpga_build as build
    class Tools:
        identity = 'unit-only-toolchain'
        def executable(self, name): return Path('/non-executed-unit-fixture') / name
        def environment(self, home): return {}
    monkeypatch.setattr(build, 'load_toolchain', lambda _: Tools())
    def timeout(args, **kwargs):
        assert kwargs['timeout'] == 120
        raise subprocess.TimeoutExpired(args, 120, output=b'partial stdout', stderr=b'partial stderr')
    monkeypatch.setattr(build, 'run_bounded_process', timeout)
    output = tmp_path / 'timeout'
    with pytest.raises(FpgaBuildError, match='timeout'):
        build.build_fpga(ROOT / 'examples/fpga/counter.zhl', board='epm11-r1.0',
                         toolchain_manifest=tmp_path / 'unit', output=output)
    data = json.loads((output / 'receipt.json').read_text())
    assert data['stages'][0]['state'] == 'timeout'
    assert data['hardware'] == 'UNKNOWN'
    assert (output / 'rtl-validation.stdout.log').read_text() == 'partial stdout'
    assert (output / 'rtl-validation.stderr.log').read_text() == 'partial stderr'


@pytest.fixture
def miniature_toolchain(tmp_path, monkeypatch):
    # Test the exact verifier algorithm on a miniature distribution; these
    # records cannot satisfy the unpatched production archive/file pins.
    import zlang.fpga_toolchain as owner
    root = tmp_path / 'distribution'
    (root / 'bin').mkdir(parents=True)
    for tool in owner.OSS_TOOLS:
        p = root / 'bin' / tool
        p.write_text('unit-fixture-only\n')
        p.chmod(0o755)
    (root / 'chipdb').write_text('unit chip database')
    rows = owner.inventory_tree(root)
    expected = owner.inventory_digest(rows)
    monkeypatch.setattr(owner, 'OSS_FILES_SHA256', expected)
    manifest = tmp_path / 'toolchain.json'
    manifest.write_text(json.dumps({'schema': owner.SCHEMA, 'root': str(root),
        'version': owner.OSS_VERSION, 'archive_sha256': owner.OSS_ARCHIVE_SHA256,
        'files_sha256': expected, 'files': rows}))
    return root, manifest


@pytest.mark.parametrize('kind', ['executable', 'database', 'extra', 'mode', 'manifest'])
def test_changed_tool_or_dependency_cannot_reuse_identity(miniature_toolchain, kind):
    root, manifest = miniature_toolchain
    frozen = load_toolchain(manifest)
    if kind == 'executable':
        (root / 'bin/yosys').write_text('changed executable')
    elif kind == 'database':
        (root / 'chipdb').write_text('changed database')
    elif kind == 'extra':
        (root / 'new-library').write_text('new dependency')
    elif kind == 'mode':
        (root / 'bin/yosys').chmod(0o644)
    else:
        data = json.loads(manifest.read_text())
        data['version'] = 'unreviewed'
        manifest.write_text(json.dumps(data))
    with pytest.raises(FpgaToolchainError):
        frozen.verify()


def test_tool_environment_does_not_inherit_python_or_library_overrides(miniature_toolchain, tmp_path, monkeypatch):
    _, manifest = miniature_toolchain
    monkeypatch.setenv('PYTHONPATH', '/unreviewed')
    monkeypatch.setenv('LD_PRELOAD', '/unreviewed')
    env = load_toolchain(manifest).environment(tmp_path)
    assert 'PYTHONPATH' not in env and 'LD_PRELOAD' not in env
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert env['PATH'].split(':')[0].endswith('/bin')


def test_gowin_vendor_export_is_source_bound_and_not_executed(tmp_path):
    from zlang.fpga_gowin import export_project
    out = tmp_path / 'vendor'
    r = export_project(ROOT / 'examples/fpga/counter.zhl', board='epm11-r1.0', output=out)
    assert r['state'] == 'project_prepared'
    assert r['vendor_execution'] == r['hardware'] == 'UNKNOWN'
    text = (out / 'build.tcl').read_text()
    assert '-pn GW1NR-LV9QN88PC7/I6 -device_version C' in text
    assert 'run all' in text and '-force' not in text
    assert set(r['artifacts']) == {'build.tcl', 'dut.sv', 'pins.cst', 'clock.sdc', 'backend.json'}
    with pytest.raises(FpgaBoardError, match='fresh'):
        export_project(ROOT / 'examples/fpga/counter.zhl', board='epm11-r1.0', output=out)
    with pytest.raises(FpgaBoardError, match='Gowin board'):
        export_project(ROOT / 'examples/fpga/counter.zhl', board='orangecrab-r0.2-25f',
                       output=tmp_path / 'wrong')


@pytest.mark.parametrize('target,family', [('gw1nr_lv9qn88pc7','gowin'), ('lfe5u_25f_8mg285c','ecp5')])
def test_routed_report_from_another_device_density_fails(target, family):
    from zlang.fpga_reports import verify_device_inventory
    device, _, _ = load_target(target)
    names = ({'GowinLUT4':'LUT4','GowinFF':'DFF','GowinMult18':'MULT18X18',
              'GowinBSRAM':'BSRAM','GowinPLL':'rPLL'} if family=='gowin' else
             {'Ecp5LUT4':'TRELLIS_COMB','Ecp5FF':'TRELLIS_FF','Ecp5Mult18':'MULT18X18D',
              'Ecp5EBR':'DP16KD','Ecp5PLL':'EHXPLLL'})
    data={'primitive_utilization':{names[n.rsplit('.',1)[-1]]:{'used':0,'available':c}
          for n,c in device.inventory if n.rsplit('.',1)[-1] in names}}
    verify_device_inventory(data,device,family)
    data['primitive_utilization'][next(iter(data['primitive_utilization']))]['available']+=1
    with pytest.raises(FpgaReportError,match='capacity'):
        verify_device_inventory(data,device,family)


@pytest.mark.parametrize('size', [0, 1, 4096, (1 << 20) - 1, (1 << 20) + 7])
def test_shared_streaming_file_identity_preserves_artifact_bytes(tmp_path, size):
    import hashlib
    from zlang.common.files import file_digest
    data=(b'ZLang\x00\xff\x01' * ((size+7)//8))[:size]
    path=tmp_path/'artifact.bin'
    path.write_bytes(data)
    assert file_digest(path)==hashlib.sha256(data).hexdigest()
    assert path.read_bytes()==data
    with pytest.raises(FileNotFoundError):
        file_digest(tmp_path/'absent')
