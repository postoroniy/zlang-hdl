# ZLang HDL Language Reference

Syntax, exact types, state, timing, protocols and verification contracts.

Candidate: **0.1.0a24**. Direct SystemVerilog is the production RTL backend.

[Language Reference](language-reference.md) · [Standard Library Guide](stdlib.md) · [User Guide](user-guide.md)

[Download the PDF](ZLang-HDL-Language-Reference.pdf)

## Chapters

- [Why ZLang HDL](language-overview.md)
- [Types, numerics, and packed representation](types-and-numerics.md)
- [Expressions, functions, and generics](expressions-functions-generics.md)
- [Compile-time integer modulo](compile-time-integers.md)
- [Sequential state, rules, pipelines, and storage](sequential-state-storage.md)
- [Physical clocks and resets](physical-clock-reset-contract.md)
- [Hierarchy, protocols, and explicit CDC](hierarchy-protocols.md)
- [Named module interfaces](named-module-interfaces.md)
- [Tagged unions](tagged-unions.md)
- [Parameterized aggregate protocols](parameterized-aggregate-protocol.md)
- [Implementation intent, frequency, and II](implementation-intent.md)
- [Optimization and formal verification](optimization-formal.md)
- [Storage-owning instance arrays](storage-instance-arrays.md)
- [Source and product identity](source-product-identity.md)
- [Language support matrix](syntax-support-matrix.md)
- [Known limitations](known-limitations.md)
- [ZLang HDL concise source-authoring reference](language-quick-reference.md)

[Native `--logic-state` simulation and diagnostics](native-jit-simulation.md#four-state-initialization-and-diagnostics)

## Previous reference links

Existing `reference-*` links remain as navigation aliases. The linked chapter owns the text.

<a id="reference-why-zlang"></a>
- [Continue in the canonical chapter](language-overview.md#reference-why-zlang)

<a id="reference-quick-start"></a>
- [Continue in the canonical chapter](language-overview.md#reference-quick-start)

<a id="reference-types-and-numerics"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics)

<a id="reference-types-and-numerics-characters-fixed-strings-and-tuples"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-characters-fixed-strings-and-tuples)

<a id="reference-types-and-numerics-nominal-enums"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-nominal-enums)

<a id="reference-types-and-numerics-nominal-tagged-unions"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-nominal-tagged-unions)

<a id="reference-types-and-numerics-scalar-families"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-scalar-families)

<a id="reference-types-and-numerics-integer-width-rules"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-integer-width-rules)

<a id="reference-types-and-numerics-fixed-point"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-fixed-point)

<a id="reference-types-and-numerics-vectors-and-structs"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-vectors-and-structs)

<a id="reference-types-and-numerics-slicing-concatenation-and-representation"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-slicing-concatenation-and-representation)

<a id="reference-types-and-numerics-current-boundaries"></a>
- [Continue in the canonical chapter](types-and-numerics.md#reference-types-and-numerics-current-boundaries)

<a id="reference-expressions-functions-generics"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics)

<a id="reference-expressions-functions-generics-operators-and-selection"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-operators-and-selection)

<a id="reference-expressions-functions-generics-aggregate-value-ergonomics"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-aggregate-value-ergonomics)

<a id="reference-expressions-functions-generics-pure-functions"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-pure-functions)

<a id="reference-expressions-functions-generics-typed-constants-and-statically-selected-functions"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-typed-constants-and-statically-selected-functions)

<a id="reference-expressions-functions-generics-compile-time-integer-modulo"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-compile-time-integer-modulo)

<a id="reference-expressions-functions-generics-compile-time-functions-and-selection"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-compile-time-functions-and-selection)

<a id="reference-expressions-functions-generics-functional-datapath"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-functional-datapath)

<a id="reference-expressions-functions-generics-scalable-bounded-functional-elaboration"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-scalable-bounded-functional-elaboration)

<a id="reference-expressions-functions-generics-typed-bit-and-collection-composition"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-typed-bit-and-collection-composition)

<a id="reference-expressions-functions-generics-generic-structs-functions-and-operators"></a>
- [Continue in the canonical chapter](expressions-functions-generics.md#reference-expressions-functions-generics-generic-structs-functions-and-operators)

<a id="reference-sequential-state-storage"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage)

<a id="reference-sequential-state-storage-clock-and-reset"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-clock-and-reset)

<a id="reference-sequential-state-storage-registers-and-next-state"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-registers-and-next-state)

<a id="reference-sequential-state-storage-runtime-indexed-vector-register-updates"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-runtime-indexed-vector-register-updates)

<a id="reference-sequential-state-storage-delays-and-fixed-pipelines"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-delays-and-fixed-pipelines)

<a id="reference-sequential-state-storage-exact-module-timing-contracts"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-exact-module-timing-contracts)

<a id="reference-sequential-state-storage-guarded-atomic-actions"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-guarded-atomic-actions)

<a id="reference-sequential-state-storage-fifos"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-fifos)

<a id="reference-sequential-state-storage-writable-memories"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-writable-memories)

<a id="reference-storage-memories"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-storage-memories)

<a id="reference-sequential-state-storage-named-same-clock-ports"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-named-same-clock-ports)

<a id="reference-sequential-state-storage-explicit-dual-clock-memory"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-explicit-dual-clock-memory)

<a id="reference-sequential-state-storage-replicated-read-ports-wrappers-and-banking"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-replicated-read-ports-wrappers-and-banking)

<a id="reference-sequential-state-storage-initialized-synchronous-roms"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-initialized-synchronous-roms)

<a id="reference-sequential-state-storage-stateful-hierarchy"></a>
- [Continue in the canonical chapter](sequential-state-storage.md#reference-sequential-state-storage-stateful-hierarchy)

<a id="reference-physical-clock-reset-contract"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract)

<a id="reference-physical-clock-reset-contract-recommended-asynchronous-reset"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract-recommended-asynchronous-reset)

<a id="reference-physical-clock-reset-contract-backend-lowering"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract-backend-lowering)

<a id="reference-physical-clock-reset-contract-hierarchy-inheritance"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract-hierarchy-inheritance)

<a id="reference-physical-clock-reset-contract-formal-applicability"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract-formal-applicability)

<a id="reference-physical-clock-reset-contract-boundaries"></a>
- [Continue in the canonical chapter](physical-clock-reset-contract.md#reference-physical-clock-reset-contract-boundaries)

<a id="reference-hierarchy-protocols"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols)

<a id="reference-hierarchy-protocols-modules-and-instances"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-modules-and-instances)

<a id="reference-hierarchy-protocols-typed-external-modules"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-typed-external-modules)

<a id="reference-hierarchy-protocols-clock-and-reset-boundary"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-clock-and-reset-boundary)

<a id="reference-hierarchy-protocols-public-top-abi"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-public-top-abi)

<a id="reference-hierarchy-protocols-ready-valid"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-ready-valid)

<a id="reference-hierarchy-protocols-credit-and-request-response"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-credit-and-request-response)

<a id="reference-hierarchy-protocols-aggregate-protocols-and-the-standard-library"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-aggregate-protocols-and-the-standard-library)

<a id="reference-hierarchy-protocols-packets-virtual-channels-and-arbitration"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-packets-virtual-channels-and-arbitration)

<a id="reference-hierarchy-protocols-csr-blocks"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-csr-blocks)

<a id="reference-hierarchy-protocols-clock-domain-crossings"></a>
- [Continue in the canonical chapter](hierarchy-protocols.md#reference-hierarchy-protocols-clock-domain-crossings)

<a id="reference-named-module-interfaces"></a>
- [Continue in the canonical chapter](named-module-interfaces.md#reference-named-module-interfaces)

<a id="reference-named-module-interfaces-exact-conformance"></a>
- [Continue in the canonical chapter](named-module-interfaces.md#reference-named-module-interfaces-exact-conformance)

<a id="reference-named-module-interfaces-aggregate-protocol-signatures"></a>
- [Continue in the canonical chapter](named-module-interfaces.md#reference-named-module-interfaces-aggregate-protocol-signatures)

<a id="reference-named-module-interfaces-selection-and-backend-identity"></a>
- [Continue in the canonical chapter](named-module-interfaces.md#reference-named-module-interfaces-selection-and-backend-identity)

<a id="reference-named-module-interfaces-first-slice-boundaries"></a>
- [Continue in the canonical chapter](named-module-interfaces.md#reference-named-module-interfaces-first-slice-boundaries)

<a id="reference-tagged-unions"></a>
- [Continue in the canonical chapter](tagged-unions.md#reference-tagged-unions)

<a id="reference-tagged-unions-type-and-layout"></a>
- [Continue in the canonical chapter](tagged-unions.md#reference-tagged-unions-type-and-layout)

<a id="reference-tagged-unions-construction-and-matching"></a>
- [Continue in the canonical chapter](tagged-unions.md#reference-tagged-unions-construction-and-matching)

<a id="reference-tagged-unions-deliberate-boundaries"></a>
- [Continue in the canonical chapter](tagged-unions.md#reference-tagged-unions-deliberate-boundaries)

<a id="reference-parameterized-aggregate-protocol"></a>
- [Continue in the canonical chapter](parameterized-aggregate-protocol.md#reference-parameterized-aggregate-protocol)

<a id="reference-implementation-intent"></a>
- [Continue in the canonical chapter](implementation-intent.md#reference-implementation-intent)

<a id="reference-optimization-formal"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal)

<a id="reference-optimization-formal-canonical-and-selected-ir"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-canonical-and-selected-ir)

<a id="reference-optimization-formal-one-implementation-policy-path"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-one-implementation-policy-path)

<a id="reference-optimization-formal-explicit-choices"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-explicit-choices)

<a id="reference-optimization-formal-exact-and-selected-timing"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-exact-and-selected-timing)

<a id="reference-optimization-formal-first-class-verification-goals-and-contracts"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-first-class-verification-goals-and-contracts)

<a id="reference-optimization-formal-execution-and-immutable-bundles"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-execution-and-immutable-bundles)

<a id="reference-optimization-formal-formal-layers"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-formal-layers)

<a id="reference-optimization-formal-reports"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-optimization-formal-reports)

<a id="reference-egraph-optimization-infrastructure"></a>
- [Continue in the canonical chapter](optimization-formal.md#reference-egraph-optimization-infrastructure)

<a id="reference-storage-instance-arrays"></a>
- [Continue in the canonical chapter](storage-instance-arrays.md#reference-storage-instance-arrays)

<a id="reference-storage-instance-arrays-supported-boundary"></a>
- [Continue in the canonical chapter](storage-instance-arrays.md#reference-storage-instance-arrays-supported-boundary)

<a id="reference-source-identity-migration"></a>
- [Continue in the canonical chapter](source-product-identity.md#reference-source-identity-migration)

<a id="reference-syntax-support-matrix"></a>
- [Continue in the canonical chapter](syntax-support-matrix.md#reference-syntax-support-matrix)

<a id="reference-known-limitations"></a>
- [Continue in the canonical chapter](known-limitations.md#reference-known-limitations)

<a id="reference-known-limitations-language-and-backend-boundaries"></a>
- [Continue in the canonical chapter](known-limitations.md#reference-known-limitations-language-and-backend-boundaries)

<a id="reference-known-limitations-verification-boundaries"></a>
- [Continue in the canonical chapter](known-limitations.md#reference-known-limitations-verification-boundaries)

<a id="reference-stdlib"></a>
- [Continue in the canonical chapter](stdlib-overview.md#reference-stdlib)

<a id="reference-stdlib-buses"></a>
- [Continue in the canonical chapter](stdlib-overview.md#reference-stdlib-buses)

<a id="reference-stdlib-fixed-point-math"></a>
- [Continue in the canonical chapter](stdlib-overview.md#reference-stdlib-fixed-point-math)

<a id="reference-stdlib-complex-values-streams-storage-and-coding"></a>
- [Continue in the canonical chapter](stdlib-overview.md#reference-stdlib-complex-values-streams-storage-and-coding)

<a id="reference-stdlib-target-and-architecture-descriptions"></a>
- [Continue in the canonical chapter](stdlib-overview.md#reference-stdlib-target-and-architecture-descriptions)

<a id="reference-standard-bus-library"></a>
- [Continue in the canonical chapter](standard-bus-library.md#reference-standard-bus-library)

<a id="reference-standard-bus-library-bounded-ztpu-axi-burst-subset"></a>
- [Continue in the canonical chapter](standard-bus-library.md#reference-standard-bus-library-bounded-ztpu-axi-burst-subset)

<a id="reference-getting-started"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started)

<a id="reference-getting-started-install-for-development"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-install-for-development)

<a id="reference-getting-started-a-first-module"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-a-first-module)

<a id="reference-getting-started-check-named-verification-goals"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-check-named-verification-goals)

<a id="reference-getting-started-source-files"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-source-files)

<a id="reference-getting-started-reading-common-notation"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-reading-common-notation)

<a id="reference-getting-started-where-to-continue"></a>
- [Continue in the canonical chapter](getting-started.md#reference-getting-started-where-to-continue)

<a id="reference-installing-toolchain"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain)

<a id="reference-installing-toolchain-recommended-installation-with-uv"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-recommended-installation-with-uv)

<a id="reference-installing-toolchain-conventional-venv-and-pip-alternative"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-conventional-venv-and-pip-alternative)

<a id="reference-installing-toolchain-windows-through-wsl2"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-windows-through-wsl2)

<a id="reference-installing-toolchain-check-and-run-zlang"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-check-and-run-zlang)

<a id="reference-installing-toolchain-choose-which-external-tools-you-need"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-choose-which-external-tools-you-need)

<a id="reference-installing-toolchain-recommended-oss-cad-suite"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-recommended-oss-cad-suite)

<a id="reference-installing-toolchain-alternative-install-components-separately"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-alternative-install-components-separately)

<a id="reference-installing-toolchain-verify-the-installation"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-verify-the-installation)

<a id="reference-installing-toolchain-verification-architecture-and-troubleshooting"></a>
- [Continue in the canonical chapter](installing-toolchain.md#reference-installing-toolchain-verification-architecture-and-troubleshooting)

<a id="reference-projects-dependencies"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies)

<a id="reference-projects-dependencies-project-manifest"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies-project-manifest)

<a id="reference-projects-dependencies-lock-update"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies-lock-update)

<a id="reference-projects-dependencies-offline-compilation"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies-offline-compilation)

<a id="reference-projects-dependencies-identity-and-artifacts"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies-identity-and-artifacts)

<a id="reference-projects-dependencies-deliberate-boundaries"></a>
- [Continue in the canonical chapter](projects-dependencies.md#reference-projects-dependencies-deliberate-boundaries)

<a id="reference-native-simulation"></a>
- [Continue in the canonical chapter](native-jit-simulation.md#reference-native-simulation)

<a id="reference-simulation-verification"></a>
- [Continue in the canonical chapter](native-jit-simulation.md#reference-simulation-verification)

<a id="reference-generate-systemverilog"></a>
- [Continue in the canonical chapter](backends-tooling.md#reference-generate-systemverilog)

<a id="reference-vendor-fpga-debug-taps"></a>
- [Continue in the canonical chapter](backends-tooling.md#reference-vendor-fpga-debug-taps)

<a id="reference-implementation-profiles"></a>
- [Continue in the canonical chapter](implementation-profiles.md#reference-implementation-profiles)

<a id="reference-profiles-targets"></a>
- [Continue in the canonical chapter](implementation-profiles.md#reference-profiles-targets)

<a id="reference-implementation-profiles-semantic-regions"></a>
- [Continue in the canonical chapter](implementation-profiles.md#reference-implementation-profiles-semantic-regions)

<a id="reference-implementation-profiles-normalization-and-conflicts"></a>
- [Continue in the canonical chapter](implementation-profiles.md#reference-implementation-profiles-normalization-and-conflicts)

<a id="reference-implementation-profiles-backend-plans"></a>
- [Continue in the canonical chapter](implementation-profiles.md#reference-implementation-profiles-backend-plans)

<a id="reference-target-platform-architecture-description"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description)

<a id="reference-target-platform-architecture-description-compiler-shipped-source-descriptions"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description-compiler-shipped-source-descriptions)

<a id="reference-target-platform-architecture-description-manual-selection"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description-manual-selection)

<a id="reference-target-platform-architecture-description-the-selected-graph"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description-the-selected-graph)

<a id="reference-target-platform-architecture-description-backend-and-manifest"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description-backend-and-manifest)

<a id="reference-target-platform-architecture-description-boundaries"></a>
- [Continue in the canonical chapter](target-platform-architecture-description.md#reference-target-platform-architecture-description-boundaries)

<a id="reference-low-level-target-resource-library"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library)

<a id="reference-low-level-target-resource-library-implemented-model"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-implemented-model)

<a id="reference-low-level-target-resource-library-dsp48e1-manual-pipeline-validation"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-dsp48e1-manual-pipeline-validation)

<a id="reference-low-level-target-resource-library-signed-product-cascade-validation"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-signed-product-cascade-validation)

<a id="reference-low-level-target-resource-library-ramb36-validation"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-ramb36-validation)

<a id="reference-low-level-target-resource-library-intel-and-asic-status"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-intel-and-asic-status)

<a id="reference-low-level-target-resource-library-clock-resource-blocker"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-clock-resource-blocker)

<a id="reference-low-level-target-resource-library-backendartifact-implementation-manifest-v7"></a>
- [Continue in the canonical chapter](low-level-target-resource-library.md#reference-low-level-target-resource-library-backendartifact-implementation-manifest-v7)

<a id="reference-high-level-target-aware-architecture-pipeline-planner"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner)

<a id="reference-high-level-target-aware-architecture-pipeline-planner-user-contract"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner-user-contract)

<a id="reference-high-level-target-aware-architecture-pipeline-planner-candidate-generation-and-legality"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner-candidate-generation-and-legality)

<a id="reference-high-level-target-aware-architecture-pipeline-planner-timing-dag"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner-timing-dag)

<a id="reference-high-level-target-aware-architecture-pipeline-planner-evidence-and-extraction"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner-evidence-and-extraction)

<a id="reference-high-level-target-aware-architecture-pipeline-planner-current-boundary"></a>
- [Continue in the canonical chapter](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner-current-boundary)

<a id="reference-platform-constraint-publication"></a>
- [Continue in the canonical chapter](platform-constraint-publication.md#reference-platform-constraint-publication)

<a id="reference-platform-constraint-publication-decision"></a>
- [Continue in the canonical chapter](platform-constraint-publication.md#reference-platform-constraint-publication-decision)

<a id="reference-direct-systemverilog"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog)

<a id="reference-direct-systemverilog-supported-composition-subset"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog-supported-composition-subset)

<a id="reference-direct-systemverilog-simulation-only-architectural-state-access"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog-simulation-only-architectural-state-access)

<a id="reference-direct-systemverilog-formal-boundary"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog-formal-boundary)

<a id="reference-direct-systemverilog-explicitly-unsupported"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog-explicitly-unsupported)

<a id="reference-direct-systemverilog-exhaustive-example-matrix"></a>
- [Continue in the canonical chapter](direct-systemverilog.md#reference-direct-systemverilog-exhaustive-example-matrix)

<a id="reference-formal-verification"></a>
- [Continue in the canonical chapter](formal-verification.md#reference-formal-verification)

<a id="reference-whole-build-manifests"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests)

<a id="reference-whole-build-manifests-compiler-and-implementation-identities"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-compiler-and-implementation-identities)

<a id="reference-whole-build-manifests-published-files-and-companions"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-published-files-and-companions)

<a id="reference-whole-build-manifests-exact-evidence-meanings"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-exact-evidence-meanings)

<a id="reference-whole-build-manifests-harness-generation-is-not-proof-execution"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-harness-generation-is-not-proof-execution)

<a id="reference-whole-build-manifests-counterexample-and-source-attribution"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-counterexample-and-source-attribution)

<a id="reference-whole-build-manifests-cli-publication"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-cli-publication)

<a id="reference-whole-build-manifests-current-limitations"></a>
- [Continue in the canonical chapter](whole-build-manifests.md#reference-whole-build-manifests-current-limitations)

<a id="reference-structured-diagnostics"></a>
- [Continue in the canonical chapter](structured-diagnostics.md#reference-structured-diagnostics)

<a id="reference-generated-source-maps"></a>
- [Continue in the canonical chapter](generated-source-maps.md#reference-generated-source-maps)

<a id="reference-generated-source-maps-exactness-boundary"></a>
- [Continue in the canonical chapter](generated-source-maps.md#reference-generated-source-maps-exactness-boundary)

<a id="reference-generated-source-maps-generated-rtl-navigation-audit"></a>
- [Continue in the canonical chapter](generated-source-maps.md#reference-generated-source-maps-generated-rtl-navigation-audit)

<a id="reference-generated-navigation-bundles"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles)

<a id="reference-generated-navigation-bundles-why-this-is-a-separate-v1-contract"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles-why-this-is-a-separate-v1-contract)

<a id="reference-generated-navigation-bundles-layout-and-schema"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles-layout-and-schema)

<a id="reference-generated-navigation-bundles-publication-and-validation"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles-publication-and-validation)

<a id="reference-generated-navigation-bundles-source-freshness"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles-source-freshness)

<a id="reference-generated-navigation-bundles-relocatability-and-limitations"></a>
- [Continue in the canonical chapter](generated-navigation-bundles.md#reference-generated-navigation-bundles-relocatability-and-limitations)

<a id="reference-tooling-integration-api"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api)

<a id="reference-tooling-integration-api-demand-driven-observational-analysis"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-demand-driven-observational-analysis)

<a id="reference-tooling-integration-api-diagnostic-edits"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-diagnostic-edits)

<a id="reference-tooling-integration-api-generated-rtl-navigation-audit"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-generated-rtl-navigation-audit)

<a id="reference-tooling-integration-api-document-symbols"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-document-symbols)

<a id="reference-tooling-integration-api-semantic-hover"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-semantic-hover)

<a id="reference-tooling-integration-api-definitions"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-definitions)

<a id="reference-tooling-integration-api-references"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-references)

<a id="reference-tooling-integration-api-rename"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-rename)

<a id="reference-tooling-integration-api-completion"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-completion)

<a id="reference-tooling-integration-api-signature-help"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-signature-help)

<a id="reference-tooling-integration-api-semantic-tokens"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-semantic-tokens)

<a id="reference-tooling-integration-api-reusable-tooling-and-symbol-sessions"></a>
- [Continue in the canonical chapter](tooling-integration-api.md#reference-tooling-integration-api-reusable-tooling-and-symbol-sessions)

<a id="reference-zlang-lsp"></a>
- [Continue in the canonical chapter](zlang-lsp.md#reference-zlang-lsp)

<a id="reference-zlang-lsp-supported-now"></a>
- [Continue in the canonical chapter](zlang-lsp.md#reference-zlang-lsp-supported-now)

<a id="reference-zlang-lsp-cache-configuration"></a>
- [Continue in the canonical chapter](zlang-lsp.md#reference-zlang-lsp-cache-configuration)

<a id="reference-zlang-lsp-not-implemented"></a>
- [Continue in the canonical chapter](zlang-lsp.md#reference-zlang-lsp-not-implemented)
