from pathlib import Path
import unittest

from zlang.ast.nodes import RequestResponseOrdering
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class RequestResponseParserTests(unittest.TestCase):
    def test_out_of_order_interface_options_and_nested_targets_parse(self) -> None:
        module = parse((ROOT / "examples/request_client.zl").read_text())
        interface = module.request_responses[0]
        self.assertEqual(interface.name, "mem")
        self.assertEqual(interface.request_type.text, "Request")
        self.assertEqual(interface.response_type.text, "Response")
        self.assertEqual(interface.max_outstanding, 2)
        self.assertEqual(interface.ordering, RequestResponseOrdering.OUT_OF_ORDER)
        self.assertEqual(interface.match_by, "id")
        self.assertEqual(
            [assignment.target for assignment in module.assignments[:3]],
            [
                "mem.request.payload",
                "mem.request.valid",
                "mem.response.ready",
            ],
        )

    def test_in_order_interface_omits_match_field(self) -> None:
        module = parse(
            "module Client { clock c reset r interface mem:"
            "request_response<u8,u16> { max_outstanding 4 ordering in_order } "
            "mem.request.payload=0 mem.request.valid=0 mem.response.ready=0 }"
        )
        self.assertEqual(
            module.request_responses[0].ordering,
            RequestResponseOrdering.IN_ORDER,
        )
        self.assertIsNone(module.request_responses[0].match_by)

    def test_interface_requires_limit_and_ordering_syntax(self) -> None:
        with self.assertRaisesRegex(ParseError, "syntax error"):
            parse(
                "module Bad { interface mem:request_response<u8,u8> { "
                "ordering in_order } }"
            )


if __name__ == "__main__":
    unittest.main()
