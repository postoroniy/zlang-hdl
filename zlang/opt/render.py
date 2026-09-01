"""Stable human-readable rendering for canonical optimization IR."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from collections.abc import Mapping

from zlang.common import stable_json
from zlang.opt.ir import CanonicalModule, NodeCategory
from zlang.source import SourceOrigin


def render(module: CanonicalModule, *, include_origins: bool = True) -> str:
    """Render stable canonical IR, optionally omitting diagnostic origins.

    Source origins are useful in reports but are deliberately excluded from
    normalized semantic hashes because whitespace-only edits change their spans.
    """
    counts = " ".join(
        f"{category.value}={len(module.nodes(category))}"
        for category in NodeCategory
    )
    lines = [
        f"module {module.name}",
        f"stage {module.stage.value}",
        f"nodes {counts}",
    ]
    if module.root_module_identity is not None:
        root = module.root_module_identity
        lines.append(
            "root-module "
            f"logical_path={root.logical_path} digest={root.digest} "
            f"package={root.package_identity} "
            f"revision={root.package_revision or 'none'}"
        )
    if module.dependency_closure is not None:
        lines.append(
            "dependency-closure "
            f"schema={module.dependency_closure.schema} "
            f"lock={module.dependency_closure.lock_identity} "
            f"identity={module.dependency_closure.identity}"
        )
        for dependency in module.dependency_closure.modules:
            lines.append(
                "dependency-module "
                f"logical_path={dependency.logical_path} "
                f"digest={dependency.digest} "
                f"package={dependency.package_identity} "
                f"revision={dependency.package_revision or 'none'}"
            )
    if module.timing_contract is not None:
        contract = module.timing_contract
        origin = (
            contract.source_origin.render()
            if include_origins and contract.source_origin is not None
            else "none" if include_origins else "omitted"
        )
        lines.append(
            "timing "
            f"latency={contract.latency} "
            f"ii={contract.initiation_interval} "
            f"clock={contract.clock_domain or 'none'} "
            f"reset={contract.reset_domain or 'none'} "
            f"origin={origin}"
        )
    for item in module.output_timings:
        lines.append(
            f"output-timing port={item.port} value={_render_timing(item.timing)}"
        )
    for item in module.instance_output_timings:
        lines.append(
            "instance-output-timing "
            f"instance={item.instance} port={item.port} "
            f"value={_render_timing(item.timing)}"
        )
    lines.append("expressions")
    for node in module.expressions:
        metadata = node.metadata
        operands = " ".join(f"%{operand}" for operand in node.operands)
        attributes = " ".join(
            f"{name}={_render_value(value)}" for name, value in node.attributes
        )
        domains = ",".join(metadata.domains) or "none"
        effects = ",".join(effect.value for effect in metadata.effects) or "none"
        origins = (
            ",".join(origin.render() for origin in node.origins) or "none"
            if include_origins
            else "omitted"
        )
        semantic_metadata = (
            f"width={metadata.width} signedness={metadata.signedness.value} "
            f"latency={metadata.latency} ii={metadata.initiation_interval} "
            f"domains=[{domains}] purity={metadata.purity.value} "
            f"effects=[{effects}] origins=[{origins}]"
        )
        tail = " ".join(item for item in (operands, attributes) if item)
        lines.append(
            f"  %{node.id} {node.category.value}.{node.op.value} "
            f"type={node.type} {semantic_metadata}"
            f"{(' ' + tail) if tail else ''}"
        )
    lines.append("entities")
    for entity in module.entities:
        roots = " ".join(f"%{root}" for root in entity.roots)
        details = " ".join(f"{key}={value}" for key, value in entity.details)
        tail = " ".join(item for item in (roots, details) if item)
        lines.append(
            f"  {entity.id} {entity.category.value}.{entity.kind} "
            f"name={entity.name}{(' ' + tail) if tail else ''}"
        )
    return "\n".join(lines) + "\n"


def render_identity(module: CanonicalModule) -> str:
    """Render the complete canonical IR for content-identity hashing.

    The ordinary :func:`render` output is intentionally concise and aimed at
    humans.  It does not print every losslessly retained module field, so it
    must not be used as a semantic content identity.  This representation
    walks the complete canonical dataclass graph, tags nominal Python types,
    and omits only diagnostic source origins.  In particular, public module
    signatures, types, domains, timing, dependency identities, exploration
    selections, and protocol/storage metadata all participate.

    Source locations and source-unit digests carried *by* ``SourceOrigin`` do
    not participate: moving an otherwise identical declaration must not alter
    canonical semantic identity.  Project/dependency content identities are
    separate semantic inputs and remain included.
    """

    return stable_json(_identity_value(module))


_ORIGIN_FIELDS = frozenset(
    {
        "origin",
        "origins",
        "source_origin",
        # Standalone compile_file uses its physical path as diagnostic source
        # provenance.  Locked-project logical module/digest identity is kept
        # separately in root_module_identity/dependency_closure.
        "source_identity",
        "source_hash",
        "source_path",
        # Verification declarations intentionally have a separate identity;
        # source assertions must not invalidate selected-hardware/M36/M38 keys.
        "verification_scopes",
        "verification_expressions",
    }
)


def _identity_value(value: object) -> object:
    """Convert canonical IR values into a strict JSON-compatible tree."""

    if isinstance(value, SourceOrigin):
        # Defensive for origins stored outside the conventional field names.
        return {"$source_origin": "omitted"}
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [
                    item.name,
                    _identity_field_value(item.name, getattr(value, item.name)),
                ]
                for item in fields(value)
                if item.name not in _ORIGIN_FIELDS
            ],
        }
    if isinstance(value, Mapping):
        entries = [
            (_identity_value(key), _identity_value(item))
            for key, item in value.items()
        ]
        entries.sort(key=lambda pair: stable_json(pair[0]))
        return {"$mapping": [[key, item] for key, item in entries]}
    if isinstance(value, (tuple, list)):
        return [_identity_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_identity_value(item) for item in value]
        items.sort(key=stable_json)
        return {"$set": items}
    if isinstance(value, Path):
        return {"$path": value.as_posix()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        "canonical IR identity cannot serialize "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _identity_field_value(name: str, value: object) -> object:
    if name == "declaration_identity" and isinstance(value, str):
        source, separator, declaration = value.partition("::")
        if separator and Path(source).is_absolute():
            # Preserve the nominal declaration kind/name without letting a
            # checkout location become its semantic content identity.
            value = "$source::" + declaration
    return _identity_value(value)


def _render_value(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return "[" + ",".join(_render_value(item) for item in value) + "]"
    return str(value)


def _render_timing(value: object) -> str:
    knowledge = getattr(value, "knowledge", None)
    label = getattr(knowledge, "value", str(knowledge))
    if label == "known":
        return f"known({getattr(value, 'latency')})"
    if label == "unknown":
        return f"unknown({getattr(value, 'reason')})"
    return "timeless"
