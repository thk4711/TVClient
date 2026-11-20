#!/usr/bin/env python3
"""Offline package/source checks, not target execution or database qualification."""
import hashlib
from email.parser import Parser
import json
from pathlib import Path
import re
import shutil
import sys


PACKAGES = ("satip_controller", "satip_appliance")
STATIC_SUFFIXES = (".html", ".js", ".css", ".txt")
SECURITY_VERSIONS = {
    "zeroconf": "0.149.16", "starlette": "1.3.1", "anyio": "4.14.2",
    "idna": "3.15", "certifi": "2026.10.19",
}


def validate_dependency_versions(site):
    """Read installed wheel metadata without executing AArch64 target code."""
    versions = {name: [] for name in SECURITY_VERSIONS}
    for path in sorted(Path(site).glob("*.dist-info/METADATA")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Linked target Python dependency metadata")
        metadata = Parser().parsestr(path.read_text())
        name = (metadata.get("Name") or "").lower().replace("_", "-")
        if name in versions:
            versions[name].append(metadata.get("Version"))
    for name, expected in SECURITY_VERSIONS.items():
        if versions[name] != [expected]:
            raise ValueError(f"Incorrect target Python security dependency: {name}; use a fresh output")
    return dict(SECURITY_VERSIONS)


def source_files(repo):
    repo = Path(repo)
    sources = [path for package in PACKAGES for path in sorted((repo / package).glob("*.py"))]
    sources += [path for path in sorted((repo / "satip_controller/static").glob("*"))
                if path.suffix in STATIC_SUFFIXES]
    for package in PACKAGES:
        if repo / package / "__init__.py" not in sources:
            raise ValueError("Missing application package")
    for source in sources:
        if not source.is_file() or any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError("Linked or missing application source refused")
    return sources


def application_root(target):
    target = Path(target)
    root = target / "opt/satip"
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("Linked application destination refused")
        if path == target:
            break
    return root


def install_sources(repo, target):
    """Replace only the two package-owned directories, including old bytecode."""
    repo = Path(repo)
    contents = {path.relative_to(repo): path.read_bytes() for path in source_files(repo)}
    root = application_root(target)
    for package in PACKAGES:
        if (root / package).is_symlink():
            raise ValueError("Linked application package destination refused")
    for package in PACKAGES:
        destination = root / package
        if destination.exists():
            shutil.rmtree(destination)
    for relative, content in contents.items():
        installed = root / relative
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(content)
        installed.chmod(0o644)
        for directory in (installed.parent, *installed.parent.parents):
            directory.chmod(0o755)
            if directory == root:
                break


def validate_sources(target, repo):
    """Check the exact installed source inventory and permitted target bytecode."""
    repo = Path(repo)
    root = application_root(target)
    hashes = {}
    sources = source_files(repo)
    expected = {path.relative_to(repo) for path in sources}
    # The pinned target is Python 3.14; compileall emits checked-hash caches.
    caches = {path.parent / "__pycache__" / (path.stem + ".cpython-314.pyc")
              for path in expected if path.suffix == ".py"}
    directories = {parent for path in expected | caches for parent in path.parents}
    actual = set()
    for installed in root.rglob("*"):
        relative = installed.relative_to(root)
        if installed.is_symlink():
            raise ValueError("Linked installed application file refused")
        if installed.is_dir():
            if relative not in directories:
                raise ValueError("Unexpected installed application directory")
        elif installed.is_file() and relative in caches:
            if installed.read_bytes()[4:8] != b"\x03\x00\x00\x00":
                raise ValueError("Application bytecode is not checked-hash")
        else:
            actual.add(relative)
    if actual != expected:
        raise ValueError("Installed application inventory differs from staged source")
    for source in sources:
        relative = source.relative_to(repo)
        installed = root / relative
        if not installed.is_file() or installed.is_symlink() or installed.read_bytes() != source.read_bytes():
            raise ValueError("Installed application differs from staged source")
        hashes[str(relative)] = hashlib.sha256(installed.read_bytes()).hexdigest()
    return hashes


def validate_mux_grace(output, repo):
    manifest_path = Path(repo) / "buildroot/external/patches/tvheadend/idle-mux-grace.json"
    manifest = json.loads(manifest_path.read_text())
    patch = manifest_path.with_name("0001-idle-mux-grace.patch")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("Tvheadend idle-mux patch differs from reviewed manifest")
    source = Path(output) / "build" / ("tvheadend-" + manifest["revision"])
    for name, digest in manifest["files"].items():
        source_file = source / name
        if not source_file.is_file() or hashlib.sha256(source_file.read_bytes()).hexdigest() != digest:
            raise ValueError("Tvheadend idle-mux grace source missing/stale; use a fresh output")
        if name.endswith(".c"):
            obj = source / "build.linux" / Path(name).with_suffix(".o")
            if not obj.is_file() or obj.read_bytes()[:4] != b"\x7fELF":
                raise ValueError("Tvheadend idle-mux grace object missing")
    return {"patch_sha256": manifest["patch_sha256"], "source_sha256": manifest["files"]}


def validate(target, output, repo, checkout):
    target, output = Path(target), Path(output)
    hashes = validate_sources(target, repo)
    site = target / "usr/lib/python3.14/site-packages"
    stdlib = site.parent
    # Buildroot defaults to sourceless bytecode beside the original module path.
    expat = stdlib / "xml/parsers/expat"
    if (not any(expat.with_suffix(suffix).is_file() for suffix in (".py", ".pyc"))
            or not list((stdlib / "lib-dynload").glob("pyexpat*.so"))):
        raise ValueError("Missing target SAT-IP XML parser")
    if not list((stdlib / "lib-dynload").glob("zlib*.so")):
        raise ValueError("Missing target Python zlib (required by HTTPX management/preview)")
    for module in ("evdev", "zeroconf", "ifaddr", "fastapi", "uvicorn", "httpx", "starlette", "pydantic", "pydantic_core", "anyio", "idna", "certifi"):
        if not (site / module).is_dir():
            raise ValueError("Missing target Python dependency")
    dependency_versions = validate_dependency_versions(site)
    binary = target / "usr/bin/tvheadend"
    if binary.read_bytes()[:4] != b"\x7fELF":
        raise ValueError("Missing backend ELF")
    configuration = output / "build/tvheadend-37453bc3fe5f9e10f3428ebb1abdc613f8b07186/build.linux/build.h"
    text = configuration.read_text()
    for feature, value in (("SATIP_CLIENT", "1"), ("SATIP_SERVER", "0"), ("IPTV", "1")):
        if not re.search(r"^#define\s+ENABLE_" + feature + r"\s+" + value + r"\b", text, re.M):
            raise ValueError("Incorrect backend SAT-IP client/server feature selection")
    result = {"schema": 1, "application_version": "0.1.1", "files": hashes,
              "python_security_dependencies": dependency_versions,
              "backend_revision": "37453bc3fe5f9e10f3428ebb1abdc613f8b07186",
              "backend_database_qualified": False, "persistent_schema": 1,
              "backend_mux_grace": validate_mux_grace(output, repo)}
    (output / "satip-app-build.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    if sys.argv[1:2] == ["--install-sources"]:
        install_sources(Path(sys.argv[2]), Path(sys.argv[3]))
        raise SystemExit(0)
    target, output = map(Path, sys.argv[1:])
    repo = Path(__file__).resolve().parents[4]
    validate(target, output, repo, Path.cwd())
    print("SAT-IP application sources, Python dependencies and backend features verified offline")
