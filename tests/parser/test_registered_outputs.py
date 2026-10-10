from __future__ import annotations

from zlang.ast import Direction, OutputDrive
from zlang.parser import parse


def test_registered_output_and_drive_preserve_explicit_syntax_intent() -> None:
    module = parse(
        "module M { clock clk reset rst in load:bit in value:s8 "
        "out reg held:s8 = -1 out fired:bit "
        "when load { held <- value drive fired = 1 } }"
    )

    held, fired = tuple(
        port for port in module.ports if port.direction is Direction.OUTPUT
    )
    assert held.name == "held" and held.registered
    assert held.initializer is not None
    assert fired.name == "fired" and not fired.registered
    assert isinstance(module.rules[0].actions[1], OutputDrive)
    assert module.rules[0].actions[1].target == "fired"
