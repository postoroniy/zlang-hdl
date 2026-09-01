"""Compiler-shipped inputs for the installed backend-comparison command.

These small standalone designs mirror the repository examples used by the
historical experiment.  Keeping the bounded corpus in the Python distribution
makes ``zlang-compare-backends`` independent of a source checkout.
"""

BACKEND_COMPARISON_SOURCES = {
    "examples/alu.zl": """module ALU {
    in a  : u32
    in b  : u32
    in op : u3

    out y : u32

    y = switch op {
        0 => truncate<32>(a + b)
        1 => a - b
        2 => a & b
        3 => a | b
        else => 0
    }
}
""",
    "examples/pipelined_mac.zl": """module PipelinedMAC {
    clock clk
    reset rst

    in a  : u8
    in b  : u8
    in c  : u16
    out y : u17

    y = pipeline(2) {
        a * b + c
    }
}
""",
    "examples/rv_passthrough.zl": """module RvPassthrough {
  in rx: rv<u8>
  out tx: rv<u8>

  tx.payload = rx.payload
  tx.valid = rx.valid
  rx.ready = tx.ready
}
""",
    "examples/credit_source.zl": """module CreditSource {
  clock clk
  reset rst

  in payload_data: u8
  in request: bit
  out tx: credit<u8, 2>

  tx.payload = payload_data
  tx.send = request
}
""",
    "examples/control_csr.zl": """module ControlCsr {
  clock clk
  reset rst

  csr control @ 0x4000_0000 {
    CONTROL @ 0x00 {
      enable bit @0 rw = 0
      mode u3 @3:1 rw = 0
      start bit @4 pulse
      command u3 @7:5 wo = 0
      reserved0 bits<24> @31:8 reserved
    }

    STATUS @ 0x04 {
      busy bit @0 ro = 1
      error bit @1 w1c = 1
      reserved1 bits<30> @31:2 reserved
    }
  }
}
""",
    "examples/request_client.zl": """struct Request {
  id: u2
  data: u8
}

struct Response {
  id: u2
  data: u8
}

module RequestClient {
  clock clk
  reset rst

  interface mem: request_response<Request, Response> {
    max_outstanding 2
    ordering out_of_order
    match_by id
  }

  in request_payload: Request
  in issue: bit
  in accept_response: bit
  out response_payload: Response

  mem.request.payload = request_payload
  mem.request.valid = issue
  mem.response.ready = accept_response
  response_payload = mem.response.payload
}
""",
    "examples/rule_counter.zl": """module RuleCounter {
  clock clk
  reset rst

  in increment: bit
  in clear: bit
  out count_out: u8

  reg count: u8 = 0

  priority {
    clear_count: when clear {
      count <- 0
    }

    increment_count: when increment {
      count <- truncate<8>(count + 1)
    }
  }

  count_out = count
}
""",
}

__all__ = ["BACKEND_COMPARISON_SOURCES"]
