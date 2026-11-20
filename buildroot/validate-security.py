#!/usr/bin/env python3
"""Check security overrides after actual upstream package recipe evaluation."""
from pathlib import Path
import runpy
import subprocess
import sys


def validate(checkout, output, repo):
    repo = Path(repo).absolute()
    external = repo / "buildroot/external"
    versions = runpy.run_path(str(external / "board/satip/validate-app-build.py"))["SECURITY_VERSIONS"]
    expected = {}
    for name, version in versions.items():
        prefix = "PYTHON_" + name.upper()
        hashes = str(external / f"patches/python-{name}/{version}/python-{name}.hash")
        expected[prefix + "_VERSION"] = version
        expected[prefix + "_DL_VERSION"] = version
        expected[prefix + "_SOURCE"] = f"{name}-{version}.tar.gz"
        expected[prefix + "_HASH_FILES"] = hashes
        if name in ("certifi", "idna"):
            expected["HOST_" + prefix + "_VERSION"] = version
            expected["HOST_" + prefix + "_HASH_FILES"] = hashes
    result = subprocess.run(["make", "-s", "--no-print-directory", "-C", str(checkout),
        "HOSTARCH=x86_64", "O=" + str(output), "BR2_EXTERNAL=" + str(external),
        "printvars", "VARS=" + " ".join(expected)], capture_output=True, text=True, check=True)
    actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    for name, value in expected.items():
        if actual.get(name) != value:
            raise ValueError(f"Buildroot security override not retained: {name}")
    return versions


if __name__ == "__main__":
    validate(Path(sys.argv[1]), Path(sys.argv[2]), Path(__file__).resolve().parents[1])
    print("Buildroot Python security versions, sources and hashes verified")
