"""Data-driven physical-ABI event playback; no duplicated simulation semantics.

The SV program is O(ABI width), not O(events × output count). A reviewed
hex stimulus file carries update masks/values and coincident-edge masks.
"""

from collections.abc import Mapping,Sequence
from dataclasses import dataclass

from zlang.backend.identifiers import rtl_identifier
from zlang.ir.module import Module,PortDirection


STIMULUS_FILE = 'zlang-compare-events.hex'


@dataclass(frozen=True)
class ComparisonTestbench:
    """Generated executable testbench and its exact, hashed input data."""

    text: str
    stimulus: str


def _domain_levels(module):
    clocks,resets = {},{}
    for domain in module.clock_domains:
        clocks[domain.clock] = int(domain.edge.value == 'falling')
        if domain.reset is not None:
            levels = (0,1) if domain.reset_polarity.value == 'active_low' else (1,0)
            if resets.setdefault(domain.reset,levels) != levels:
                raise ValueError(f"reset '{domain.reset}' has inconsistent polarity")
    if module.clock is not None:
        clocks.setdefault(module.clock,0)
    if module.reset is not None:
        resets.setdefault(module.reset,(1,0))
    return clocks,resets


def render_testbench(module: Module,rtl_module: str,
                     events: Sequence[Mapping[str,object]]) -> ComparisonTestbench:
    leaves = module.top_physical_abi.leaves
    inputs = {leaf.external_name:leaf for leaf in leaves if leaf.direction is PortDirection.INPUT}
    outputs = tuple(leaf for leaf in leaves if leaf.direction is PortDirection.OUTPUT)
    clocks,resets = _domain_levels(module)
    used = {rtl_identifier(leaf.external_name) for leaf in leaves}
    def name(stem):
        result = stem
        while result in used:
            result += '_'
        used.add(result)
        return result
    data,index = name('zlang_compare_stimulus'),name('zlang_compare_index')
    offsets,edge_offsets,offset = {},{},0
    for key,leaf in inputs.items():
        offsets[key] = offset
        offset += leaf.width+1
    for key in sorted(clocks):
        edge_offsets[key] = offset
        offset += 1
    width = max(1,offset)
    encoded = []
    for event in events:
        updates = dict(event['set'])
        for key,asserted in sorted(event['reset'].items()):
            if not isinstance(asserted,bool):
                raise ValueError('reset event values must be boolean')
            if key not in resets:
                raise ValueError(f"unknown reset '{key}'")
            updates[key] = resets[key][0 if asserted else 1]
        word = 0
        for key,value in updates.items():
            if key not in inputs:
                raise ValueError(f"physical event update '{key}' is not an input")
            leaf,start = inputs[key],offsets[key]
            word |= (((int(value) & ((1 << leaf.width)-1)) << 1)|1) << start
        selected = tuple(event['edges'])
        if len(selected) != len(set(selected)):
            raise ValueError('one event cannot contain a clock twice')
        for key in selected:
            if key not in clocks:
                raise ValueError(f"unknown clock '{key}'")
            word |= 1 << edge_offsets[key]
        encoded.append(f'{word:0{(width+3)//4}x}')
    lines = ['`timescale 1ns/1ps','module zlang_compare_tb;']
    for leaf in leaves:
        packed = '' if leaf.width == 1 else f' [{leaf.width-1}:0]'
        lines.append(f'  logic{packed} {rtl_identifier(leaf.external_name)};')
    lines.extend((f'  logic [{width-1}:0] {data} [0:{max(1,len(events))-1}];',
                  f'  integer {index};',f'  {rtl_module} dut ('))
    for i,leaf in enumerate(leaves):
        key = rtl_identifier(leaf.external_name)
        lines.append(f'    .{key}({key})'+(',' if i+1 < len(leaves) else ''))
    lines.extend(('  );','  initial begin',f'    $readmemh("{STIMULUS_FILE}", {data});'))
    for leaf in inputs.values():
        lines.append(f"    {rtl_identifier(leaf.external_name)} = {leaf.width}'h0;")
    for key,inactive in sorted(clocks.items()):
        lines.append(f"    {rtl_identifier(key)} = 1'b{inactive};")
    for key,(_,inactive) in sorted(resets.items()):
        lines.append(f"    {rtl_identifier(key)} = 1'b{inactive};")
    lines.extend(('    #1;',f'    for ({index}=0; {index}<{len(events)}; {index}={index}+1) begin'))
    for key,leaf in inputs.items():
        start = offsets[key]
        lines.append(f'      if ({data}[{index}][{start}]) {rtl_identifier(key)} = '
                     f'{data}[{index}][{start+1} +: {leaf.width}];')
    lines.append('      #1;')
    if edge_offsets:
        mask = ' || '.join(f'{data}[{index}][{bit}]' for bit in edge_offsets.values())
        lines.append(f'      if ({mask}) begin')
        for key,bit in edge_offsets.items():
            lines.append(f"        if ({data}[{index}][{bit}]) {rtl_identifier(key)} = 1'b{1-clocks[key]};")
        lines.append('        #1;')
        for key,bit in edge_offsets.items():
            lines.append(f"        if ({data}[{index}][{bit}]) {rtl_identifier(key)} = 1'b{clocks[key]};")
        lines.extend(('        #1;','      end'))
    for leaf in outputs:
        key = rtl_identifier(leaf.external_name)
        lines.extend((f"      if ((^{key}) === 1'bx)",
                      f'        $display("sample %0d {leaf.external_name} X", {index});',
                      '      else',
                      f'        $display("sample %0d {leaf.external_name} %0h", {index}, {key});'))
    lines.extend(('    end','    $finish;','  end','endmodule',''))
    return ComparisonTestbench('\n'.join(lines),'\n'.join(encoded or ['0'])+'\n')
