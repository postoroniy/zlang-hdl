// SPDX-License-Identifier: Apache-2.0

reg [1:0] queue[0:3];reg [2:0] written,consumed,rs1,rs2,ws1,ws2;
reg full_ref,valid_ref;reg [1:0] data_ref;
wire push_ref=source_valid && !source_reset && !full_ref;
wire pop_ref=destination_ready && !destination_reset && valid_ref;
wire [2:0] next_w=written+push_ref;
wire [2:0] next_r=consumed+pop_ref;
wire slot_open=!valid_ref || destination_ready;
wire fetch_ref=slot_open && !destination_reset && next_r!=ws2;
wire [2:0] d_wr=written-consumed,d_rr1=consumed-rs1,d_r12=rs1-rs2,d_wr2=written-rs2;
wire [2:0] d_ww1=written-ws1,d_w12=ws1-ws2,d_w2r=ws2-consumed;
reg started=0;
always @(posedge source_clock) begin
 if(!started) assume(source_reset && destination_reset);started<=1;
 if(source_reset) begin written<=0;rs1<=0;rs2<=0;full_ref<=0;end
 else begin
  rs1<=consumed;rs2<=rs1;
  if(push_ref) queue[written[1:0]]<=source_payload;
  written<=next_w;full_ref<=(3'(next_w-rs2)==3'd4);
 end
end
always @(posedge destination_clock) begin
 if(destination_reset) begin consumed<=0;ws1<=0;ws2<=0;valid_ref<=0;data_ref<=0;end
 else begin
  consumed<=next_r;ws1<=written;ws2<=ws1;
  if(slot_open) valid_ref<=fetch_ref;
  if(fetch_ref) data_ref<=queue[next_r[1:0]];
 end
end
always @* if(started) begin
 assert(source_ready==(!source_reset && !full_ref));
 assert(destination_valid==(!destination_reset && valid_ref));
 assert(destination_payload==data_ref);
 assert(OBS_W==written);assert(OBS_R==consumed);
 assert(OBS_WG==((written>>1)^written));assert(OBS_RG==((consumed>>1)^consumed));
 assert(OBS_RS1==((rs1>>1)^rs1));assert(OBS_RS2==((rs2>>1)^rs2));
 assert(OBS_WS1==((ws1>>1)^ws1));assert(OBS_WS2==((ws2>>1)^ws2));
 assert(OBS_FULL==full_ref);assert(OBS_VALID==valid_ref);
 assert(3'(written-consumed)<=4);
 assert(d_wr2<=4);
 assert({2'b0,d_wr}+{2'b0,d_rr1}+{2'b0,d_r12}=={2'b0,d_wr2});
 assert({2'b0,d_ww1}+{2'b0,d_w12}+{2'b0,d_w2r}=={2'b0,d_wr});
 if(!full_ref) assert(d_wr2<4);
 if(valid_ref) assert(d_w2r>0);
 if(valid_ref) begin assert(written!=consumed);assert(data_ref==queue[consumed[1:0]]);end
 for(integer i=0;i<4;i=i+1)
  if(2'(i-consumed[1:0])<3'(written-consumed)) assert(OBS_CELLS[i]==queue[i]);
end
