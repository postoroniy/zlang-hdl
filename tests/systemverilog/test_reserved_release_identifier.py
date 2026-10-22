"""A legal source release name is escaped only in physical SystemVerilog."""

import pytest

from tests.simulation.differential import run_differential
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError


SOURCE = '''module Child {in release:bit out y:bit y=release}
module Top {in release:bit out y:bit
 child:Child child.release=release y=child.y}
'''


def test_release_port_and_hierarchy_use_physical_names_only(tmp_path):
    result = compile_source(SOURCE,top='Top')
    assert [port.name for port in result.ir.ports] == ['release','y']
    rtl = emit(result.ir)
    assert 'logic zlang_release' in rtl
    assert '.zlang_release(' in rtl
    path = tmp_path/'top.zhl'
    path.write_text(SOURCE)
    trace = run_differential(path,top='Top',directory=tmp_path,
                             events=[{'set':{'release':value}} for value in (0,1,0,1)])
    assert trace.native == trace.direct_sv


def test_release_mangling_collision_is_not_silently_aliased():
    result = compile_source('module Top {in release,zlang_release:bit out y:bit y=release ^ zlang_release}')
    with pytest.raises(SystemVerilogEmissionError,match='identifier collision'):
        emit(result.ir)
