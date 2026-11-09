# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Small global-RAM/controller cases independent of external IP sources."""


PROFILES = (
    (0, "read_first", "preserve", "clear"),
    (0, "write_first", "preserve", "preserve"),
    (1, "read_first", "preserve", "clear"),
    (1, "write_first", "clear", "clear"),
    (2, "read_first", "preserve", "preserve"),
    (2, "write_first", "clear", "preserve"),
    (16, "read_first", "preserve", "clear"),
)


def controller_source(latency=1, collision="read_first", contents="preserve",
                      read_data="clear", *, ported=False, parent=False):
    ports = "read_port reader write_port writer" if ported else ""
    controls = ("bank.reader.address=address bank.writer.address=cursor\n"
                "bank.writer.enable=write_enable bank.writer.data=data\n"
                "bank.writer.mask=mask" if ported else
                "bank.read_address=address bank.write_address=cursor\n"
                "bank.write_enable=write_enable bank.write_data=data\n"
                "bank.write_mask=mask")
    observed = "bank.reader.data" if ported else "bank.read_data"
    source = f"""module MemoryController {{
      clock clk reset rst
      in address:u2 in write_enable,step,sample:bit in data:u9 in mask:bits<2>
      out q:u9 out reg cursor:u2=0 out reg seen:u9=0
      memory bank:mem<u9,4> {{
        {ports}
        read_latency {latency} collision {collision}
        reset {{contents {contents} read_data {read_data}}}
      }}
      when step {{cursor <- truncate<2>(cursor+1)}}
      when sample {{seen <- {observed}}}
      {controls}
      q={observed}
    }}"""
    if parent:
        source += """module Top {clock clk reset rst
          in address:u2 in write_enable,step,sample:bit in data:u9 in mask:bits<2>
          out q:u9 out cursor:u2 out seen:u9
          core:MemoryController {address=address write_enable=write_enable
            step=step sample=sample data=data mask=mask}
          q=core.q cursor=core.cursor seen=core.seen
        }"""
    return source
