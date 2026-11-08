# SPDX-License-Identifier: Apache-2.0
"""Limited P-output models and exact bindings for qualified FPGA multipliers.

The declared mode is static signed or unsigned, synchronous active-high reset,
CE tied high, input registers bypassed, optional multiplier/output registers.
Shift/cascade outputs and input-register configurations are not qualified.
Sources: Gowin UG287-1.4E; ECP5 FPGA-TN-02205-1.3, plus the exact pinned
Yosys/Trellis D-cell ABI. No C/D or A/B primitive alias is inferred.
"""

from zlang.backend.systemverilog.errors import SystemVerilogEmissionError


CORE = r"""
module zlang_fpga_mul_model #(parameter INPUT_REG=0, PIPE_REG=0, OUTPUT_REG=0)
 (input clk,rst,ce, input [17:0] a,b, input asign,bsign, output [35:0] p);
 wire [17:0] ax,bx;
 generate if (INPUT_REG) begin
   reg [17:0] aq,bq;
   always @(posedge clk) if(rst) begin aq<=0;bq<=0;end else if(ce) begin aq<=a;bq<=b;end
   assign ax=aq;assign bx=bq;
 end else begin assign ax=a;assign bx=b;end endgenerate
 wire [35:0] ss_product=$signed(ax)*$signed(bx);
 wire [35:0] uu_product=$unsigned(ax)*$unsigned(bx);
 wire signed [36:0] su_product=$signed(ax)*$signed({1'b0,bx});
 wire signed [36:0] us_product=$signed({1'b0,ax})*$signed(bx);
 wire [35:0] product=asign ? (bsign ? ss_product : su_product[35:0]) :
                              (bsign ? us_product[35:0] : uu_product);
 wire [35:0] mid;
 generate if(PIPE_REG) begin
   reg [35:0] q; always @(posedge clk) if(rst) q<=0;else if(ce) q<=product[35:0];
   assign mid=q;
 end else assign mid=product[35:0]; endgenerate
 generate if(OUTPUT_REG) begin
   reg [35:0] q; always @(posedge clk) if(rst) q<=0;else if(ce) q<=mid;
   assign p=q;
 end else assign p=mid; endgenerate
endmodule
"""

GOWIN = r"""
module MULT18X18 #(parameter AREG=0,BREG=0,PIPE_REG=0,OUT_REG=0,
 ASIGN_REG=0,BSIGN_REG=0,SOA_REG=0, MULT_RESET_MODE="SYNC")
 (input [17:0] A,SIA,B,SIB, input ASIGN,BSIGN,ASEL,BSEL,CE,CLK,RESET,
 output [35:0] DOUT, output [17:0] SOA,SOB);
 generate if(AREG!=0 || BREG!=0 ||
 (PIPE_REG!=0 && PIPE_REG!=1) || (OUT_REG!=0 && OUT_REG!=1) ||
 ASIGN_REG!=0 || BSIGN_REG!=0 || SOA_REG!=0 || MULT_RESET_MODE!="SYNC")
 begin unsupported_gowin_multiplier_configuration invalid(); end endgenerate
 always @* begin assert(ASEL==0);assert(BSEL==0);end
 zlang_fpga_mul_model #(.INPUT_REG(AREG),.PIPE_REG(PIPE_REG),.OUTPUT_REG(OUT_REG))
 core(.clk(CLK),.rst(RESET),.ce(CE),.a(A),.b(B),.asign(ASIGN),.bsign(BSIGN),.p(DOUT));
 assign SOA=18'bx;assign SOB=18'bx;
endmodule
"""


def ecp5_model():
    groups = ("A", "B", "C", "SRIA", "SRIB")
    inputs = [f"{g}{i}" for g in groups for i in range(18)]
    inputs += ["SIGNEDA", "SIGNEDB", "SOURCEA", "SOURCEB"]
    inputs += [f"{g}{i}" for g in ("CLK", "CE", "RST") for i in range(4)]
    outputs = [
        f"{g}{i}"
        for g, n in [
            ("P", 36),
            ("SROA", 18),
            ("SROB", 18),
            ("ROA", 18),
            ("ROB", 18),
            ("ROC", 18),
        ]
        for i in range(n)
    ]
    outputs += ["SIGNEDP"]
    params = []
    for g in ("INPUTA", "INPUTB", "INPUTC", "PIPELINE", "OUTPUT"):
        params += [
            f'parameter REG_{g}_CLK="NONE"',
            f'REG_{g}_CE="CE0"',
            f'REG_{g}_RST="RST0"',
        ]
    params += ['parameter RESETMODE="SYNC", GSR="DISABLED"']
    def pairs(g, n):
        return "{" + ",".join(f"{g}{i}" for i in reversed(range(n))) + "}"
    return (
        "module MULT18X18D #(\n"
        + ",\n".join(params)
        + ") (input "
        + ",".join(inputs)
        + ", output "
        + ",".join(outputs)
        + ");\n"
        + f"""
 generate if(REG_INPUTA_CLK!=REG_INPUTB_CLK ||
 REG_INPUTA_CLK!="NONE" ||
 (REG_PIPELINE_CLK!="NONE" && REG_PIPELINE_CLK!="CLK0") ||
 (REG_OUTPUT_CLK!="NONE" && REG_OUTPUT_CLK!="CLK0") || REG_INPUTC_CLK!="NONE" ||
 REG_INPUTA_CE!="CE0" || REG_INPUTB_CE!="CE0" || REG_PIPELINE_CE!="CE0" || REG_OUTPUT_CE!="CE0" ||
 REG_INPUTA_RST!="RST0" || REG_INPUTB_RST!="RST0" || REG_PIPELINE_RST!="RST0" || REG_OUTPUT_RST!="RST0" ||
 RESETMODE!="SYNC" || GSR!="DISABLED")
 begin unsupported_ecp5_multiplier_configuration invalid(); end endgenerate
 always @* begin assert(SOURCEA==0); assert(SOURCEB==0);end
 wire [35:0] product;
 zlang_fpga_mul_model #(.INPUT_REG(REG_INPUTA_CLK=="CLK0"),
 .PIPE_REG(REG_PIPELINE_CLK=="CLK0"),.OUTPUT_REG(REG_OUTPUT_CLK=="CLK0"))
 core(.clk(CLK0),.rst(RST0),.ce(CE0),.a({pairs("A", 18)}),.b({pairs("B", 18)}),
 .asign(SIGNEDA),.bsign(SIGNEDB),.p(product));
 assign {pairs("P", 36)}=product;
 assign SIGNEDP=SIGNEDA|SIGNEDB;
 assign {pairs("SROA", 18)}=18'bx;
 assign {pairs("SROB", 18)}=18'bx;
 assign {pairs("ROA", 18)}=18'bx;
 assign {pairs("ROB", 18)}=18'bx;
 assign {pairs("ROC", 18)}=18'bx;
endmodule
"""
    )


def multiplier_instance(
    family, *, pipe_reg, output_reg, a, b, p, clock, reset, name, signed, observe=False
):
    if family in {"ice40", "nexus", "gowin9", "gowin_v"}:
        from zlang.backend.systemverilog.fpga_scalar_primitives import scalar_instance
        return scalar_instance(family, pipe_reg=pipe_reg, output_reg=output_reg,
                               a=a, b=b, p=p, clock=clock, reset=reset,
                               name=name, signed=signed, observe=observe)
    input_reg = 0
    ce = "1'b1"
    if (
        family not in ("gowin", "ecp5")
        or type(signed) is not bool
        or any(type(v) is not int or v not in (0, 1) for v in (pipe_reg, output_reg))
    ):
        raise SystemVerilogEmissionError("unqualified FPGA multiplier configuration")
    sign = "1'b1" if signed else "1'b0"
    if family == "gowin":
        return f"""MULT18X18 #(.AREG({input_reg}),.BREG({input_reg}),.PIPE_REG({pipe_reg}),.OUT_REG({output_reg}))
 {name}(.A({a}),.B({b}),.SIA(18'b0),.SIB(18'b0),.ASEL(1'b0),.BSEL(1'b0),
 .ASIGN({sign}),.BSIGN({sign}),.CLK({clock}),.RESET({reset}),.CE({ce}),.DOUT({p}),.SOA(),.SOB(){",.ZLANG_OBS_A(obs_a),.ZLANG_OBS_B(obs_b),.ZLANG_OBS_MID(obs_mid)" if observe else ""});"""
    def clock_value(enabled):
        return '"CLK0"' if enabled else '"NONE"'
    params = {
        f"REG_{g}_CLK": clock_value(v)
        for g, v in [
            ("INPUTA", input_reg),
            ("INPUTB", input_reg),
            ("PIPELINE", pipe_reg),
            ("OUTPUT", output_reg),
        ]
    }
    params.update(RESETMODE='"SYNC"', GSR='"DISABLED"')
    ports = {
        f"{g}{i}": f"{v}[{i}]"
        for g, v in [("A", a), ("B", b), ("P", p)]
        for i in range(36 if g == "P" else 18)
    }
    ports.update({f"C{i}": "1'b0" for i in range(18)})
    # Unselected dedicated shift inputs are not routable fabric pins. Leave
    # them open, as the pinned synthesizer does for SOURCEA=SOURCEB=0.
    ports.update({f"{g}{i}": "" for g in ("SRIA", "SRIB") for i in range(18)})
    ports.update(SIGNEDA=sign, SIGNEDB=sign, SOURCEA="1'b0", SOURCEB="1'b0")
    ports.update(
        {
            f"{g}{i}": (v if i == 0 else "")
            for g, v in [("CLK", clock), ("CE", ce), ("RST", reset)]
            for i in range(4)
        }
    )
    ports.update(
        {
            f"{g}{i}": ""
            for g in ("SROA", "SROB", "ROA", "ROB", "ROC")
            for i in range(18)
        }
    )
    ports["SIGNEDP"] = ""
    if not (pipe_reg or output_reg):
        ports.update(CLK0="", CE0="", RST0="")
    if observe:
        ports.update(ZLANG_OBS_A="obs_a", ZLANG_OBS_B="obs_b", ZLANG_OBS_MID="obs_mid")
    return (
        "MULT18X18D #("
        + ",".join(f".{k}({v})" for k, v in params.items())
        + f") {name}(\n"
        + ",\n".join(f".{k}({v})" for k, v in ports.items())
        + ");"
    )


def multiplier_models(family, *, observe=False):
    if family in {"ice40", "nexus", "gowin9", "gowin_v"}:
        from zlang.backend.systemverilog.fpga_scalar_primitives import scalar_model
        return scalar_model(family, observe=observe)
    if family not in ("gowin", "ecp5"):
        raise SystemVerilogEmissionError("unqualified FPGA primitive family")
    core = CORE
    wrapper = GOWIN if family == "gowin" else ecp5_model()
    if observe:
        core = core.replace(
            "output [35:0] p);",
            "output [35:0] p, output [17:0] ZLANG_OBS_A,ZLANG_OBS_B, output [35:0] ZLANG_OBS_MID);",
        )
        core = core.replace(
            "endmodule",
            "assign ZLANG_OBS_A=ax;assign ZLANG_OBS_B=bx;assign ZLANG_OBS_MID=mid;\nendmodule",
        )
        if family == "gowin":
            wrapper = wrapper.replace(
                "output [17:0] SOA,SOB);",
                "output [17:0] SOA,SOB,ZLANG_OBS_A,ZLANG_OBS_B, output [35:0] ZLANG_OBS_MID);",
            )
            wrapper = wrapper.replace(
                ".p(DOUT));",
                ".p(DOUT),.ZLANG_OBS_A(ZLANG_OBS_A),.ZLANG_OBS_B(ZLANG_OBS_B),.ZLANG_OBS_MID(ZLANG_OBS_MID));",
            )
        else:
            wrapper = wrapper.replace(
                "SIGNEDP);",
                "SIGNEDP, output [17:0] ZLANG_OBS_A,ZLANG_OBS_B, output [35:0] ZLANG_OBS_MID);",
            )
            wrapper = wrapper.replace(
                ".p(product));",
                ".p(product),.ZLANG_OBS_A(ZLANG_OBS_A),.ZLANG_OBS_B(ZLANG_OBS_B),.ZLANG_OBS_MID(ZLANG_OBS_MID));",
            )
    return core + wrapper
