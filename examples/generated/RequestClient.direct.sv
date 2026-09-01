`default_nettype none
module RequestClient__zlang_core (
  input logic clk,
  input logic rst,
  input wire logic [9:0] request_payload,
  input wire logic issue,
  input wire logic accept_response,
  input logic mem_request_ready,
  input wire logic [9:0] mem_response_payload,
  input logic mem_response_valid,
  output logic [9:0] response_payload,
  output logic [9:0] mem_request_payload,
  output logic mem_request_valid,
  output logic mem_response_ready
);
  // Generated from backend-independent typed ZLang IR.
  logic [1:0] mem_outstanding;
  logic [1:0] mem_ids [0:1];
  logic [1:0] mem_ids_next [0:1];
  logic [1:0] mem_ids_valid, mem_ids_valid_next;
  logic mem_duplicate, mem_missing;
  logic mem_request_id_present, mem_response_id_present;
  logic mem_request_transfer, mem_response_transfer;
  integer zlang_scan;
  integer zlang_next;
  integer zlang_state;
  logic zlang_inserted;
  assign mem_request_payload = request_payload;
  assign response_payload = mem_response_payload;
  always_comb begin
    mem_request_id_present = 1'b0;
    mem_response_id_present = 1'b0;
    for (zlang_scan = 0; zlang_scan < 2; zlang_scan = zlang_scan + 1) begin
      if (mem_ids_valid[zlang_scan] && mem_ids[zlang_scan] == request_payload[9:8]) mem_request_id_present = 1'b1;
      if (mem_ids_valid[zlang_scan] && mem_ids[zlang_scan] == mem_response_payload[9:8]) mem_response_id_present = 1'b1;
    end
    mem_duplicate = !rst && issue && mem_request_ready && (mem_outstanding < 2'd2) && mem_request_id_present;
    mem_missing = !rst && accept_response && mem_response_valid && (mem_outstanding != '0) && !mem_response_id_present;
  end
  assign mem_request_valid = !rst && (mem_outstanding < 2'd2) && !mem_duplicate && issue;
  assign mem_response_ready = !rst && (mem_outstanding != '0) && !mem_missing && accept_response;
  assign mem_request_transfer = mem_request_valid && mem_request_ready;
  assign mem_response_transfer = mem_response_valid && mem_response_ready;
  always_comb begin
    mem_ids_valid_next = mem_ids_valid;
    for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) mem_ids_next[zlang_next] = mem_ids[zlang_next];
    if (mem_response_transfer) begin
      for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) begin
        if (mem_ids_valid_next[zlang_next] && mem_ids_next[zlang_next] == mem_response_payload[9:8]) mem_ids_valid_next[zlang_next] = 1'b0;
      end
    end
    zlang_inserted = 1'b0;
    if (mem_request_transfer) begin
      for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) begin
        if (!zlang_inserted && !mem_ids_valid_next[zlang_next]) begin
          mem_ids_next[zlang_next] = request_payload[9:8];
          mem_ids_valid_next[zlang_next] = 1'b1;
          zlang_inserted = 1'b1;
        end
      end
    end
  end
  always_ff @(posedge clk) begin
    if (rst) begin
      mem_outstanding <= '0;
      mem_ids_valid <= '0;
      for (zlang_state = 0; zlang_state < 2; zlang_state = zlang_state + 1) mem_ids[zlang_state] <= '0;
    end else begin
      case ({mem_request_transfer, mem_response_transfer})
        2'b10: if (mem_outstanding < 2'd2) mem_outstanding <= mem_outstanding + 1'b1;
        2'b01: if (mem_outstanding != '0) mem_outstanding <= mem_outstanding - 1'b1;
        default: mem_outstanding <= mem_outstanding;
      endcase
      mem_ids_valid <= mem_ids_valid_next;
      for (zlang_state = 0; zlang_state < 2; zlang_state = zlang_state + 1) mem_ids[zlang_state] <= mem_ids_next[zlang_state];
    end
  end
endmodule
module RequestClient (
  input wire logic clk,
  input wire logic rst,
  input wire logic [1:0] request_payload_id,
  input wire logic [7:0] request_payload_data,
  input wire logic issue,
  input wire logic accept_response,
  output logic [1:0] response_payload_id,
  output logic [7:0] response_payload_data,
  output logic [1:0] mem_request_payload_id,
  output logic [7:0] mem_request_payload_data,
  output logic mem_request_valid,
  input wire logic mem_request_ready,
  input wire logic [1:0] mem_response_payload_id,
  input wire logic [7:0] mem_response_payload_data,
  input wire logic mem_response_valid,
  output logic mem_response_ready
);
  // Generated from backend-independent typed ZLang IR.
  logic [9:0] zlang_top_core_response_payload;
  logic [9:0] zlang_top_core_mem_request_payload;
  logic zlang_top_core_mem_request_valid;
  logic zlang_top_core_mem_response_ready;
  RequestClient__zlang_core zlang_top_core (
    .clk(clk),
    .rst(rst),
    .request_payload({request_payload_id, request_payload_data}),
    .issue(issue),
    .accept_response(accept_response),
    .response_payload(zlang_top_core_response_payload),
    .mem_request_payload(zlang_top_core_mem_request_payload),
    .mem_request_valid(zlang_top_core_mem_request_valid),
    .mem_request_ready(mem_request_ready),
    .mem_response_payload({mem_response_payload_id, mem_response_payload_data}),
    .mem_response_valid(mem_response_valid),
    .mem_response_ready(zlang_top_core_mem_response_ready)
  );
  assign response_payload_id = zlang_top_core_response_payload[9:8];
  assign response_payload_data = zlang_top_core_response_payload[7:0];
  assign mem_request_payload_id = zlang_top_core_mem_request_payload[9:8];
  assign mem_request_payload_data = zlang_top_core_mem_request_payload[7:0];
  assign mem_request_valid = zlang_top_core_mem_request_valid;
  assign mem_response_ready = zlang_top_core_mem_response_ready;
endmodule
`default_nettype wire
