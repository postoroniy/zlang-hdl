# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Publish and load immutable verification bundle directories."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterable, Mapping

from zlang.backend import publication as publication
from zlang.common import stable_digest, stable_pretty_json


from zlang import verification_bundle_codec as bundle_codec
from zlang import verification_codec_support as codec_support

def _candidate_replay_records(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    if payload.get("formal_ir_version") != 4:
        return ()
    values = payload.get("candidate_equivalence_records")
    if not isinstance(values, list):
        raise codec_support.VerificationBundleError(
            "candidate equivalence records must be an array"
        )
    return tuple(values)  # already structurally checked by payload validation


def _validate_candidate_replay_files(
    payload: Mapping[str, object],
    *,
    files: tuple[bundle_codec.VerificationBundleFile, ...],
    jobs: tuple[bundle_codec.VerificationJob, ...],
    read_bytes: object,
) -> tuple[object, ...]:
    """Decode and cross-link immutable candidate companions to exact plans."""

    from zlang.candidate_equivalence import FrozenCandidateEquivalenceSite

    records = _candidate_replay_records(payload)
    files_by_path = {item.logical_path: item for item in files}
    referenced = {
        path
        for job in jobs
        for path in (*job.source_files, *job.config_files, *job.source_map_files)
    }
    replay_paths = {str(item["logical_path"]) for item in records}
    unreferenced = set(files_by_path) - referenced
    if unreferenced != replay_paths:
        detail = sorted(unreferenced ^ replay_paths)
        raise codec_support.VerificationBundleError(
            "candidate companion file set differs from replay records"
            + ("" if not detail else f": '{detail[0]}'")
        )
    restored = []
    for record in records:
        path = str(record["logical_path"])
        file_record = files_by_path.get(path)
        if file_record is None or file_record.kind != "companion":
            raise codec_support.VerificationBundleError(
                f"candidate replay companion '{path}' is missing"
            )
        if file_record.content_hash != record["content_hash"]:
            raise codec_support.VerificationBundleError(
                f"candidate replay companion '{path}' hash differs from its record"
            )
        try:
            content = read_bytes(path)
            if not isinstance(content, bytes):
                raise TypeError("candidate replay reader did not return bytes")
            value = json.loads(content.decode("utf-8"))
            site = FrozenCandidateEquivalenceSite.from_data(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise codec_support.VerificationBundleError(
                f"invalid candidate replay companion '{path}': {error}"
            ) from error
        if (
            site.plan.site_identity != record["site_identity"]
            or site.plan.candidate_identity != record["candidate_identity"]
            or site.plan.plan_identity != record["plan_identity"]
            or site.replay_identity != record["replay_identity"]
        ):
            raise codec_support.VerificationBundleError(
                f"candidate replay companion '{path}' differs from its record"
            )
        restored.append(site)
    return tuple(restored)


def load_candidate_equivalence_replay(
    bundle: bundle_codec.LoadedVerificationBundle | Path,
) -> tuple[object, ...]:
    """Load strict frozen semantic-reference equivalence inputs without compiling source."""

    loaded = load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    payload = loaded.verification_ir.get("payload")
    if not isinstance(payload, Mapping):
        raise codec_support.VerificationBundleError("verification bundle payload is invalid")
    return _validate_candidate_replay_files(
        payload,
        files=loaded.manifest.files,
        jobs=loaded.manifest.jobs,
        read_bytes=loaded.read_bytes,
    )


def _listed_files(directory: Path) -> set[str]:
    root = Path(directory)
    if not root.is_dir():
        raise codec_support.VerificationBundleError(f"verification bundle directory is missing: {root}")
    result: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in tuple(directories):
            path = current_path / name
            if stat.S_ISLNK(path.lstat().st_mode):
                raise codec_support.VerificationBundleError(
                    f"verification bundle contains symbolic link '{path.relative_to(root)}'"
                )
        for name in files:
            path = current_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise codec_support.VerificationBundleError(
                    f"verification bundle contains non-regular file '{path.relative_to(root)}'"
                )
            result.add(path.relative_to(root).as_posix())
    return result


def publish_verification_bundle(
    directory: Path,
    *,
    top: str,
    hardware_identity: str,
    verification_identity: str,
    property_ids: Iterable[str],
    verification_ir: Mapping[str, object],
    files: Iterable[bundle_codec.VerificationBundleInput],
    jobs: Iterable[bundle_codec.VerificationJob],
) -> bundle_codec.VerificationBundleManifest:
    """Publish a deterministic immutable bundle, rejecting collisions."""

    properties = tuple(sorted(codec_support.validate_property_id(item) for item in property_ids))
    if len(set(properties)) != len(properties):
        raise codec_support.VerificationBundleError("verification property IDs must be unique")
    expected_verification_identity = bundle_codec.verification_identity_for(
        top=top,
        hardware_identity=hardware_identity,
        property_ids=properties,
        payload=verification_ir,
    )
    if verification_identity != expected_verification_identity:
        raise codec_support.VerificationBundleError(
            "verification identity does not match verification IR contents"
        )
    inputs = tuple(files)
    paths = tuple(item.logical_path for item in inputs)
    if len(set(paths)) != len(paths):
        raise codec_support.VerificationBundleError("verification bundle input paths must be unique")
    records = tuple(sorted(
        (bundle_codec.VerificationBundleFile.from_input(item) for item in inputs),
        key=lambda item: item.logical_path,
    ))
    ordered_jobs = tuple(sorted(jobs, key=lambda item: item.property_id))
    bundle_codec._validate_verification_payload(
        verification_ir,
        property_ids=properties,
        jobs=ordered_jobs,
    )
    content_by_path = {item.logical_path: item.content for item in inputs}
    _validate_candidate_replay_files(
        verification_ir,
        files=records,
        jobs=ordered_jobs,
        read_bytes=lambda path: content_by_path[path],
    )
    ir_data = bundle_codec._verification_ir_data(
        top=top,
        hardware_identity=hardware_identity,
        verification_identity=verification_identity,
        property_ids=properties,
        payload=verification_ir,
    )
    ir_content = stable_pretty_json(ir_data).encode("utf-8")
    ir_record = bundle_codec.VerificationBundleFile(
        "verification-ir.json",
        "verification_ir",
        hashlib.sha256(ir_content).hexdigest(),
        len(ir_content),
    )
    identities = verification_ir.get("identities", {})
    if not isinstance(identities, Mapping):
        raise codec_support.VerificationBundleError("verification identities must be an object")
    source_identity = identities.get("source")
    dependency_identity = identities.get("dependency")
    compiler_identity = identities.get("compiler")
    if source_identity is None:
        source_identity = "source:" + stable_digest({
            "top": top, "hardware_identity": hardware_identity,
        })
    if dependency_identity is None:
        dependency_identity = "dependency:" + stable_digest({
            "hardware_identity": hardware_identity,
        })
    if compiler_identity is None:
        compiler_identity = "compiler:" + stable_digest({
            "verification_ir_schema": bundle_codec.VERIFICATION_IR_SCHEMA,
        })
    manifest = bundle_codec.VerificationBundleManifest(
        top,
        hardware_identity,
        verification_identity,
        properties,
        ir_record,
        records,
        ordered_jobs,
        codec_support.validate_identity(source_identity, "source identity"),
        codec_support.validate_identity(dependency_identity, "dependency identity"),
        codec_support.validate_identity(compiler_identity, "compiler identity"),
    )
    payloads = [
        (Path(item.logical_path), source.content)
        for item, source in zip(
            records,
            sorted(inputs, key=lambda value: value.logical_path),
            strict=True,
        )
    ]
    payloads.extend((
        (Path("verification-ir.json"), ir_content),
        (Path("manifest.json"), manifest.to_json().encode("utf-8")),
    ))
    expected_paths = {path.as_posix() for path, _ in payloads}
    destination = Path(directory)
    if destination.exists():
        actual_before = _listed_files(destination)
        unexpected_before = sorted(actual_before - expected_paths)
        if unexpected_before:
            raise codec_support.VerificationBundleError(
                "verification bundle directory contains an unexpected file set: "
                f"'{unexpected_before[0]}'"
            )
    try:
        publication.publish_relative_files(directory, payloads, existing="identical")
    except publication.SafePublicationError as error:
        raise codec_support.VerificationBundleError(str(error)) from error
    actual_paths = _listed_files(directory)
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        detail = unexpected[0] if unexpected else missing[0]
        raise codec_support.VerificationBundleError(
            f"verification bundle directory contains an unexpected file set: '{detail}'"
        )
    return manifest


def load_verification_bundle(directory: Path) -> bundle_codec.LoadedVerificationBundle:
    """Load and fully revalidate an immutable bundle before execution."""

    root = Path(directory)
    manifest_path = root / "manifest.json"
    try:
        if stat.S_ISLNK(manifest_path.lstat().st_mode):
            raise codec_support.VerificationBundleError("verification manifest must not be a symbolic link")
        manifest = bundle_codec.VerificationBundleManifest.from_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise codec_support.VerificationBundleError("verification bundle is missing manifest.json") from error
    except UnicodeDecodeError as error:
        raise codec_support.VerificationBundleError("verification manifest is not UTF-8") from error
    except OSError as error:
        raise codec_support.VerificationBundleError(f"cannot read verification manifest: {error}") from error
    all_records = (manifest.verification_ir, *manifest.files)
    try:
        publication.validate_relative_hashes(
            root,
            ((Path(item.logical_path), item.content_hash) for item in all_records),
        )
    except publication.SafePublicationError as error:
        raise codec_support.VerificationBundleError(str(error)) from error
    for item in all_records:
        try:
            actual_size = (root / item.logical_path).stat().st_size
        except OSError as error:
            raise codec_support.VerificationBundleError(
                f"cannot inspect verification bundle file '{item.logical_path}': {error}"
            ) from error
        if actual_size != item.size:
            raise codec_support.VerificationBundleError(
                f"verification bundle file '{item.logical_path}' has the wrong size"
            )
    expected_paths = {"manifest.json", *(item.logical_path for item in all_records)}
    actual_paths = _listed_files(root)
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        detail = unexpected[0] if unexpected else missing[0]
        raise codec_support.VerificationBundleError(
            f"verification bundle directory contains an unexpected file set: '{detail}'"
        )
    try:
        ir_text = (root / manifest.verification_ir.logical_path).read_text(encoding="utf-8")
        ir_data = bundle_codec._validate_verification_ir(json.loads(ir_text))
    except json.JSONDecodeError as error:
        raise codec_support.VerificationBundleError(
            f"invalid verification IR JSON at line {error.lineno} column {error.colno}"
        ) from error
    except UnicodeDecodeError as error:
        raise codec_support.VerificationBundleError("verification IR is not UTF-8") from error
    if (
        ir_data["top"] != manifest.top
        or ir_data["hardware_identity"] != manifest.hardware_identity
        or ir_data["verification_identity"] != manifest.verification_identity
        or tuple(ir_data["property_ids"]) != manifest.property_ids
    ):
        raise codec_support.VerificationBundleError(
            "verification IR identity fields do not match the manifest"
        )
    payload = ir_data["payload"]
    assert isinstance(payload, Mapping)
    bundle_codec._validate_verification_payload(
        payload,
        property_ids=manifest.property_ids,
        jobs=manifest.jobs,
    )
    loaded = bundle_codec.LoadedVerificationBundle(root, manifest, ir_data)
    _validate_candidate_replay_files(
        payload,
        files=manifest.files,
        jobs=manifest.jobs,
        read_bytes=loaded.read_bytes,
    )
    return loaded
