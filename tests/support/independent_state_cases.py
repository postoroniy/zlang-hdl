# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Independent conditional register owners for scheduler scaling regressions."""


def source(groups=13, lanes=3):
    count = groups * lanes
    outputs = "\n".join(f"out reg q{i}:u2=0" for i in range(count))
    rules = []
    for group in range(groups):
        actions = "\n".join(
            f"when active[{i}] {{q{i}<-truncate<2>(q{i}+step)}}"
            for i in range(group * lanes, (group + 1) * lanes)
        )
        rules.append(f"rule owner{group:02} when guards[{group}] {{{actions}}}")
    return f"""module IndependentOwners {{
        clock clk reset rst
        in guards:bits<{groups}> in active:bits<{count}> in step:u2
        {outputs}
        {' '.join(rules)}
    }}"""
