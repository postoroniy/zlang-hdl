from pathlib import Path
import unittest

from zlang.ir.expressions import RequestResponseRef
from zlang.ir.interfaces import (
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseOrdering,
)
from zlang.ir.types import UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class RequestResponseSemanticTests(unittest.TestCase):
    def test_outstanding_and_match_model_survive_in_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/request_client.zl").read_text()))
        interface = module.request_responses[0]
        self.assertEqual(interface.max_outstanding, 2)
        self.assertEqual(interface.ordering, RequestResponseOrdering.OUT_OF_ORDER)
        self.assertEqual(interface.match_by, "id")
        self.assertEqual(interface.id_type, UIntType(2))
        self.assertEqual(
            [
                (assignment.channel, assignment.signal)
                for assignment in module.assignments[:3]
            ],
            [
                (RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD),
                (RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
                (RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
            ],
        )
        response_ref = module.assignments[3].expression
        self.assertIsInstance(response_ref, RequestResponseRef)
        self.assertEqual(response_ref.channel, RequestResponseChannel.RESPONSE)
        self.assertEqual(response_ref.signal, ReadyValidSignal.PAYLOAD)

    def test_out_of_order_matching_requires_compatible_struct_id_fields(self) -> None:
        cases = (
            (
                "module Bad { clock c reset r interface mem:"
                "request_response<u8,u8>{max_outstanding 2 ordering "
                "out_of_order match_by id} mem.request.payload=0 "
                "mem.request.valid=0 mem.response.ready=0 }",
                "requires struct request and response payloads",
            ),
            (
                "struct Q { tag:u2 } struct S { tag:u3 } module Bad { "
                "clock c reset r interface mem:request_response<Q,S>{"
                "max_outstanding 2 ordering out_of_order match_by tag} "
                "mem.request.payload=0 mem.request.valid=0 mem.response.ready=0 }",
                "different request and response types",
            ),
            (
                "struct Q { id:u2 } struct S { other:u2 } module Bad { "
                "clock c reset r interface mem:request_response<Q,S>{"
                "max_outstanding 2 ordering out_of_order match_by id} "
                "mem.request.payload=0 mem.request.valid=0 mem.response.ready=0 }",
                "must exist in both request and response payloads",
            ),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_ordering_and_clock_configuration_are_checked(self) -> None:
        with self.assertRaisesRegex(SemanticError, "requires match_by"):
            analyze(
                parse(
                    "struct P { id:u2 } module Bad { clock c reset r "
                    "interface mem:request_response<P,P>{max_outstanding 2 "
                    "ordering out_of_order} mem.request.payload=0 "
                    "mem.request.valid=0 mem.response.ready=0 }"
                )
            )
        with self.assertRaisesRegex(SemanticError, "must not use match_by"):
            analyze(
                parse(
                    "struct P { id:u2 } module Bad { clock c reset r "
                    "interface mem:request_response<P,P>{max_outstanding 2 "
                    "ordering in_order match_by id} mem.request.payload=0 "
                    "mem.request.valid=0 mem.response.ready=0 }"
                )
            )
        with self.assertRaisesRegex(
            SemanticError, "require a module clock and reset"
        ):
            analyze(
                parse(
                    "module Bad { interface mem:request_response<u8,u8>{"
                    "max_outstanding 2 ordering in_order} "
                    "mem.request.payload=0 mem.request.valid=0 "
                    "mem.response.ready=0 }"
                )
            )

    def test_owned_fields_and_required_assignments_are_checked(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "cannot drive incoming request/response field"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r interface mem:"
                    "request_response<u8,u8>{max_outstanding 2 ordering in_order} "
                    "mem.request.payload=0 mem.request.valid=0 "
                    "mem.request.ready=0 mem.response.ready=0 }"
                )
            )
        with self.assertRaisesRegex(
            SemanticError, "mem.response.ready.*has no assignment"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r interface mem:"
                    "request_response<u8,u8>{max_outstanding 2 ordering in_order} "
                    "mem.request.payload=0 mem.request.valid=0 }"
                )
            )
        with self.assertRaisesRegex(SemanticError, "transfer.*read-only"):
            analyze(
                parse(
                    "module Bad { clock c reset r interface mem:"
                    "request_response<u8,u8>{max_outstanding 2 ordering in_order} "
                    "mem.request.payload=0 mem.request.valid=0 "
                    "mem.response.ready=0 mem.request.transfer=0 }"
                )
            )

    def test_request_response_dependency_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "combinational request/response dependency cycle"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r interface mem:"
                    "request_response<u8,u8>{max_outstanding 2 ordering in_order} "
                    "mem.request.payload=0 "
                    "mem.request.valid=mem.request.transfer "
                    "mem.response.ready=0 }"
                )
            )


if __name__ == "__main__":
    unittest.main()
