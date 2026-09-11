"""Public Python API for the ZLang HDL compiler prototype."""

from zlang._version import __version__
from zlang.source_identity import (
    CLI_NAME,
    DISTRIBUTION_NAME,
    MIME_TYPE,
    PUBLIC_LANGUAGE_NAME,
    SOURCE_SUFFIX,
    VSCODE_LANGUAGE_ID,
)

from zlang.compiler import (
    CompilationResult,
    PhysicalCompilationInputs,
    SemanticCheckResult,
    check_file_snapshot,
    compile_file,
    compile_source,
    create_file_compilation_session,
    create_file_compilation_session_snapshot,
)
from zlang.compilation_session import CompilationSession
from zlang.simulation_state import (
    SimulationStateBinding,
    SimulationStateCatalog,
    SimulationStateError,
    SimulationStateKind,
    SimulationStateSession,
    build_simulation_state_catalog,
)
from zlang.candidate_sites import (
    CandidateRankRecord,
    CandidateRewriteKind,
    CandidateSiteKind,
    CandidateSiteLedger,
    CandidateSiteRecord,
)
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
    FormalArtifactRecipe,
)

__all__ = [
    "__version__",
    "CLI_NAME",
    "CompilationResult",
    "CompilationSession",
    "DISTRIBUTION_NAME",
    "CandidateRankRecord",
    "CandidateRewriteKind",
    "CandidateSiteKind",
    "CandidateSiteLedger",
    "CandidateSiteRecord",
    "FormalArtifactNamespace",
    "FormalArtifactProvider",
    "FormalArtifactRecipe",
    "MIME_TYPE",
    "PhysicalCompilationInputs",
    "PUBLIC_LANGUAGE_NAME",
    "SemanticCheckResult",
    "SimulationStateBinding",
    "SimulationStateCatalog",
    "SimulationStateError",
    "SimulationStateKind",
    "SimulationStateSession",
    "SOURCE_SUFFIX",
    "VSCODE_LANGUAGE_ID",
    "check_file_snapshot",
    "build_simulation_state_catalog",
    "compile_file",
    "compile_source",
    "create_file_compilation_session",
    "create_file_compilation_session_snapshot",
]
