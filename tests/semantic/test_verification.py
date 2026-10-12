from pathlib import Path
import unittest

from zlang.ir.expressions import Binary, InputRef
from zlang.ir.types import BitType
from zlang.ir.verification import ContractKind
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class VerificationSemanticTests(unittest.TestCase):
    def test_contracts_are_typed_and_tied_to_output_symbols(self) -> None:
        module = analyze(parse((ROOT / "examples/contracted_add.zhl").read_text()))
        assumption, guarantee = module.contracts

        self.assertEqual(assumption.kind, ContractKind.ASSUME)
        self.assertEqual(guarantee.kind, ContractKind.GUARANTEE)
        self.assertEqual((guarantee.clock, guarantee.reset), ("clk", "rst"))
        self.assertEqual(guarantee.expression.type, BitType())
        self.assertIsInstance(guarantee.expression, Binary)
        self.assertIsInstance(guarantee.expression.left, InputRef)
        self.assertEqual(guarantee.expression.left.name, "y")

    def test_contract_expression_must_be_bit(self) -> None:
        with self.assertRaisesRegex(SemanticError, "expression must be bit"):
            analyze(
                parse(
                    "module Bad { clock c reset r in a:u8 out y:u8 y=a "
                    "guarantee value @ c disable iff r { y } }"
                )
            )

    def test_contract_clock_and_associated_reset_must_exist(self) -> None:
        cases = (
            (
                "module Bad { clock c reset r in a:bit out y:bit y=a "
                "guarantee g @ missing disable iff r { y == a } }",
                "unknown clock 'missing'",
            ),
            (
                "module Bad { clock c reset r in a:bit out y:bit y=a "
                "guarantee g @ c disable iff wrong { y == a } }",
                "must use reset 'r'",
            ),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_contract_rejects_cross_domain_references(self) -> None:
        with self.assertRaisesRegex(SemanticError, "references signal in domain 'b'"):
            analyze(
                parse(
                    "module Bad { clock a reset ra @ a clock b reset rb @ b "
                    "in x:bit @ b out y:bit @ a y=0 "
                    "guarantee g @ a disable iff ra { x == 0 } }"
                )
            )

    def test_duplicate_contract_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(SemanticError, "duplicate contract 'same'"):
            analyze(
                parse(
                    "module Bad { clock c reset r in a:bit out y:bit y=a "
                    "assume same @ c disable iff r { a } "
                    "guarantee same @ c disable iff r { y } }"
                )
            )

    def test_internal_state_and_temporal_forms_fail_explicitly(self) -> None:
        cases = (
            (
                "module Bad { clock c reset r in a:bit out y:bit reg q:bit=0 "
                "q <- a y=q guarantee g @ c disable iff r { q == y } }",
                "cannot yet bind internal register 'q'",
            ),
            (
                "module Bad { clock c reset r in a:bit out y:bit y=a "
                "guarantee g @ c disable iff r { delay<1>(a) } }",
                "does not support temporal expressions",
            ),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_assumptions_may_not_constrain_wire_outputs(self) -> None:
        source = (
            "module Bad { clock c reset r in a:bit out y:bit y=a "
            "assume bad @ c disable iff r { y } }"
        )
        with self.assertRaisesRegex(
            SemanticError, "assumptions may constrain environment-owned inputs only"
        ):
            analyze(parse(source))

    def test_ready_valid_assumption_ownership_is_directional(self) -> None:
        accepted = (
            "module Good { clock c reset r in rx:rv<u8> out tx:rv<u8> "
            "connect rx -> tx { buffer 1 } "
            "assume incoming @ c disable iff r { rx.valid & tx.ready } }"
        )
        analyze(parse(accepted))
        rejected = (
            "module Bad { clock c reset r in rx:rv<u8> out tx:rv<u8> "
            "connect rx -> tx { buffer 1 } "
            "assume drives_dut @ c disable iff r { rx.ready | tx.valid } }"
        )
        with self.assertRaisesRegex(SemanticError, "implementation-owned"):
            analyze(parse(rejected))
        transfer = (
            "module Bad { clock c reset r in rx:rv<u8> out tx:rv<u8> "
            "connect rx -> tx { buffer 1 } "
            "assume transfer @ c disable iff r { rx.transfer } }"
        )
        with self.assertRaisesRegex(
            SemanticError, "combines environment- and implementation-owned"
        ):
            analyze(parse(transfer))

    def test_credit_and_vc_credit_assumption_ownership_is_directional(self) -> None:
        credit = (
            "module Good { clock c reset r in data:u8 in send:bit "
            "out tx:credit<u8,2> tx.payload=data tx.send=send "
            "assume returned @ c disable iff r { tx.return } }"
        )
        analyze(parse(credit))
        with self.assertRaisesRegex(SemanticError, "implementation-owned"):
            analyze(parse(credit.replace("tx.return", "tx.send")))

        vc = (
            "module Good { clock c reset r in data:u8 in channel:u1 in send:bit "
            "out tx:vc_credit<u8,2,2> tx.payload=data tx.vc=channel tx.send=send "
            "assume returned @ c disable iff r { tx.return } }"
        )
        analyze(parse(vc))
        with self.assertRaisesRegex(SemanticError, "implementation-owned"):
            analyze(parse(vc.replace("tx.return", "tx.send")))

    def test_packet_assumption_ownership_is_directional(self) -> None:
        prefix = (
            "module Good { clock c reset r in high:packet<u8> "
            "in low:packet<u8> out tx:packet<u8> "
            "arbiter [high,low] -> tx { policy fixed_priority grant packet } "
        )
        analyze(parse(prefix +
            "assume offered @ c disable iff r { high.valid & tx.ready } }"))
        with self.assertRaisesRegex(SemanticError, "implementation-owned"):
            analyze(parse(prefix +
                "assume driven @ c disable iff r { high.ready | tx.valid } }"))

    def test_request_response_assumption_ownership_uses_inferred_role(self) -> None:
        prefix = (
            "struct Req { data:u8 } struct Rsp { data:u8 } "
            "module Good { clock c reset r "
            "interface mem:request_response<Req,Rsp> { "
            "max_outstanding 1 ordering in_order } "
            "in req:Req in issue:bit in consume:bit out rsp:Rsp "
            "mem.request.payload=req mem.request.valid=issue "
            "mem.response.ready=consume rsp=mem.response.payload "
        )
        analyze(parse(prefix +
            "assume accepted @ c disable iff r { mem.request.ready } }"))
        with self.assertRaisesRegex(SemanticError, "implementation-owned"):
            analyze(parse(prefix +
                "assume driven @ c disable iff r { mem.request.valid } }"))
        with self.assertRaisesRegex(
            SemanticError, "combines environment- and implementation-owned"
        ):
            analyze(parse(prefix +
                "assume transferred @ c disable iff r { mem.request.transfer } }"))


if __name__ == "__main__":
    unittest.main()
