`default_nettype none
module ContractedAdd__zlang_contracts (
    input logic clk,
    input logic rst,
    input logic [3:0] a,
    input logic [3:0] b,
    input logic [4:0] y
);
  operands_bounded: assume property (@(posedge clk) disable iff (rst == 1'b1) (1'($unsigned(((1'($unsigned(((4'($unsigned(a))) < (4'($unsigned(4'd8))))))) & (1'($unsigned(((4'($unsigned(b))) < (4'($unsigned(4'd8))))))))))));
  sum_matches: assert property (@(posedge clk) disable iff (rst == 1'b1) (((5'($unsigned(y))) == (5'($unsigned(5'($unsigned(((5'($unsigned(5'($unsigned(a))))) + (5'($unsigned(5'($unsigned(b))))))))))))));
endmodule

bind ContractedAdd ContractedAdd__zlang_contracts zlang_contracts (
    .clk(clk),
    .rst(rst),
    .a(a),
    .b(b),
    .y(y)
);
`default_nettype wire
