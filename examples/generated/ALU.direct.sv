`default_nettype none
module ALU (
  input wire logic [31:0] a,
  input wire logic [31:0] b,
  input wire logic [2:0] op,
  output logic [31:0] y
);
  // Generated from backend-independent typed ZLang IR.
  // ZLang IR output: y
  assign y = ((op) == 3'd0 ? 32'(({{1{1'b0}}, a} + {{1{1'b0}}, b})) : ((op) == 3'd1 ? ((a) - (b)) : ((op) == 3'd2 ? ((a) & (b)) : ((op) == 3'd3 ? ((a) | (b)) : 32'd0))));
endmodule
`default_nettype wire
