`default_nettype none
module ControlCsr (
  input logic clk,
  input logic rst,
  input wire logic [31:0] addr,
  input wire logic write,
  input wire logic [31:0] wdata,
  input wire logic read,
  output logic [31:0] rdata,
  output logic ready
);
  // Generated from backend-independent typed ZLang IR.
  logic csr_control_control_enable;
  logic [2:0] csr_control_control_mode;
  logic csr_control_control_start;
  logic [2:0] csr_control_control_command;
  logic csr_control_status_error;
  always_ff @(posedge clk) begin
    if (rst) begin
      csr_control_control_enable <= 1'd0;
      csr_control_control_mode <= 3'd0;
      csr_control_control_start <= 1'd0;
      csr_control_control_command <= 3'd0;
      csr_control_status_error <= 1'd1;
    end else begin
      if ((write && addr == 32'h40000000)) csr_control_control_enable <= wdata[0];
      if ((write && addr == 32'h40000000)) csr_control_control_mode <= wdata[3:1];
      csr_control_control_start <= (write && addr == 32'h40000000) ? wdata[4] : '0;
      if ((write && addr == 32'h40000000)) csr_control_control_command <= wdata[7:5];
      if ((write && addr == 32'h40000004)) csr_control_status_error <= csr_control_status_error & ~wdata[1];
    end
  end
  always_comb begin
    rdata = 32'b0;
    if (read) begin
      case (addr)
        32'h40000000: begin
          rdata[0:0] = csr_control_control_enable;
          rdata[3:1] = csr_control_control_mode;
        end
        32'h40000004: begin
          rdata[0:0] = 1'd1;
          rdata[1:1] = csr_control_status_error;
        end
        default: rdata = 32'b0;
      endcase
    end
    ready = (read || write) && (addr == 32'h40000000 || addr == 32'h40000004);
  end
endmodule
`default_nettype wire
