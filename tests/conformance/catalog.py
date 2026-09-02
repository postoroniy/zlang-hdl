"""Single test-owned catalog for the executable ZLang language tour."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from zlang.ast.nodes import Module
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]
LANGUAGE_TOUR_PATH = ROOT / "examples" / "all_syntax.zhl"

EXPECTED_LANGUAGE_TOUR_TOPS = (
    "ScalarSyntax",
    "FunctionalSyntax",
    "GeneratedSyntax",
    "MappedSyntax",
    "CompileTimeSyntax",
    "ConciseLoweringSyntax",
    "FixedSyntax",
    "StructSyntax",
    "TextTupleSyntax",
    "StateSyntax",
    "AsyncResetSyntax",
    "EncodedEnumSyntax",
    "FsmSyntax",
    "VectorStateUpdateSyntax",
    "FifoSyntax",
    "MemorySyntax",
    "RuleLocalMemorySyntax",
    "ReadyValidSyntax",
    "CreditSyntax",
    "RequestResponseSyntax",
    "AggregateProtocolSyntax",
    "ArbitrationSyntax",
    "CdcSyntax",
    "CsrSyntax",
    "ContractSyntax",
    "ExplorationSyntax",
    "AllSyntax",
)


@cache
def language_tour_source() -> str:
    return LANGUAGE_TOUR_PATH.read_text()


@cache
def language_tour_syntax() -> Module:
    return parse(language_tour_source())


def language_tour_tops() -> tuple[str, ...]:
    syntax = language_tour_syntax()
    return tuple(item.name for item in (*syntax.submodules, syntax))
