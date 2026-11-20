#!/usr/bin/env python3
"""Identify development inputs without requiring a first Git commit."""

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys


def source_identity(encoded, inputs_sha256):
    """Validate wrapper provenance without transferring Git/private files."""
    identity = json.loads(encoded)
    if not isinstance(identity, dict) or set(identity) != {
            "source_commit", "source_dirty", "build_inputs_sha256"}:
        raise ValueError("Invalid originating source identity")
    commit, dirty = identity["source_commit"], identity["source_dirty"]
    if (commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit))
            or dirty is not None and type(dirty) is not bool
            or identity["build_inputs_sha256"] != inputs_sha256):
        raise ValueError("Originating source identity does not match staged inputs")
    return identity


def reject_overlay_caches(target, files):
    """Local Buildroot overlay copying must not publish workstation bytecode."""
    overlay = Path("buildroot/external/board/satip/rootfs-overlay")
    directories = {Path(name).relative_to(overlay).parent for name in files
                   if Path(name).is_relative_to(overlay)}
    for relative in sorted(directories):
        cache = target / relative / "__pycache__"
        if cache.exists() or cache.is_symlink():
            raise ValueError("Generated Python cache in image overlay; remove the source overlay "
                             f"cache {str(overlay / relative / '__pycache__')!r} and rebuild "
                             "with a clean target (PYTHONDONTWRITEBYTECODE=1 prevents new caches)")


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    repo, checkout, output = map(Path, sys.argv[1:])
    inventory = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                  "scripts/source_manifest.py"))
    try:
        files = inventory["build_inputs"](repo)
        reject_overlay_caches(output / "target", files)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    def git(directory, *args):
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    status = git(repo, "status", "--porcelain")
    data = {"schema": 1, "product": "satip", "hardware": "rpi4-aarch64",
            "profile": "development-foundation", "release": False,
            "source_commit": git(repo, "rev-parse", "HEAD"),
            "source_dirty": None if status is None else bool(status),
            "build_inputs_sha256": hashlib.sha256(canonical).hexdigest(),
            "build_inputs": files,
            "resolved_config_sha256": hashlib.sha256((output / ".config").read_bytes()).hexdigest(),
            "buildroot_commit": git(checkout, "rev-parse", "HEAD"),
            "kernel_commit": "21b410140c47ffab5668399f6f143c7d7b935c8b",
            "firmware_commit": "063bcab6c8a90efb0d19f69d88cbbc7ec79cab68"}
    if "SATIP_SOURCE_IDENTITY" in os.environ:
        data.update(source_identity(os.environ["SATIP_SOURCE_IDENTITY"], data["build_inputs_sha256"]))
    media = output / "satip-media-build.json"
    if media.is_file():
        data["media"] = json.loads(media.read_text())
    application = output / "satip-app-build.json"
    if application.is_file():
        data["application"] = json.loads(application.read_text())
    (output / "satip-build.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
