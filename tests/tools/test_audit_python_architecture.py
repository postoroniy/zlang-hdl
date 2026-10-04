from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_python_architecture", ROOT / "tools/audit_python_architecture.py"
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_architecture_audit_is_deterministic_and_reports_cycles(tmp_path: Path) -> None:
    package = tmp_path / "fixture"
    package.mkdir()
    (package / "a.py").write_text(
        "from fixture import b\n"
        "def duplicate(value):\n    return value + 1\n"
        "def _unused():\n    return 2\n"
        "def _used_through_owner():\n    return 3\n"
        "class First:\n"
        "    def shared(self, value):\n        return value * 2\n",
        encoding="utf-8",
    )
    (package / "b.py").write_text(
        "from fixture import a\n"
        "a._used_through_owner()\n"
        "def duplicate(value):\n    return value + 1\n"
        "class Second:\n"
        "    def shared(self, value):\n        return value * 2\n",
        encoding="utf-8",
    )

    first = AUDIT.audit(package)
    second = AUDIT.audit(package)

    assert first == second
    assert first["summary"] == {
        "classes": 2,
        "files": 2,
        "functions": 4,
        "lines": 17,
        "methods": 2,
    }
    assert first["import_cycles"] == [["fixture.a", "fixture.b"]]
    assert first["duplicate_function_names"]["duplicate"] == [
        "fixture.a:duplicate",
        "fixture.b:duplicate",
    ]
    assert len(first["duplicate_bodies"]) == 1
    assert first["duplicate_method_bodies"] == [[
        "fixture.a:First.shared",
        "fixture.b:Second.shared",
    ]]
    assert first["potential_dead_private_definitions"] == [
        {"lines": 2, "name": "fixture.a:_unused"}
    ]


def test_compiler_architecture_regressions_remain_closed() -> None:
    report = AUDIT.audit(ROOT / "zlang")

    assert report["duplicate_bodies"] == []
    assert not any(
        "zlang.ir.traversal" in component
        and "zlang.ir.functional" in component
        for component in report["import_cycles"]
    )

    traversal = (ROOT / "zlang/ir/traversal.py").read_text(encoding="utf-8")
    arena = (ROOT / "zlang/ir/expression_arena.py").read_text(encoding="utf-8")
    assert "zlang.ir.functional" not in traversal
    assert "return repr(value)" not in arena
    assert report["unallowlisted_repr_identity_sites"] == ()

    private_candidates = {
        item["name"] for item in report["potential_dead_private_definitions"]
    }
    assert private_candidates == set()


def test_god_module_and_symbol_debt_cannot_grow() -> None:
    report = AUDIT.audit(ROOT / "zlang")
    modules = {item["name"]: item["lines"] for item in report["oversized_modules"]}
    functions = {
        item["name"]: item["lines"] for item in report["oversized_functions"]
    }
    classes = {item["name"]: item["lines"] for item in report["oversized_classes"]}

    expected_module_ceilings: dict[str, int] = {}
    assert set(modules) == set(expected_module_ceilings)
    assert all(
        modules[name] <= ceiling
        for name, ceiling in expected_module_ceilings.items()
    )

    expected_function_ceilings = {
        "zlang.backend.manifest:publish_artifact": 541,
        "zlang.backend.systemverilog.composed:_emit_composed_component": 711,
        "zlang.backend.systemverilog.expression:_expression": 456,
        "zlang.backend.systemverilog.state:_append_unified_state": 521,
        "zlang.pipeline_scheduling:schedule_fixed_pipeline": 485,
        "zlang.verification_bundle_codec:_validate_verification_payload": 551,
    }
    assert set(functions) <= set(expected_function_ceilings)
    assert all(
        functions[name] <= expected_function_ceilings[name]
        for name in functions
    )

    expected_class_ceilings: dict[str, int] = {}
    assert set(classes) <= set(expected_class_ceilings)
    assert all(classes[name] <= expected_class_ceilings[name] for name in classes)

    # The refactor must recover its scaffolding cost rather than rebaseline it.
    assert report["summary"]["lines"] <= 142_278


def test_semantic_subsystem_does_not_depend_on_protocol_or_backend_adapters() -> None:
    forbidden = (
        "zlang.backend",
        "zlang.lsp",
        "zlang.parser",
        "zlang.targets",
        "zlang.tooling",
    )
    violations: list[str] = []
    for path in sorted((ROOT / "zlang/semantic").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                names = (node.module,)
            elif isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            else:
                continue
            for name in names:
                if name.startswith(forbidden):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")

    assert violations == []


def test_semantic_owner_modules_do_not_import_semantic_orchestration() -> None:
    for relative in (
        "zlang/semantic/actions.py",
        "zlang/semantic/callable_bodies.py",
        "zlang/semantic/callable_functional_specialization.py",
        "zlang/semantic/callable_specialization.py",
        "zlang/semantic/callable_specialization_bindings.py",
        "zlang/semantic/callable_state.py",
        "zlang/semantic/callables.py",
        "zlang/semantic/assignment_analysis.py",
        "zlang/semantic/connection_endpoints.py",
        "zlang/semantic/connection_validation.py",
        "zlang/semantic/hierarchical_connections.py",
        "zlang/semantic/expressions.py",
        "zlang/semantic/expression_aggregates.py",
        "zlang/semantic/expression_calls.py",
        "zlang/semantic/expression_coercion.py",
        "zlang/semantic/expression_collections.py",
        "zlang/semantic/expression_control.py",
        "zlang/semantic/expression_names_members.py",
        "zlang/semantic/expression_support.py",
        "zlang/semantic/generic_binding.py",
        "zlang/semantic/csr/analyzer.py",
        "zlang/semantic/csr/bindings.py",
        "zlang/semantic/csr/layout.py",
        "zlang/semantic/equivalences.py",
        "zlang/semantic/hierarchy.py",
        "zlang/semantic/implementation.py",
        "zlang/semantic/imports.py",
        "zlang/semantic/instances.py",
        "zlang/semantic/module_preparation.py",
        "zlang/semantic/module_interfaces.py",
        "zlang/semantic/module_pipeline.py",
        "zlang/semantic/storage_validation.py",
        "zlang/semantic/state.py",
        "zlang/semantic/verification.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        assert "zlang.semantic.analyze" not in imported
        assert "analyze" not in imported


def test_callable_specialization_internals_stay_inside_composition() -> None:
    internal_modules = {
        "zlang.semantic.callable_functional_specialization",
        "zlang.semantic.callable_specialization_bindings",
        "zlang.semantic.callable_state",
        "zlang.semantic.generic_binding",
    }
    allowed_consumers = {
        "callable_functional_specialization.py",
        "callable_specialization.py",
        "callable_specialization_bindings.py",
        "callables.py",
        "context.py",
        "generic_binding.py",
    }
    violations: list[str] = []
    for path in sorted((ROOT / "zlang/semantic").glob("*.py")):
        if path.name in allowed_consumers:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            module = node.module
            if node.level and not module.startswith("zlang.semantic"):
                module = f"zlang.semantic.{module}"
            if module in internal_modules:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{module}")
    assert violations == []


def test_semantic_validation_rules_have_single_explicit_owners() -> None:
    facade = ast.parse(
        (ROOT / "zlang/semantic/analyze.py").read_text(encoding="utf-8")
    )
    facade_functions = {
        node.name for node in facade.body if isinstance(node, ast.FunctionDef)
    }
    assert facade_functions.isdisjoint({
        "_reject_recursive_functions",
        "_reject_instance_output_dependency_cycles",
        "_reject_interface_dependency_cycles",
        "_reject_memory_dependency_cycles",
        "_direct_connection_assignments",
        "_connection_output_keys",
        "_validate_nested_instance_array_child",
        "_check_implementation_choice",
        "_contains_explore",
        "_implementation_mac_shape",
        "_require_pure_implementation_value",
        "_validate_contract_expression",
        "_validate_verification_expression",
        "_observes_public_implementation_output",
        "_validate_assumption_ownership",
    })

    expected = {
        "zlang/semantic/callables.py": {"CallableDependencyValidator"},
        "zlang/semantic/assignment_analysis.py": {"AssignmentAnalyzer"},
        "zlang/semantic/connection_endpoints.py": {
            "HierarchicalEndpointResolver",
        },
        "zlang/semantic/connection_validation.py": {
            "ConnectionAnalyzer",
            "OutputConnectivityValidator",
            "ProtocolDependencyValidator",
        },
        "zlang/semantic/hierarchical_connections.py": {
            "HierarchicalConnectionAnalyzer",
        },
        "zlang/semantic/hierarchy.py": {
            "ClockDomainAnalyzer",
            "HierarchyAnalyzer",
            "HierarchyDependencyValidator",
            "StateDomainResolver",
        },
        "zlang/semantic/implementation.py": {"ImplementationIntentAnalyzer"},
        "zlang/semantic/imports.py": {"ImportAnalyzer"},
        "zlang/semantic/instances.py": {
            "InstanceElaborator",
            "InstanceSpecializer",
        },
        "zlang/semantic/module_interfaces.py": {"NamedInterfaceAnalyzer"},
        "zlang/semantic/storage_validation.py": {
            "MemoryDependencyValidator",
            "StorageAnalyzer",
            "StorageDeclarationAnalyzer",
        },
        "zlang/semantic/state.py": {"RuleAnalyzer", "StateTransitionAnalyzer"},
        "zlang/semantic/verification.py": {
            "VerificationIdentityFinalizer",
            "VerificationOwnershipValidator",
            "VerificationPredicateValidator",
            "VerificationSemanticAnalyzer",
        },
    }
    for relative, class_names in expected.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        assert {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        } >= class_names

    facade_text = (ROOT / "zlang/semantic/analyze.py").read_text(
        encoding="utf-8"
    )
    for name in (
        "_conditional_action_leaves",
        "_action_paths_are_exclusive",
        "resolve_state_domain",
        "_clock_domain_from_source",
        "_interface_clock_domains",
        "_validate_async_reset_domain_scope",
        "_normalize_concise_module_items",
        "_normalize_qualified_imports",
    ):
        assert f"def {name}(" not in facade_text
    assert "fsm_expanded" not in facade_text
    assert "generated_priorities" not in facade_text

    actions = (ROOT / "zlang/semantic/actions.py").read_text(encoding="utf-8")
    preparation = (
        ROOT / "zlang/semantic/module_preparation.py"
    ).read_text(encoding="utf-8")
    assert "def conditional_action_leaves(" in actions
    assert "def action_paths_are_exclusive(" in actions
    assert "def normalize_selected_module_items(" in preparation


def test_canonical_lowering_owners_do_not_import_compatibility_facade() -> None:
    owner_paths = (
        "zlang/opt/entity_lowering.py",
        "zlang/opt/expression_lowering.py",
        "zlang/opt/expression_restoration.py",
        "zlang/opt/module_lowering.py",
        "zlang/opt/module_restoration.py",
    )
    for relative in owner_paths:
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.opt.lowering" not in imported

    facade = ast.parse(
        (ROOT / "zlang/opt/lowering.py").read_text(encoding="utf-8")
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.ClassDef))
        for node in facade.body
    )


def test_systemverilog_functional_planning_has_one_explicit_owner() -> None:
    facade_path = ROOT / "zlang/backend/systemverilog/emitter.py"
    facade = ast.parse(facade_path.read_text(encoding="utf-8"))
    facade_functions = {
        node.name for node in facade.body if isinstance(node, ast.FunctionDef)
    }
    assert facade_functions.isdisjoint({
        "_functional_region_objects",
        "_functional_region_composition_plan",
        "_functional_table_lookups",
        "_functional_binder_dependent",
        "_compact_bitwise_reductions",
        "_contains_compact_bitwise_reduction",
        "_functional_scatter_reduction_names",
    })

    planning_path = (
        ROOT / "zlang/backend/systemverilog/functional/planning.py"
    )
    planning = ast.parse(planning_path.read_text(encoding="utf-8"))
    assert {
        node.name for node in planning.body if isinstance(node, ast.ClassDef)
    } == {"FunctionalRegionPlanner"}

    for relative in (
        "zlang/backend/systemverilog/context.py",
        "zlang/backend/systemverilog/functional/model.py",
        "zlang/backend/systemverilog/functional/planning.py",
        "zlang/backend/systemverilog/functional_scatter.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.backend.systemverilog.emitter" not in imported


def test_functional_emission_state_has_one_context_owner() -> None:
    facade = (
        ROOT / "zlang/backend/systemverilog/emitter.py"
    ).read_text(encoding="utf-8")
    context = (
        ROOT / "zlang/backend/systemverilog/context.py"
    ).read_text(encoding="utf-8")
    target = (
        ROOT / "zlang/backend/systemverilog/target.py"
    ).read_text(encoding="utf-8")

    assert "zlang_systemverilog_functional_expression" not in facade
    assert "zlang_systemverilog_scatter_helpers" not in facade
    assert "ContextVar" not in facade
    assert "class EmissionContext:" in context
    assert "class FunctionalEmissionContext:" in context
    assert "top_boundary: object | None" in context
    assert "with emission_scope(module.name):" in target


def test_systemverilog_has_no_callback_only_emitter_services() -> None:
    facade = (ROOT / "zlang/backend/systemverilog/emitter.py").read_text(
        encoding="utf-8"
    )
    for relative in (
        "zlang/backend/systemverilog/storage.py",
        "zlang/backend/systemverilog/protocols.py",
    ):
        path = ROOT / relative
        assert path.exists()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.backend.systemverilog.emitter" not in imported
    assert "from zlang.backend.systemverilog import protocols as sv_protocols" in facade
    assert "from zlang.backend.systemverilog import storage as sv_storage" in facade
    assert "_STATE_EMITTER" not in facade
    assert "_MEMORY_EMITTER" not in facade
    assert "_PROTOCOL_EMITTERS" not in facade
    assert "def _expression(" not in facade
    assert "def _append_unified_state(" not in facade
    for relative in (
        "zlang/backend/systemverilog/errors.py",
        "zlang/backend/systemverilog/rendering.py",
        "zlang/backend/systemverilog/expression.py",
        "zlang/backend/systemverilog/state.py",
        "zlang/backend/systemverilog/functional/reduction.py",
        "zlang/backend/systemverilog/functional/scatter.py",
        "zlang/backend/systemverilog/functional/region.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.backend.systemverilog.emitter" not in imported

    for name in (
        "functional_region_plan",
        "_functional_region_rendering",
        "_functional_region_replication",
        "_functional_reduction_plan",
        "_functional_scatter_plan",
        "_functional_scatter_rendering",
    ):
        assert f"def {name}(" not in facade


def test_simulation_protocol_and_target_services_have_one_way_dependencies() -> None:
    simulation_facade = (
        ROOT / "zlang/simulation_protocols.py"
    ).read_text(encoding="utf-8")
    plan_builder = (
        ROOT / "zlang/simulation_plan_build.py"
    ).read_text(encoding="utf-8")
    targets = (ROOT / "zlang/targets.py").read_text(encoding="utf-8")

    assert not (ROOT / "zlang/simulation/protocols/pipeline.py").exists()
    assert not (ROOT / "zlang/target_services.py").exists()

    assert "_LEAF_PROTOCOL_PIPELINE" not in simulation_facade
    assert "_HIERARCHY_PROTOCOL_PIPELINE" not in simulation_facade
    assert "from zlang.simulation_protocol_hierarchy import (" in simulation_facade
    for relative in (
        "zlang/simulation_protocol_shared.py",
        "zlang/simulation_ready_valid.py",
        "zlang/simulation_credit.py",
        "zlang/simulation_vc_credit.py",
        "zlang/simulation_packet.py",
        "zlang/simulation_request_response.py",
        "zlang/simulation_protocol_adapters.py",
        "zlang/simulation_protocol_access.py",
        "zlang/simulation_protocol_hierarchy.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.simulation_protocols" not in imported
    assert "module = lower_protocol_hierarchy(module)" in plan_builder
    assert "def has_protocol_surface" not in plan_builder
    assert "def _refresh_limits(" not in plan_builder
    assert "global MAX_PLAN" not in plan_builder
    expression_plan = (
        ROOT / "zlang/simulation_expression_plan.py"
    ).read_text(encoding="utf-8")
    assert "def native_expression_attributes(" in expression_plan
    assert "def native_expression_attributes(" not in plan_builder
    assert "validate_pipeline_configuration," in targets
    assert "map_manual_architecture," in targets
    assert "select_implementation_graph," in targets
    assert "def validate_pipeline_configuration(" not in targets
    assert "def map_manual_architecture(" not in targets
    assert "def select_implementation_graph(" not in targets


def test_tooling_parser_and_verification_lifecycle_services_are_explicit() -> None:
    tooling = ast.parse(
        (ROOT / "zlang/tooling.py").read_text(encoding="utf-8")
    )
    lsp = (ROOT / "zlang/lsp/server.py").read_text(encoding="utf-8")
    parser = (ROOT / "zlang/parser/parser.py").read_text(encoding="utf-8")
    bundle = ast.parse(
        (ROOT / "zlang/verification_bundle.py").read_text(encoding="utf-8")
    )
    verification_cli = (
        ROOT / "zlang/verification_cli.py"
    ).read_text(encoding="utf-8")
    verification_publication = (
        ROOT / "zlang/verification_publication_builder.py"
    ).read_text(encoding="utf-8")

    assert not (ROOT / "zlang/tooling_services.py").exists()
    assert len((ROOT / "zlang/tooling.py").read_text(encoding="utf-8").splitlines()) <= 800
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tooling.body
    )
    assert "self.tooling_session = ToolingSession()" in lsp
    assert "definition_at(" in lsp
    assert "check_snapshot(" in lsp
    assert "_session=self.tooling_session" in lsp

    assert not (ROOT / "zlang/parser/rules.py").exists()
    assert "class _AstBuilder(" in parser
    for owner in (
        "TargetAndGenericRules",
        "AggregateCallableModuleRules",
        "StateRules",
        "ProtocolAndCsrRules",
        "TypeRules",
        "ExpressionRules",
        "SourceRuleMixin",
    ):
        assert owner in parser
    # The tested packaged-parser cache contract remains in the compatibility
    # module while callback source-coordinate state has an explicit owner.
    assert "_PARSER_LOCK = Lock()" in parser
    assert "_PARSER: Lark | None = None" in parser

    assert not (ROOT / "zlang/verification_bundle_services.py").exists()
    assert "load_verification_bundle(" in verification_cli
    assert "run_verification_bundle_staged(" in verification_cli
    assert "publish_verification_bundle(" in verification_publication

    for relative in (
        "zlang/verification_bundle_codec.py",
        "zlang/verification_bundle_io.py",
        "zlang/verification_bundle_report.py",
        "zlang/verification_bundle_execution.py",
        "zlang/verification_goal_routing.py",
        "zlang/verification_publication_builder.py",
        "zlang/verification_recursive_publication.py",
        "zlang/verification_root_publication.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.verification_bundle" not in imported

    parser_rule_trees = [
        ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "zlang/parser").glob("*_rules.py"))
    ]
    for tree in parser_rule_trees:
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "zlang.parser.parser" not in imported
    for tree, forbidden in (
        (tooling, "zlang.tooling_services"),
        (bundle, "zlang.verification_bundle_services"),
    ):
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert forbidden not in imported


def test_tooling_owner_modules_do_not_import_the_compatibility_facade() -> None:
    for relative in (
        "zlang/tooling_models.py",
        "zlang/tooling_session.py",
        "zlang/tooling_symbol_cache.py",
        "zlang/tooling_workspace.py",
        "zlang/tooling_symbols.py",
        "zlang/tooling_queries.py",
        "zlang/tooling_navigation.py",
        "zlang/tooling_diagnostics.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert "zlang.tooling" not in imported


def test_cli_owner_modules_do_not_import_the_public_facade() -> None:
    for path in sorted((ROOT / "zlang").glob("cli_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert "zlang.cli" not in imported
