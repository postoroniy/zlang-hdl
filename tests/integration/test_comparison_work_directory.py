"""Native/RTL comparison works with relative work paths without weakening safety."""

from pathlib import Path

import pytest

from zlang import sim
from zlang.simulation_compare import compare_program, SimulationComparisonError


@pytest.mark.parametrize('simulator,spaced', (('verilator', False), ('iverilog', False), ('iverilog', True)))
@pytest.mark.parametrize('relative', (True, False))
def test_comparison_nested_work_directory(tmp_path, monkeypatch, simulator, spaced, relative):
    source = tmp_path/'design.zhl'
    source.write_text('module Top{in a:u8 out y:u9 y=a+1}')
    program = sim.compile(source, top='Top')
    monkeypatch.chdir(tmp_path)
    directory = Path('nested/work with spaces' if spaced else 'nested/work')
    if not relative:
        directory = directory.resolve()
    result = compare_program(program, ({'set': {'a': 3}}, {'set': {'a': 255}}),
                             simulator=simulator, artifact_directory=directory)
    assert result.native_trace == result.rtl_trace == ({'y': 4}, {'y': 256})
    assert result.artifact_directory == directory.resolve()


def test_verilator_upstream_make_space_limitation_is_a_failure_not_a_pass(tmp_path, monkeypatch):
    source = tmp_path/'design.zhl'
    source.write_text('module Top{out y:bit y=0}')
    program = sim.compile(source, top='Top')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SimulationComparisonError, match='cannot build in directories containing spaces'):
        compare_program(program, ({'set': {}},), simulator='verilator',
                        artifact_directory=Path('work with spaces'))
    assert not (tmp_path/'work with spaces/comparison.json').exists()


def test_comparison_work_directory_still_rejects_symlink_and_nonempty(tmp_path, monkeypatch):
    source = tmp_path/'design.zhl'
    source.write_text('module Top{out y:bit y=0}')
    program = sim.compile(source, top='Top')
    monkeypatch.chdir(tmp_path)
    real = tmp_path/'real'
    real.mkdir()
    link = tmp_path/'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SimulationComparisonError, match='must not be a symlink'):
        compare_program(program, ({'set': {}},), simulator='verilator', artifact_directory=Path('link'))
    sentinel = real/'user.txt'
    sentinel.write_text('keep')
    with pytest.raises(SimulationComparisonError, match='must be empty'):
        compare_program(program, ({'set': {}},), simulator='verilator', artifact_directory=Path('real'))
    assert sentinel.read_text() == 'keep'
