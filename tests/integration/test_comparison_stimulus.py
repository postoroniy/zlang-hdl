"""Long event playback remains bounded and preserves sparse/edge semantics."""

import hashlib
import json

import pytest

from zlang import sim
from zlang.compiler import compile_source
from zlang.simulation_compare import compare_program
from zlang.simulation_comparison_testbench import render_testbench,STIMULUS_FILE


SOURCE = '''module Top {clock clk reset rst in enable:bit
 in zlang_compare_index:u8 in zlang_compare_stimulus:bit out y:u16
 reg count:u16=0 when enable {count <- truncate<16>(count+1)} y=count}
'''


@pytest.mark.parametrize('simulator',('iverilog','verilator'))
def test_long_comparison_stimulus_and_exact_sparse_update_semantics(tmp_path,simulator):
    path = tmp_path/'top.zhl'
    path.write_text(SOURCE)
    events = [{'edges':['clk'],'reset':{'rst':True},'set':{'enable':0,
               'zlang_compare_index':0,'zlang_compare_stimulus':0}}]
    expected,count,enable = [0],0,0
    for i in range(6000):
        update = {} if i%3 else {'enable':int(i%5 != 0)}
        enable = update.get('enable',enable)
        edges = [] if i%7 == 0 else ['clk']
        reset = i%997 == 0
        if edges:
            count = 0 if reset else (count+enable)&65535
        events.append({'edges':edges,'reset':{'rst':reset},'set':update})
        expected.append(count)
    result = compare_program(sim.compile(path),events,simulator=simulator,
                             artifact_directory=tmp_path/'evidence')
    assert result.native_trace == result.rtl_trace == tuple({'y':v} for v in expected)
    text = (tmp_path/'evidence/compare_tb.sv').read_text()
    assert len(text) < 5000
    assert text.count('$display') == 2
    stimulus = (tmp_path/'evidence'/STIMULUS_FILE).read_bytes()
    assert len(stimulus.splitlines()) == len(events)
    manifest = json.loads((tmp_path/'evidence/comparison.json').read_text())
    assert manifest['files'][STIMULUS_FILE] == hashlib.sha256(stimulus).hexdigest()


def test_stimulus_renderer_rejects_malformed_physical_events():
    module = compile_source(SOURCE).ir
    for event in (
        {'set':{'missing':1},'reset':{},'edges':[]},
        {'set':{},'reset':{'rst':1},'edges':[]},
        {'set':{},'reset':{'missing':False},'edges':[]},
        {'set':{},'reset':{},'edges':['clk','clk']},
        {'set':{},'reset':{},'edges':['missing']},
    ):
        with pytest.raises(ValueError):
            render_testbench(module,'Top',(event,))


@pytest.mark.parametrize('simulator',('iverilog','verilator'))
def test_empty_event_comparison_and_no_input_design(tmp_path,simulator):
    path = tmp_path/'top.zhl'
    path.write_text('module Top{out y:bit y=1}')
    program = sim.compile(path)
    for i,events in enumerate(([],[{'set':{},'reset':{},'edges':[]}])):
        result = compare_program(program,events,simulator=simulator,
                                 artifact_directory=tmp_path/f'evidence{i}')
        assert result.native_trace == result.rtl_trace == (() if not events else ({'y':1},))
