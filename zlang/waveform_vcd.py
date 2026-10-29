# SPDX-License-Identifier: Apache-2.0
"""Shared deterministic VCD identifier allocation, independent of simulators."""


def identifier(index: int) -> str:
    alphabet = tuple(chr(item) for item in range(33, 127))
    result = ""
    value = index
    while True:
        result += alphabet[value % len(alphabet)]
        value //= len(alphabet)
        if value == 0:
            return result
