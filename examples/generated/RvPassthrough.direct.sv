`default_nettype none
module RvPassthrough (
  input wire logic [7:0] rx_payload,
  input logic rx_valid,
  output logic rx_ready,
  output logic [7:0] tx_payload,
  output logic tx_valid,
  input logic tx_ready
);
  // Generated from backend-independent typed ZLang IR.
  assign tx_payload = rx_payload;
  assign tx_valid = rx_valid;
  assign rx_ready = tx_ready;
endmodule
`default_nettype wire
