#!/usr/bin/env python3
"""Detect substantial copied Python function bodies in compiler sources.

The release gate rejects only exact AST-body duplicates.  Alpha-normalized
matches are review hints: they can expose copy/paste with renamed locals, but
they are not correctness evidence because adapters often have parallel shape
and deliberately different contracts.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MIN_BODY_NODES = 60


@dataclass(frozen=True)
class FunctionBody:
    path: Path
    name: str
    line: int
    exact: str
    alpha: str

    @property
    def location(self) -> str:
        return f"{self.path.as_posix()}:{self.line}:{self.name}"


class _AlphaNormalizer(ast.NodeTransformer):
    """Normalize local spelling for advisory clone discovery only."""

    def visit_Name(self, node: ast.Name) -> ast.Name:  # noqa: N802
        return ast.copy_location(ast.Name(id="$name", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.arg:
        return ast.copy_location(
            ast.arg(
                arg="$arg",
                annotation=(
                    self.visit(node.annotation)
                    if node.annotation is not None
                    else None
                ),
            ),
            node,
        )


def _body_module(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Module:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    return ast.Module(body=body, type_ignores=[])


def function_bodies(root: Path) -> tuple[FunctionBody, ...]:
    records: list[FunctionBody] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = _body_module(node)
            if sum(1 for _ in ast.walk(body)) < MIN_BODY_NODES:
                continue
            exact = ast.dump(body, annotate_fields=True, include_attributes=False)
            normalized = _AlphaNormalizer().visit(ast.fix_missing_locations(body))
            alpha = ast.dump(
                normalized, annotate_fields=True, include_attributes=False
            )
            records.append(FunctionBody(path, node.name, node.lineno, exact, alpha))
    return tuple(records)


def duplicate_groups(
    records: Iterable[FunctionBody],
    *,
    alpha: bool = False,
) -> tuple[tuple[FunctionBody, ...], ...]:
    grouped: dict[str, list[FunctionBody]] = defaultdict(list)
    for record in records:
        grouped[record.alpha if alpha else record.exact].append(record)
    return tuple(
        tuple(sorted(group, key=lambda item: item.location))
        for _, group in sorted(grouped.items())
        if len(group) > 1
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("zlang"))
    parser.add_argument(
        "--report-alpha",
        action="store_true",
        help="also print advisory alpha-equivalent function groups",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    records = function_bodies(arguments.root)
    exact = duplicate_groups(records)
    for group in exact:
        print("exact duplicate function bodies:")
        for record in group:
            print(f"  {record.location}")
    if arguments.report_alpha:
        exact_locations = {
            tuple(record.location for record in group) for group in exact
        }
        for group in duplicate_groups(records, alpha=True):
            locations = tuple(record.location for record in group)
            if locations in exact_locations:
                continue
            print("advisory alpha-equivalent function bodies:")
            for record in group:
                print(f"  {record.location}")
    return 1 if exact else 0


if __name__ == "__main__":
    raise SystemExit(main())
