`default_nettype none
module RuleCounter (
  input logic clk,
  input logic rst,
  input wire logic increment,
  input wire logic clear,
  output logic [7:0] count_out
);
  // Generated from backend-independent typed ZLang IR.
  logic [7:0] count;
  always_ff @(posedge clk) begin
    if (rst) begin
      count <= 8'd0;
    end else begin
      if (clear) count <= 8'd0;
      else if (increment) count <= 8'(({{1{1'b0}}, count} + {{1{1'b0}}, 8'd1}));
      else count <= count;
    end
  end
  assign count_out = count;
endmodule
`default_nettype wire
