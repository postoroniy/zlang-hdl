#!/usr/bin/env python3
"""Report structural Python debt without importing the compiler.

The audit is intentionally syntax-only: it can run against a broken checkout,
does not execute project code, and produces deterministic JSON suitable for
before/after refactor comparison.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Iterable


_IDENTITY_WORDS = ("identity", "digest", "hash", "fingerprint", "key")

# These owners feed published QoR/evidence/cache identities whose current
# spelling is compatibility-bound.  They remain visible in the report and may
# be removed only together with the corresponding evidence/schema rotation.
# Exact owner names make any new repr-based identity fail the architecture
# gate instead of silently broadening an entire module prefix.
INTENTIONAL_REPR_IDENTITY_OWNERS: dict[str, str] = {
    "zlang.architecture:identity": "published architecture identity",
    "zlang.backend.naming:_local_owner_identity": "published RTL naming identity",
    "zlang.backend.systemverilog.emitter:digest": "physical RTL object identity",
    "zlang.costs:_sort_key": "legacy deterministic candidate ordering",
    "zlang.exploration:_semantic_identity": "candidate evidence identity",
    "zlang.formal_exploration:proof_cache_key": "formal proof cache compatibility",
    "zlang.ir.callables:_callable_definition_order_key": "callable ordering compatibility",
    "zlang.ir.scheduled:identity": "published scheduled-graph identity",
    "zlang.ir.scheduled:legacy_identity": "explicit legacy restoration identity",
    "zlang.ir.target:_identity": "published target graph identity",
    "zlang.ir.verification:expression_key": "verification expression compatibility",
    "zlang.ir.verification:verification_identity": "verification artifact compatibility",
    "zlang.pipeline_scheduling:_identity": "packaged QoR graph identity",
    "zlang.reductions:identity": "published reduction candidate identity",
    "zlang.reductions:semantic_identity": "published reduction semantic identity",
    "zlang.target_planner:identity": "packaged QoR evidence identity",
    "zlang.target_timing:_identity": "packaged timing graph identity",
    "zlang.targets:digest": "published target physical identity",
    "zlang.timing:_value_identity": "published timing value identity",
}


def _qualified(module: str, name: str) -> str:
    return f"{module}:{name}"


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root.parent).with_suffix("")
    return ".".join(relative.parts)


def _function_body_key(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = ast.Module(body=node.body, type_ignores=[])
    return ast.dump(body, annotate_fields=True, include_attributes=False)


def _imported_modules(tree: ast.AST, module: str) -> set[str]:
    result: set[str] = set()
    package = module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = package[: max(0, len(package) - node.level)]
                imported = ".".join((*prefix, node.module or ""))
            else:
                imported = node.module or ""
            if imported:
                imported = imported.rstrip(".")
                result.add(imported)
                result.update(
                    f"{imported}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
    return result


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in sorted(graph.get(node, ())):
            if child not in graph:
                continue
            if child not in indices:
                visit(child)
                lowlinks[node] = min(lowlinks[node], lowlinks[child])
            elif child in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[child])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            child = stack.pop()
            on_stack.remove(child)
            component.append(child)
            if child == node:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components)


def audit(root: Path) -> dict[str, object]:
    paths = tuple(sorted(root.rglob("*.py")))
    modules: dict[str, dict[str, object]] = {}
    function_names: dict[str, list[str]] = defaultdict(list)
    class_names: dict[str, list[str]] = defaultdict(list)
    bodies: dict[str, list[str]] = defaultdict(list)
    method_bodies: dict[str, list[str]] = defaultdict(list)
    imports: dict[str, set[str]] = {}
    loaded_names: Counter[str] = Counter()
    private_definitions: list[tuple[str, str, int]] = []
    repr_identity_sites: list[dict[str, object]] = []
    direct_json_dumps: list[str] = []
    traversal_sites: list[str] = []

    for path in paths:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        module = _module_name(root, path)
        functions: list[dict[str, object]] = []
        classes: list[dict[str, object]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines = (node.end_lineno or node.lineno) - node.lineno + 1
                owner = _qualified(module, node.name)
                functions.append({"lines": lines, "name": node.name})
                function_names[node.name].append(owner)
                if len(node.body) > 1 or not isinstance(
                    node.body[0], (ast.Pass, ast.Expr)
                ):
                    bodies[_function_body_key(node)].append(owner)
                if node.name.startswith("_") and not node.name.startswith("__"):
                    private_definitions.append((module, node.name, lines))
            elif isinstance(node, ast.ClassDef):
                lines = (node.end_lineno or node.lineno) - node.lineno + 1
                methods = tuple(
                    member
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                classes.append({
                    "lines": lines,
                    "methods": len(methods),
                    "name": node.name,
                })
                class_names[node.name].append(_qualified(module, node.name))
                for method in methods:
                    if len(method.body) > 1 or not isinstance(
                        method.body[0], (ast.Pass, ast.Expr)
                    ):
                        owner = _qualified(module, f"{node.name}.{method.name}")
                        method_bodies[_function_body_key(method)].append(owner)

        current_function: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                current_function.append(node.name)
                self.generic_visit(node)
                current_function.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load):
                    loaded_names[node.id] += 1

            def visit_Call(self, node: ast.Call) -> None:
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                location = f"{module}:{node.lineno}"
                if name == "dumps" and isinstance(node.func, ast.Attribute):
                    direct_json_dumps.append(location)
                if name in {"expression_children", "walk_expression"}:
                    traversal_sites.append(location)
                if name == "repr" and current_function and any(
                    word in current_function[-1].lower() for word in _IDENTITY_WORDS
                ):
                    owner = _qualified(module, current_function[-1])
                    repr_identity_sites.append({
                        "allowlist_reason": INTENTIONAL_REPR_IDENTITY_OWNERS.get(owner),
                        "line": node.lineno,
                        "owner": owner,
                    })
                self.generic_visit(node)

        Visitor().visit(tree)
        imports[module] = _imported_modules(tree, module)
        modules[module] = {
            "classes": sorted(classes, key=lambda item: (-item["lines"], item["name"])),
            "functions": sorted(
                functions, key=lambda item: (-item["lines"], item["name"])
            ),
            "lines": len(text.splitlines()),
        }

    duplicate_bodies = sorted(
        (sorted(owners) for owners in bodies.values() if len(owners) > 1),
        key=lambda owners: (-len(owners), owners),
    )
    duplicate_method_bodies = sorted(
        (sorted(owners) for owners in method_bodies.values() if len(owners) > 1),
        key=lambda owners: (-len(owners), owners),
    )
    duplicate_function_names = {
        name: sorted(owners)
        for name, owners in sorted(function_names.items())
        if len(owners) > 1
    }
    duplicate_class_names = {
        name: sorted(owners)
        for name, owners in sorted(class_names.items())
        if len(owners) > 1
    }
    dead_candidates = [
        {
            "lines": lines,
            "name": _qualified(module, name),
        }
        for module, name, lines in private_definitions
        if loaded_names[name] == 0
    ]
    dead_candidates.sort(key=lambda item: (-item["lines"], item["name"]))

    return {
        "direct_json_dumps": sorted(direct_json_dumps),
        "duplicate_bodies": duplicate_bodies,
        "duplicate_method_bodies": duplicate_method_bodies,
        "duplicate_class_names": duplicate_class_names,
        "duplicate_function_names": duplicate_function_names,
        "import_cycles": _strongly_connected(imports),
        "modules": modules,
        "potential_dead_private_definitions": dead_candidates,
        "repr_identity_sites": sorted(
            repr_identity_sites,
            key=lambda item: (item["owner"], item["line"]),
        ),
        "schema": "zlang-python-architecture-audit-v3",
        "summary": {
            "classes": sum(len(item["classes"]) for item in modules.values()),
            "files": len(paths),
            "functions": sum(len(item["functions"]) for item in modules.values()),
            "lines": sum(item["lines"] for item in modules.values()),
            "methods": sum(
                class_["methods"]
                for item in modules.values()
                for class_ in item["classes"]
            ),
        },
        "traversal_sites": sorted(traversal_sites),
        "unallowlisted_repr_identity_sites": tuple(
            item for item in sorted(
                repr_identity_sites,
                key=lambda value: (value["owner"], value["line"]),
            )
            if item["allowlist_reason"] is None
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("zlang"))
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = json.dumps(audit(args.root), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
