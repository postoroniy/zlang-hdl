module tb;
  logic clk = 0;
  logic rst;
  integer cycle;
  logic [14:0] command_payload;
  logic command_valid;
  wire command_ready;
  logic [27:0] psdu_payload;
  logic psdu_valid;
  wire psdu_ready;
  wire [23:0] signal_payload;
  wire signal_valid;
  logic signal_ready;
  wire [50:0] zlang_output_payload;
  wire zlang_output_valid;
  logic zlang_output_ready;

  Ieee80211aTransmitter dut (.*);

  initial begin
    // BPSK 6 Mbit/s, one-byte PSDU 0x55.
    command_payload = 15'h1001;
    psdu_payload = 28'h0000557;
    signal_ready = 1;
    zlang_output_ready = 1;
    for (cycle = 0; cycle < 903; cycle = cycle + 1) begin
      rst = cycle == 0;
      command_valid = cycle == 1;
      psdu_valid = cycle == 2;
      #1;
      $display(
        "REC %0d %0d %0d %0d %06x %0d %013x",
        cycle,
        command_ready,
        psdu_ready,
        signal_valid,
        signal_payload,
        zlang_output_valid,
        zlang_output_payload
      );
      #1 clk = 1;
      #1 clk = 0;
    end
    $finish;
  end
endmodule
