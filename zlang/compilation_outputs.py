"""Builders for immutable terminal compilation products."""

from __future__ import annotations

from zlang.ast.nodes import Module as AstModule
from zlang.backend.systemverilog import emit_contracts
from zlang import costs
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.compilation_products import (
    CompilationResult,
    DocumentProduct,
    FormalProduct,
    PlanningProduct,
    ReportProduct,
    SelectionProduct,
)
from zlang.csr import emit_csr_json, emit_csr_markdown
from zlang.exploration import render_exploration_report
from zlang import formal as formal_api
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_tooling import FormalToolResolver
from zlang.implementations import render_implementation_report
from zlang.ir.module import Module as IrModule
from zlang.pipelines import render_pipeline_report
from zlang.target_catalog import load_target


def build_formal(module: IrModule) -> FormalProduct:
    design = formal_api.build_formal_design(module)
    recursive = formal_api.build_recursive_formal_design(module)
    return FormalProduct(
        design,
        recursive,
    )


def build_target_instance(selection: SelectionProduct) -> object | None:
    request = selection.implementation_request
    if request.target is None or request.target == "generic":
        return None
    return load_target(request.target)[0]


def build_documents(module: IrModule) -> DocumentProduct:
    return DocumentProduct(
        emit_csr_markdown(module) if module.csr_blocks else "",
        emit_csr_json(module) if module.csr_blocks else "",
        emit_contracts(module)
        if module.contracts or module.verification_scopes else "",
    )


def build_reports(
    selection: SelectionProduct,
    planning: PlanningProduct,
) -> ReportProduct:
    implementation_report = render_implementation_report(planning.module)
    if not implementation_report and selection.exploration_results:
        implementation_report = render_exploration_report(
            selection.exploration_results
        )
    return ReportProduct(
        implementation_report,
        costs.render_cost_report(selection.extraction),
        render_pipeline_report(planning.module)
        + (
            planning.target_planning_result.report
            if planning.target_planning_result
            else ""
        ),
        render_exploration_report(selection.exploration_results),
        render_exploration_report(selection.exploration_results),
    )


def build_materialized(
    *,
    syntax: AstModule,
    selection: SelectionProduct,
    formal: FormalProduct,
    planning: PlanningProduct,
    target_instance: object | None,
    documents: DocumentProduct,
    reports: ReportProduct,
    physical_inputs: PhysicalCompilationInputs,
    formal_artifact_provider: FormalArtifactProvider | None,
    formal_tool_resolver: FormalToolResolver | None,
) -> CompilationResult:
    return CompilationResult(
        ast=syntax,
        ir=planning.module,
        optimization_ir=selection.optimization_ir,
        csr_markdown=documents.csr_markdown,
        csr_json=documents.csr_json,
        contracts_sva=documents.contracts_sva,
        implementation_report=reports.implementation,
        cost_report=reports.cost,
        pipeline_report=reports.pipeline,
        architecture_report=reports.architecture,
        high_level_ir=selection.high_level_ir,
        exploration_report=reports.exploration,
        exploration_results=selection.exploration_results,
        formal_design=formal.design,
        recursive_formal_design=formal.recursive_design,
        target_instance=target_instance,
        implementation_graph=planning.implementation_graph,
        target_planning_result=planning.target_planning_result,
        target_planner_report=(
            planning.target_planning_result.report
            if planning.target_planning_result
            else ""
        ),
        implementation_policy=selection.implementation_policy,
        implementation_request=selection.implementation_request,
        implementation_regions=tuple(
            item.region for item in selection.implementation_policy.regions
        ),
        implementation_policy_report=selection.implementation_policy.report,
        backend_implementation_plans=planning.backend_plans,
        backend_implementation_report=planning.backend_plans.report,
        physical_inputs=physical_inputs,
        formal_artifact_provider=formal_artifact_provider,
        formal_tool_resolver=formal_tool_resolver,
        candidate_site_ledger=selection.candidate_site_ledger,
        physical_formal_records=planning.physical_formal_records,
    )
