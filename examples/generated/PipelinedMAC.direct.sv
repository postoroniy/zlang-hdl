`default_nettype none
module PipelinedMAC (
  input wire logic clk,
  input wire logic rst,
  input wire logic [7:0] a,
  input wire logic [7:0] b,
  input wire logic [15:0] c,
  output logic [16:0] y
);
  // Generated from backend-independent typed ZLang IR.
  logic [16:0] pipeline_0_s1;
  logic [16:0] pipeline_0_s2;
  always_ff @(posedge clk) begin
    if (rst) begin
      pipeline_0_s1 <= '0;
      pipeline_0_s2 <= '0;
    end else begin
      pipeline_0_s1 <= ({{1{1'b0}}, ({{8{1'b0}}, a} * {{8{1'b0}}, b})} + {{1{1'b0}}, c});
      pipeline_0_s2 <= pipeline_0_s1;
    end
  end
  assign y = pipeline_0_s2;
endmodule
`default_nettype wire
