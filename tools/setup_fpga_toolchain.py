#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Provision the pinned OSS FPGA tools without changing project environments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tarfile
import tempfile

from zlang.common.files import file_digest
from zlang.fpga_toolchain import (
    FpgaToolchainError, OSS_ARCHIVE_SHA256, OSS_FILES_SHA256, OSS_VERSION,
    SCHEMA, inventory_digest, inventory_tree, load_toolchain,
)


def provision(archive: Path, destination: Path, manifest: Path) -> None:
    """Extract one authenticated distribution; never replace an installation."""
    archive = archive.resolve()
    destination = destination.absolute()
    manifest = manifest.absolute()
    if file_digest(archive) != OSS_ARCHIVE_SHA256:
        raise FpgaToolchainError("OSS CAD Suite archive digest does not match pin")
    if destination.exists() or destination.is_symlink() or manifest.exists():
        raise FpgaToolchainError("choose fresh destination and manifest paths")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fpga-stage-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive) as bundle:
            bundle.extractall(stage, filter="data")
        source = stage / "oss-cad-suite"
        rows = inventory_tree(source)
        if inventory_digest(rows) != OSS_FILES_SHA256:
            raise FpgaToolchainError("installed distribution differs from pinned inventory")
        source.rename(destination)
    write_manifest(destination, manifest)


def write_manifest(root: Path, manifest: Path) -> None:
    """Bind an existing exact installation, rejecting altered distributions."""
    root, manifest = root.resolve(), manifest.absolute()
    if manifest.exists():
        raise FpgaToolchainError("toolchain manifest already exists")
    rows = inventory_tree(root)
    if inventory_digest(rows) != OSS_FILES_SHA256:
        raise FpgaToolchainError("existing installation differs from pinned inventory")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schema": SCHEMA, "root": str(root), "version": OSS_VERSION,
        "archive_sha256": OSS_ARCHIVE_SHA256,
        "files_sha256": OSS_FILES_SHA256, "files": rows,
    }, indent=2, sort_keys=True) + "\n")
    load_toolchain(manifest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.archive:
            provision(args.archive, args.destination, args.manifest)
        else:
            write_manifest(args.destination, args.manifest)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(2, f"FPGA provisioning failed: {error}\n")
    print(f"FPGA toolchain verified: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
