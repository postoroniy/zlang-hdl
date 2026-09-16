`default_nettype none
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
  // Compiler-generated inline top boundary.
  logic [9:0] zlang_packed_request_payload;
  logic [9:0] zlang_packed_response_payload;
  logic [9:0] zlang_packed_mem_request_payload;
  logic zlang_packed_mem_request_valid;
  logic zlang_packed_mem_request_ready;
  logic [9:0] zlang_packed_mem_response_payload;
  logic zlang_packed_mem_response_valid;
  logic zlang_packed_mem_response_ready;
  assign zlang_packed_request_payload = {request_payload_id, request_payload_data};
  assign zlang_packed_mem_request_ready = mem_request_ready;
  assign zlang_packed_mem_response_payload = {mem_response_payload_id, mem_response_payload_data};
  assign zlang_packed_mem_response_valid = mem_response_valid;
  logic [1:0] zlang_packed_mem_outstanding;
  logic [1:0] zlang_packed_mem_ids [0:1];
  logic [1:0] zlang_packed_mem_ids_next [0:1];
  logic [1:0] zlang_packed_mem_ids_valid, zlang_packed_mem_ids_valid_next;
  logic zlang_packed_mem_duplicate, zlang_packed_mem_missing;
  logic zlang_packed_mem_request_id_present, zlang_packed_mem_response_id_present;
  logic zlang_packed_mem_request_transfer, zlang_packed_mem_response_transfer;
  integer zlang_scan;
  integer zlang_next;
  integer zlang_state;
  logic zlang_inserted;
  assign zlang_packed_mem_request_payload = zlang_packed_request_payload;
  assign zlang_packed_response_payload = zlang_packed_mem_response_payload;
  always_comb begin
    zlang_packed_mem_request_id_present = 1'b0;
    zlang_packed_mem_response_id_present = 1'b0;
    for (zlang_scan = 0; zlang_scan < 2; zlang_scan = zlang_scan + 1) begin
      if (zlang_packed_mem_ids_valid[zlang_scan] && zlang_packed_mem_ids[zlang_scan] == zlang_packed_request_payload[9:8]) zlang_packed_mem_request_id_present = 1'b1;
      if (zlang_packed_mem_ids_valid[zlang_scan] && zlang_packed_mem_ids[zlang_scan] == zlang_packed_mem_response_payload[9:8]) zlang_packed_mem_response_id_present = 1'b1;
    end
    zlang_packed_mem_duplicate = !rst && issue && zlang_packed_mem_request_ready && (zlang_packed_mem_outstanding < 2'd2) && zlang_packed_mem_request_id_present;
    zlang_packed_mem_missing = !rst && accept_response && zlang_packed_mem_response_valid && (zlang_packed_mem_outstanding != '0) && !zlang_packed_mem_response_id_present;
  end
  assign zlang_packed_mem_request_valid = !rst && (zlang_packed_mem_outstanding < 2'd2) && !zlang_packed_mem_duplicate && issue;
  assign zlang_packed_mem_response_ready = !rst && (zlang_packed_mem_outstanding != '0) && !zlang_packed_mem_missing && accept_response;
  assign zlang_packed_mem_request_transfer = zlang_packed_mem_request_valid && zlang_packed_mem_request_ready;
  assign zlang_packed_mem_response_transfer = zlang_packed_mem_response_valid && zlang_packed_mem_response_ready;
  always_comb begin
    zlang_packed_mem_ids_valid_next = zlang_packed_mem_ids_valid;
    for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) zlang_packed_mem_ids_next[zlang_next] = zlang_packed_mem_ids[zlang_next];
    if (zlang_packed_mem_response_transfer) begin
      for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) begin
        if (zlang_packed_mem_ids_valid_next[zlang_next] && zlang_packed_mem_ids_next[zlang_next] == zlang_packed_mem_response_payload[9:8]) zlang_packed_mem_ids_valid_next[zlang_next] = 1'b0;
      end
    end
    zlang_inserted = 1'b0;
    if (zlang_packed_mem_request_transfer) begin
      for (zlang_next = 0; zlang_next < 2; zlang_next = zlang_next + 1) begin
        if (!zlang_inserted && !zlang_packed_mem_ids_valid_next[zlang_next]) begin
          zlang_packed_mem_ids_next[zlang_next] = zlang_packed_request_payload[9:8];
          zlang_packed_mem_ids_valid_next[zlang_next] = 1'b1;
          zlang_inserted = 1'b1;
        end
      end
    end
  end
  always_ff @(posedge clk) begin
    if (rst) begin
      zlang_packed_mem_outstanding <= '0;
      zlang_packed_mem_ids_valid <= '0;
      for (zlang_state = 0; zlang_state < 2; zlang_state = zlang_state + 1) zlang_packed_mem_ids[zlang_state] <= '0;
    end else begin
      case ({zlang_packed_mem_request_transfer, zlang_packed_mem_response_transfer})
        2'b10: if (zlang_packed_mem_outstanding < 2'd2) zlang_packed_mem_outstanding <= zlang_packed_mem_outstanding + 1'b1;
        2'b01: if (zlang_packed_mem_outstanding != '0) zlang_packed_mem_outstanding <= zlang_packed_mem_outstanding - 1'b1;
        default: zlang_packed_mem_outstanding <= zlang_packed_mem_outstanding;
      endcase
      zlang_packed_mem_ids_valid <= zlang_packed_mem_ids_valid_next;
      for (zlang_state = 0; zlang_state < 2; zlang_state = zlang_state + 1) zlang_packed_mem_ids[zlang_state] <= zlang_packed_mem_ids_next[zlang_state];
    end
  end
  assign response_payload_id = zlang_packed_response_payload[9:8];
  assign response_payload_data = zlang_packed_response_payload[7:0];
  assign mem_request_payload_id = zlang_packed_mem_request_payload[9:8];
  assign mem_request_payload_data = zlang_packed_mem_request_payload[7:0];
  assign mem_request_valid = zlang_packed_mem_request_valid;
  assign mem_response_ready = zlang_packed_mem_response_ready;
endmodule
`default_nettype wire
