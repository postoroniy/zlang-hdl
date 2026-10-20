"""Actual native/Direct-SV execution of compile-time-only modulo."""

from pathlib import Path
import subprocess

from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.native_simulation import simulate
from zlang.toolchain import lint_with_verilator


def test_nr_pss_modulo_native_and_generated_rtl_agree(tmp_path: Path):
    source = '''
        fn nr_pss_chip<ID,K>() -> s2 {
            mseq=0x42afa51b8fe1de592044c5d6c19a9cf6
            mseq[(K+43*ID)%127] ? -1 : 1
        }
        module PssModulo {
            out pss0:vec<127,s2> out pss1:vec<127,s2> out pss2:vec<127,s2>
            pss0=generate(k in 0..127) nr_pss_chip<ID=0,K=k>()
            pss1=generate(k in 0..127) nr_pss_chip<ID=1,K=k>()
            pss2=generate(k in 0..127) nr_pss_chip<ID=2,K=k>()
        }
    '''
    compiled = compile_source(source)
    expected = {}
    sequence = 0x42afa51b8fe1de592044c5d6c19a9cf6
    for ident in range(3):
        expected[f'pss{ident}'] = [-1 if sequence >> ((k+43*ident) % 127) & 1 else 1
                                  for k in range(127)]
    assert simulate(compiled.ir) == expected
    rtl = tmp_path/'design.sv'
    rtl.write_text(emit(compiled.ir))
    assert '%' not in rtl.read_text()
    lint_with_verilator((rtl,), compiled.ir.name)
    bench = tmp_path/'bench.sv'
    bench.write_text('''module bench;
        wire [253:0] pss0,pss1,pss2;
        PssModulo dut(.pss0(pss0),.pss1(pss1),.pss2(pss2));
        initial begin #1; $display("%h",pss0); $display("%h",pss1);
            $display("%h",pss2); $finish(0); end
        endmodule
    ''')
    build = subprocess.run(['iverilog', '-g2012', '-s', 'bench', '-o', str(tmp_path/'sim'),
                            str(rtl), str(bench)], capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(['vvp', str(tmp_path/'sim')], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    packed = [sum((value & 3) << (2*k) for k, value in enumerate(row))
              for row in expected.values()]
    assert [int(line, 16) for line in run.stdout.splitlines()] == packed
