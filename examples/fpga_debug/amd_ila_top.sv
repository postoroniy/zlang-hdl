// SPDX-License-Identifier: Apache-2.0
// Generate vendor_debug_ila in Vivado: native ILA, clk, probe0[15:0],
// probe1[2:0], probe2[15:0]. This file contains no implementation of vendor IP.
module AmdIlaTop (
    input wire clk,
    input wire rst,
    input wire [15:0] input_payload,
    input wire input_valid,
    output wire input_ready,
    output wire [15:0] output_payload,
    output wire output_valid,
    input wire output_ready
);
    (* mark_debug = "true" *) wire [15:0] debug_payload;
    (* mark_debug = "true" *) wire debug_valid, debug_ready, debug_transfer;
    (* mark_debug = "true" *) wire [15:0] debug_count;

    VendorDebugDut dut (
        .clk(clk), .rst(rst),
        .input_payload(input_payload), .input_valid(input_valid), .input_ready(input_ready),
        .output_payload(output_payload), .output_valid(output_valid), .output_ready(output_ready),
        .debug_payload(debug_payload), .debug_valid(debug_valid), .debug_ready(debug_ready),
        .debug_transfer(debug_transfer), .debug_count(debug_count)
    );

    vendor_debug_ila analyzer (
        .clk(clk),
        .probe0(debug_payload),
        .probe1({debug_valid, debug_ready, debug_transfer}),
        .probe2(debug_count)
    );
endmodule
