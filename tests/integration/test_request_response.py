from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from zlang.compiler import compile_source
from zlang.simulate import (
    ProtocolViolation,
    simulate_request_response_cycles,
)


ROOT = Path(__file__).resolve().parents[2]


def rr_boundary(
    response_payload: object,
    response_valid: int = 0,
    request_ready: int = 1,
) -> dict[str, object]:
    return {
        "request": {"ready": request_ready},
        "response": {"payload": response_payload, "valid": response_valid},
    }


class RequestResponseIntegrationTests(unittest.TestCase):
    def test_in_order_limit_response_sequence_and_reset(self) -> None:
        source = (
            "module Client { clock clk reset rst interface mem:"
            "request_response<u8,u16>{max_outstanding 2 ordering in_order} "
            "in req:u8 in issue:bit in accept:bit out response:u16 "
            "mem.request.payload=req mem.request.valid=issue "
            "mem.response.ready=accept response=mem.response.payload }"
        )
        module = compile_source(source).ir
        cycles = [
            {"req": 1, "issue": 1, "accept": 1, "mem": rr_boundary(0)},
            {"req": 2, "issue": 1, "accept": 1, "mem": rr_boundary(0)},
            {"req": 3, "issue": 1, "accept": 1, "mem": rr_boundary(0)},
            {
                "req": 0,
                "issue": 0,
                "accept": 1,
                "mem": rr_boundary(100, response_valid=1),
            },
            {
                "req": 0,
                "issue": 0,
                "accept": 1,
                "mem": rr_boundary(101, response_valid=1),
            },
            {"req": 4, "issue": 1, "accept": 1, "mem": rr_boundary(0)},
        ]
        results = simulate_request_response_cycles(
            module, cycles, reset=[False, False, False, False, False, True]
        )
        self.assertEqual(
            [result["mem"]["request"]["valid"] for result in results],
            [1, 1, 0, 0, 0, 0],
        )
        self.assertEqual(
            [result["mem"]["outstanding"] for result in results],
            [0, 1, 2, 2, 1, 0],
        )
        self.assertEqual([results[3]["response"], results[4]["response"]], [100, 101])

    def test_out_of_order_matching_accepts_reversed_response_order(self) -> None:
        module = compile_source(
            (ROOT / "examples/request_client.zhl").read_text()
        ).ir
        def cycle(
            request_id: int,
            issue: int = 1,
            response_id: int = 0,
            response_valid: int = 0,
        ) -> dict[str, object]:
            return {
                "request_payload": {"id": request_id, "data": request_id + 10},
                "issue": issue,
                "accept_response": 1,
                "mem": rr_boundary(
                    {"id": response_id, "data": response_id + 20},
                    response_valid=response_valid,
                ),
            }

        results = simulate_request_response_cycles(
            module,
            [
                cycle(1),
                cycle(2),
                cycle(0, issue=0, response_id=2, response_valid=1),
                cycle(0, issue=0, response_id=1, response_valid=1),
            ],
        )
        self.assertEqual(
            [result["mem"]["response"]["transfer"] for result in results],
            [0, 0, 1, 1],
        )
        self.assertEqual(results[-1]["mem"]["outstanding"], 1)

    def test_duplicate_and_unknown_ids_are_protocol_violations(self) -> None:
        module = compile_source(
            (ROOT / "examples/request_client.zhl").read_text()
        ).ir
        base = {
            "accept_response": 1,
            "mem": rr_boundary({"id": 0, "data": 0}),
        }
        first = {
            **base,
            "request_payload": {"id": 1, "data": 1},
            "issue": 1,
        }
        duplicate = {
            **base,
            "request_payload": {"id": 1, "data": 2},
            "issue": 1,
        }
        with self.assertRaisesRegex(ProtocolViolation, "duplicate outstanding ID 1"):
            simulate_request_response_cycles(module, [first, duplicate])

        unknown = {
            "request_payload": {"id": 0, "data": 0},
            "issue": 0,
            "accept_response": 1,
            "mem": rr_boundary(
                {"id": 2, "data": 2}, response_valid=1
            ),
        }
        with self.assertRaisesRegex(ProtocolViolation, "non-outstanding ID 2"):
            simulate_request_response_cycles(module, [first, unknown])



if __name__ == "__main__":
    unittest.main()
