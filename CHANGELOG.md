# Changelog

All notable public changes to ZLang HDL will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
During the alpha series, source syntax, Python APIs, and serialized formats may
change incompatibly when the release notes identify the change. Versioned IR,
artifact, lock, manifest, and verification schemas continue to reject
incompatible input explicitly.

## [Unreleased]

## [0.1.0a1] - 2026-09-01

### Added

- Initial experimental alpha of the ZLang hardware DSL compiler.
- Public release, security, contribution, support, and provenance policies.
- Typed semantic and canonical IR, simulation, Clash and direct-SystemVerilog
  backends, compiler-owned standard library, project locking, manifests, and
  bounded optimization and verification workflows.
- Real-design validation including DMA, standard-bus CSR paths, fixed-point FIR,
  FFT512, and an attributed IEEE 802.11a transmitter project.

[Unreleased]: https://github.com/postoroniy/zlang-hdl/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/postoroniy/zlang-hdl/releases/tag/v0.1.0a1
