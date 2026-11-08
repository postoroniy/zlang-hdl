# SPDX-License-Identifier: Apache-2.0
"""Closed scalar DSP configurations; no packed arithmetic or native cascade.

SB_MAC16 uses synchronous output zero-load, not asynchronous reset.
Arora V MULT12X12 is signed-only (UG305-1.0.1E section 2).
"""
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError


def _parameters(family, output_reg, signed):
    if type(output_reg) is not int or output_reg not in (0, 1) or type(signed) is not bool:
        raise SystemVerilogEmissionError("unsupported scalar DSP configuration")
    if family == "gowin9":
        return "MULT9X9", 9, dict(AREG=0, BREG=0, PIPE_REG=0, OUT_REG=output_reg,
                                  ASIGN_REG=0, BSIGN_REG=0, SOA_REG=0, MULT_RESET_MODE='"SYNC"')
    if family == "nexus":
        return "MULT18X18", 18, dict(REGINPUTA='"BYPASS"', REGINPUTB='"BYPASS"',
                                     REGOUTPUT='"REGISTER"' if output_reg else '"BYPASS"',
                                     GSR='"DISABLED"', RESETMODE='"SYNC"')
    if family == "gowin_v":
        return "MULT12X12", 12, {
            **{f"{r}_CLK": '"BYPASS"' for r in ("AREG", "BREG", "PREG")},
            "OREG_CLK": '"CLK0"' if output_reg else '"BYPASS"',
            **{f"{r}_CE": '"CE0"' for r in ("AREG", "BREG", "PREG", "OREG")},
            **{f"{r}_RESET": '"RESET0"' for r in ("AREG", "BREG", "PREG", "OREG")},
            "MULT_RESET_MODE": '"SYNC"'}
    if family == "ice40":
        return "SB_MAC16", 16, {
            **{p: 0 for p in ("NEG_TRIGGER", "C_REG", "A_REG", "B_REG", "D_REG",
                              "TOP_8x8_MULT_REG", "BOT_8x8_MULT_REG", "PIPELINE_16x16_MULT_REG1",
                              "PIPELINE_16x16_MULT_REG2", "TOPADDSUB_CARRYSELECT", "BOTADDSUB_CARRYSELECT", "MODE_8x8")},
            "TOPOUTPUT_SELECT": 1 if output_reg else 3, "BOTOUTPUT_SELECT": 1 if output_reg else 3,
            "TOPADDSUB_LOWERINPUT": 2, "BOTADDSUB_LOWERINPUT": 2,
            "TOPADDSUB_UPPERINPUT": 1, "BOTADDSUB_UPPERINPUT": 1,
            "A_SIGNED": int(signed), "B_SIGNED": int(signed)}
    raise SystemVerilogEmissionError("unsupported scalar DSP family")


def scalar_instance(family, *, pipe_reg, output_reg, a, b, p, clock, reset, name, signed, observe=False):
    if type(pipe_reg) is not int or pipe_reg != 0 or observe:
        raise SystemVerilogEmissionError("unsupported scalar DSP pipeline/observation")
    primitive, _, parameters = _parameters(family, output_reg, signed)
    sign = "1'b1" if signed else "1'b0"
    if family == "gowin9":
        ports = dict(A=a, B=b, SIA="9'b0", SIB="9'b0", ASIGN=sign, BSIGN=sign,
                     ASEL="1'b0", BSEL="1'b0", CE="1'b1", CLK=clock, RESET=reset, DOUT=p, SOA="", SOB="")
    elif family == "nexus":
        ports = dict(A=a, B=b, CLK=clock, CEA="1'b1", CEB="1'b1", CEOUT="1'b1",
                     RSTA="1'b0", RSTB="1'b0", RSTOUT=reset, SIGNEDA=sign, SIGNEDB=sign, Z=p)
    elif family == "gowin_v":
        ports = dict(A=a, B=b, DOUT=p, CLK="{1'b0,"+clock+"}", CE="2'b01", RESET="{1'b0,"+reset+"}")
    else:
        ports = dict(A=a, B=b, C="16'b0", D="16'b0", CLK=clock, CE="1'b1", O=p,
                     **{q: "1'b0" for q in ("AHOLD", "BHOLD", "CHOLD", "DHOLD", "IRSTTOP", "IRSTBOT",
                         "ORSTTOP", "ORSTBOT", "ADDSUBTOP", "ADDSUBBOT", "OHOLDTOP", "OHOLDBOT",
                         "CI", "ACCUMCI", "SIGNEXTIN")},
                     OLOADTOP=reset if output_reg else "1'b0", OLOADBOT=reset if output_reg else "1'b0",
                     CO="", ACCUMCO="", SIGNEXTOUT="")
    return (primitive+" #("+",".join(f".{k}({v})" for k,v in parameters.items())
            +f") {name}("+",".join(f".{k}({v})" for k,v in ports.items())+");")


def scalar_model(family, *, observe=False):
    """Executable subset with exact vendor ports and fail-closed parameters."""
    if observe:
        raise SystemVerilogEmissionError("unsupported scalar DSP observation")
    primitive, width, defaults = _parameters(family, 0, False)
    if family == "gowin9":
        ports = "input [8:0] A,B,SIA,SIB,input ASIGN,BSIGN,ASEL,BSEL,CE,CLK,RESET,output [17:0] DOUT,output [8:0] SOA,SOB"
        clock, reset, output, reg = "CLK", "RESET", "DOUT", "OUT_REG"
        checks = "ASEL==0 && BSEL==0 && CE==1"
        sa, sb, unused = "ASIGN", "BSIGN", "assign SOA=9'bx;assign SOB=9'bx;"
        variable = {"OUT_REG"}
        conditions = ["(OUT_REG!=0 && OUT_REG!=1)"]
    elif family == "nexus":
        ports = "input [17:0] A,B,input CLK,CEA,CEB,CEOUT,RSTA,RSTB,RSTOUT,SIGNEDA,SIGNEDB,output [35:0] Z"
        clock, reset, output, reg = "CLK", "RSTOUT", "Z", '(REGOUTPUT=="REGISTER")'
        checks = "CEA==1 && CEB==1 && CEOUT==1 && RSTA==0 && RSTB==0"
        sa, sb, unused = "SIGNEDA", "SIGNEDB", ""
        variable = {"REGOUTPUT"}
        conditions = ['(REGOUTPUT!="BYPASS" && REGOUTPUT!="REGISTER")']
    elif family == "gowin_v":
        ports = "input [11:0] A,B,input [1:0] CLK,CE,RESET,output [23:0] DOUT"
        clock, reset, output, reg = "CLK[0]", "RESET[0]", "DOUT", '(OREG_CLK=="CLK0")'
        checks = "CLK[1]==0 && CE==1 && RESET[1]==0"
        sa, sb, unused = "1'b1", "1'b1", ""
        variable = {"OREG_CLK"}
        conditions = ['(OREG_CLK!="BYPASS" && OREG_CLK!="CLK0")']
    else:
        ports = ("input CLK,CE,input [15:0] A,B,C,D,input AHOLD,BHOLD,CHOLD,DHOLD,IRSTTOP,IRSTBOT,ORSTTOP,ORSTBOT,"
                 "OLOADTOP,OLOADBOT,ADDSUBTOP,ADDSUBBOT,OHOLDTOP,OHOLDBOT,CI,ACCUMCI,SIGNEXTIN,output [31:0] O,output CO,ACCUMCO,SIGNEXTOUT")
        clock, reset, output, reg = "CLK", "OLOADTOP", "O", "(TOPOUTPUT_SELECT==1)"
        checks = ("CE==1 && C==0 && D==0 && OLOADTOP==OLOADBOT && "
                  "{AHOLD,BHOLD,CHOLD,DHOLD,IRSTTOP,IRSTBOT,ORSTTOP,ORSTBOT,ADDSUBTOP,ADDSUBBOT,OHOLDTOP,OHOLDBOT,CI,ACCUMCI,SIGNEXTIN}==0")
        sa, sb, unused = "A_SIGNED", "B_SIGNED", "assign CO=1'bx;assign ACCUMCO=1'bx;assign SIGNEXTOUT=1'bx;"
        variable = {"TOPOUTPUT_SELECT", "BOTOUTPUT_SELECT", "A_SIGNED", "B_SIGNED"}
        conditions = ["TOPOUTPUT_SELECT!=BOTOUTPUT_SELECT", "(TOPOUTPUT_SELECT!=1 && TOPOUTPUT_SELECT!=3)",
                      "(A_SIGNED!=0 && A_SIGNED!=1)", "A_SIGNED!=B_SIGNED"]
    conditions += [f"{k}!={v}" for k,v in defaults.items() if k not in variable]
    return (f"module {primitive} #("+",".join(f"parameter {k}={v}" for k,v in defaults.items())+f") ({ports});\n"
            +"generate if("+" || ".join(conditions)+") begin unsupported_scalar_dsp_configuration invalid();end endgenerate\n"
            +f"always @* assert({checks});\n"
            +f"wire signed [{width}:0] ax={sa} ? {{A[{width-1}],A}} : {{1'b0,A}};\n"
            +f"wire signed [{width}:0] bx={sb} ? {{B[{width-1}],B}} : {{1'b0,B}};\n"
            +f"wire [{2*width-1}:0] product=ax*bx;\n"
            +f"generate if({reg}) begin reg [{2*width-1}:0] q;always @(posedge {clock}) if({reset}) q<=0;else q<=product;assign {output}=q;end\n"
            +f"else assign {output}=product;endgenerate\n{unused}\nendmodule\n")
