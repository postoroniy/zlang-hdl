`default_nettype none
module CreditSource (
  input logic clk,
  input logic rst,
  input wire logic [7:0] payload_data,
  input wire logic request,
  input logic tx_return,
  output logic [7:0] tx_payload,
  output logic tx_send
);
  // Generated from backend-independent typed ZLang IR.
  logic [1:0] tx_credits;
  assign tx_payload = payload_data;
  assign tx_send = !rst && (tx_credits != '0) && request;
  always_ff @(posedge clk) begin
    if (rst) begin
      tx_credits <= 2'd2;
    end else begin
      case ({tx_send, tx_return})
        2'b10: tx_credits <= tx_credits - 1'b1;
        2'b01: if (tx_credits < 2'd2) tx_credits <= tx_credits + 1'b1;
        default: tx_credits <= tx_credits;
      endcase
    end
  end
endmodule
`default_nettype wire
