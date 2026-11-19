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


@pytest.mark.parametrize(
    ("port", "clock", "instance_name"),
    (("din", "", "dut"), ("dut", "", "dut_"), ("dut", "dut_", "dut__")),
    ids=("control", "dut", "dut-and-dut-underscore"),
)
def test_comparison_allocates_dut_instance_apart_from_physical_ports(
    tmp_path, port, clock, instance_name,
):
    source = tmp_path / "top.zhl"
    # An explicit clock keeps its spelling in the public ABI. Two ordinary
    # wire ports named dut and dut_ would collide during ABI projection.
    domain = f"clock {clock} reset rst" if clock else ""
    source.write_text(f"module Top {{ {domain} in {port}:u8 out y:u8 y={port} }}")
    artifacts = tmp_path / "comparison"

    result = compare_program(
        sim.compile(source), [{"set": {port: 42}}],
        simulator="iverilog", artifact_directory=artifacts,
    )

    assert result.native_trace == result.rtl_trace == ({"y": 42},)
    testbench = (artifacts / "compare_tb.sv").read_text()
    assert f"Top {instance_name} (" in testbench
    assert f".{port}({port})" in testbench
    if clock:
        assert f".{clock}({clock})" in testbench


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


@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_comparison_allocates_testbench_module_apart_from_dut(tmp_path, simulator):
    source = tmp_path / "top.zhl"
    source.write_text("module zlang_compare_tb {in dut:u8 out y:u8 y=dut}")
    events = [{"set": {"dut": value}} for value in (1, 3, 2)]
    expected = tuple({"y": value} for value in (1, 3, 2))
    artifacts = tmp_path / "comparison"
    compared = compare_program(
        sim.compile(source), events, simulator=simulator, artifact_directory=artifacts,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    testbench = (artifacts / "compare_tb.sv").read_text()
    assert "module zlang_compare_tb_;" in testbench
    assert "zlang_compare_tb dut_ (" in testbench
    with sim.compile(source, logic_state=True).create() as machine:
        assert tuple(
            {"y": row["y"].require_binary()} for row in machine.run_events(events)
        ) == expected


def test_stimulus_renderer_reserves_emitted_dependency_names():
    module = compile_source(SOURCE).ir
    original = render_testbench(module, "Top", ())
    assert original.module_name == "zlang_compare_tb"
    with_dependencies = render_testbench(
        module, "Top", (),
        rtl_text="module zlang_compare_tb (); endmodule\n"
        '(* keep_hierarchy = "yes" *) module zlang_compare_tb_ (); endmodule\n'
        "module Top (); endmodule\n",
    )
    assert with_dependencies.module_name == "zlang_compare_tb__"
    assert "module zlang_compare_tb__;" in with_dependencies.text
    assert with_dependencies.stimulus == original.stimulus


@pytest.mark.parametrize('simulator',('iverilog','verilator'))
def test_empty_event_comparison_and_no_input_design(tmp_path,simulator):
    path = tmp_path/'top.zhl'
    path.write_text('module Top{out y:bit y=1}')
    program = sim.compile(path)
    for i,events in enumerate(([],[{'set':{},'reset':{},'edges':[]}])):
        result = compare_program(program,events,simulator=simulator,
                                 artifact_directory=tmp_path/f'evidence{i}')
        assert result.native_trace == result.rtl_trace == (() if not events else ({'y':1},))
