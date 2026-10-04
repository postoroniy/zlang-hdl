# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Stable verification-bundle API composed from bounded subsystem owners."""

from zlang.formal import FormalToolchainContext

from zlang.verification_bundle_codec import (
    VERIFICATION_BUNDLE_SCHEMA,
    VERIFICATION_BUNDLE_SCHEMA_VERSION,
    VERIFICATION_IR_SCHEMA,
    VERIFICATION_IR_SCHEMA_VERSION,
    VERIFICATION_RESULT_CACHE_SCHEMA,
    VERIFICATION_RESULT_CACHE_SCHEMA_VERSION,
    VERIFICATION_RUN_REPORT_SCHEMA,
    VERIFICATION_RUN_REPORT_SCHEMA_VERSION,
    LoadedVerificationBundle,
    VerificationBundleError,
    VerificationBundleFile,
    VerificationBundleInput,
    VerificationBundleManifest,
    VerificationJob,
    verification_identity_for,
)
from zlang.verification_bundle_execution import (
    run_verification_bundle,
    run_verification_bundle_staged,
    verification_result_cache_key,
)
from zlang.verification_bundle_report import (
    VerificationCounterexampleMetadata,
    VerificationJobResult,
    VerificationRunConfig,
    VerificationRunReport,
)
from zlang.verification_bundle_io import (
    load_candidate_equivalence_replay,
    load_verification_bundle,
    publish_verification_bundle,
)

__all__ = [
    "VERIFICATION_BUNDLE_SCHEMA",
    "VERIFICATION_BUNDLE_SCHEMA_VERSION",
    "VERIFICATION_IR_SCHEMA",
    "VERIFICATION_IR_SCHEMA_VERSION",
    "VERIFICATION_RESULT_CACHE_SCHEMA",
    "VERIFICATION_RESULT_CACHE_SCHEMA_VERSION",
    "VERIFICATION_RUN_REPORT_SCHEMA",
    "VERIFICATION_RUN_REPORT_SCHEMA_VERSION",
    "LoadedVerificationBundle",
    "FormalToolchainContext",
    "VerificationBundleError",
    "VerificationBundleFile",
    "VerificationBundleInput",
    "VerificationBundleManifest",
    "VerificationCounterexampleMetadata",
    "VerificationJob",
    "VerificationJobResult",
    "VerificationRunConfig",
    "VerificationRunReport",
    "load_candidate_equivalence_replay",
    "load_verification_bundle",
    "publish_verification_bundle",
    "run_verification_bundle",
    "run_verification_bundle_staged",
    "verification_identity_for",
    "verification_result_cache_key",
]
