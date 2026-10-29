// SPDX-License-Identifier: Apache-2.0
// Quartus Prime Standard: select the preserved internal debug_* nets in
// Signal Tap Node Finder. The .stp instance is created in the vendor project.
module IntelSignalTapTop (
    input wire clk,
    input wire rst,
    input wire [15:0] input_payload,
    input wire input_valid,
    output wire input_ready,
    output wire [15:0] output_payload,
    output wire output_valid,
    input wire output_ready
);
    // keep preserves these combinational wires; preserve is for registers.
    (* keep = "true" *) wire [15:0] debug_payload;
    (* keep = "true" *) wire debug_valid, debug_ready, debug_transfer;
    (* keep = "true" *) wire [15:0] debug_count;

    VendorDebugDut dut (
        .clk(clk), .rst(rst),
        .input_payload(input_payload), .input_valid(input_valid), .input_ready(input_ready),
        .output_payload(output_payload), .output_valid(output_valid), .output_ready(output_ready),
        .debug_payload(debug_payload), .debug_valid(debug_valid), .debug_ready(debug_ready),
        .debug_transfer(debug_transfer), .debug_count(debug_count)
    );
endmodule
