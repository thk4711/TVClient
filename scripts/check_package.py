#!/usr/bin/env python3
"""Check wheel/sdist inventories and install wheels outside the source checkout."""
import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import venv
import zipfile


PACKAGES = ("satip_controller", "satip_appliance")
LICENCES = ("LICENSE", "satip_controller/static/hls-LICENSE.txt",
            "satip_controller/static/qrcode-LICENSE.txt")
EXPRESSION = "MIT AND Apache-2.0"


def require(condition, message):
    # These checks must also run when the caller enables Python optimization.
    if not condition:
        raise ValueError(message)


def payload_name(name):
    parts = PurePosixPath(name).parts
    return ((len(parts) == 2 and parts[0] in PACKAGES and parts[1].endswith(".py"))
            or (len(parts) == 3 and parts[:2] == ("satip_controller", "static")
                and PurePosixPath(name).suffix in (".html", ".js", ".css", ".txt")))


def safe_name(name):
    path = PurePosixPath(name)
    return (bool(path.parts) and not path.is_absolute() and ".." not in path.parts
            and str(path) == name.rstrip("/") and "\\" not in name
            and all(not part.startswith(".") for part in path.parts))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_payload(root):
    paths = [path for package in PACKAGES for path in (root / package).glob("*.py")]
    paths += [path for path in (root / "satip_controller/static").glob("*")
              if payload_name(path.relative_to(root).as_posix())]
    require(all(path.is_file() and not path.is_symlink() for path in paths),
            "Missing or linked package source")
    return {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in paths}


def inspect_wheel(wheel, expected=None):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and all(safe_name(name) for name in names),
                "Unsafe or duplicate wheel member")
        require(all((item.external_attr >> 16) & 0o170000 != 0o120000
                    for item in archive.infolist()), "Linked wheel member")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata_names) == 1, "Expected exactly one wheel metadata file")
        metadata_name = metadata_names[0]
        dist_info = metadata_name.rsplit("/", 1)[0]
        metadata = Parser().parsestr(archive.read(metadata_name).decode())
        require(metadata["Name"] == "satip-controller", "Incorrect wheel project")
        require(metadata["License-Expression"] == EXPRESSION, "Incorrect wheel license expression")
        require(set(metadata.get_all("License-File", [])) == set(LICENCES),
                "Incomplete wheel license metadata")
        metadata_files = {dist_info + "/" + name for name in
                          ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
        metadata_files |= {dist_info + "/licenses/" + name for name in LICENCES}
        require(all(payload_name(name) or name in metadata_files for name in names),
                "Unexpected wheel member")
        for name in LICENCES:
            notice = archive.read(dist_info + "/licenses/" + name)
            require(bool(notice), "Empty wheel license")
            if name != "LICENSE":
                require(notice == archive.read(name), "Wheel license copies differ")
        apache = archive.read("satip_controller/static/hls-LICENSE.txt").decode()
        require("TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in apache
                and "END OF TERMS AND CONDITIONS" in apache,
                "Incomplete Apache license")
        payload = {name: digest(archive.read(name)) for name in names if payload_name(name)}
        require(all(package + "/__init__.py" in payload for package in PACKAGES),
                "Missing Python package")
        if expected is not None:
            require(payload == expected, "Wheel payload differs from source inventory")
        return payload


def inspect_sdist(sdist, expected=None):
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        require(len(names) == len(set(names)) and all(safe_name(name) for name in names),
                "Unsafe or duplicate sdist member")
        roots = {PurePosixPath(name).parts[0] for name in names}
        require(len(roots) == 1, "Expected one sdist root")
        root = next(iter(roots))
        files = {}
        for member in members:
            require(member.isdir() or member.isfile(), "Linked or special sdist member")
            if member.isfile():
                relative = PurePosixPath(member.name).relative_to(root).as_posix()
                files[relative] = archive.extractfile(member).read()
        # setuptools synthesizes setup.cfg with egg-info release tags in sdists.
        metadata_files = {"pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "PKG-INFO", "setup.cfg"}
        metadata_files |= {"satip_controller.egg-info/" + name for name in
                           ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
                            "requires.txt", "top_level.txt")}
        require(all(payload_name(name) or name in metadata_files for name in files),
                "Unexpected sdist member (tests/image sources are distributed separately)")
        require({"pyproject.toml", "MANIFEST.in", "README.md", "PKG-INFO", *LICENCES} <= files.keys(),
                "Incomplete sdist metadata/licenses")
        payload = {name: digest(data) for name, data in files.items() if payload_name(name)}
        if expected is not None:
            require(payload == expected, "Sdist payload differs from source inventory")
        return root, payload


def isolated_env():
    return {key: value for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}


def make_venv(path):
    venv.EnvBuilder(with_pip=True).create(path)
    return path / "bin/python"


def check_install(wheel, payload):
    with tempfile.TemporaryDirectory(prefix="satip-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        python = make_venv(environment)
        env = isolated_env()
        subprocess.run([str(python), "-I", "-m", "pip", "install", str(wheel)],
                       cwd=root, env=env, check=True)
        subprocess.run([str(python), "-I", "-m", "pip", "check"], cwd=root, env=env, check=True)
        for entry in ("satip-player", "satip-web"):
            subprocess.run([str(environment / "bin" / entry), "--help"], cwd=root,
                           env=env, check=True, stdout=subprocess.DEVNULL)
        code = "import hashlib, json, pathlib, sys, satip_controller, satip_appliance; "
        code += "root=pathlib.Path(satip_controller.__file__).parent.parent; "
        code += "assert root.is_relative_to(sys.prefix); "
        code += "assert pathlib.Path(satip_appliance.__file__).is_relative_to(sys.prefix); "
        code += "expected=json.loads(sys.argv[1]); "
        code += "assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==value for name,value in expected.items())"
        subprocess.run([str(python), "-I", "-c", code, json.dumps(payload)],
                       cwd=root, env=env, check=True)


def check_sdist(sdist, expected):
    name, payload = inspect_sdist(sdist, expected)
    with tempfile.TemporaryDirectory(prefix="satip-sdist-") as directory:
        root = Path(directory)
        with tarfile.open(sdist, "r:gz") as archive:
            archive.extractall(root, filter="data")
        python = make_venv(root / "builder")
        output = root / "wheels"
        subprocess.run([str(python), "-I", "-m", "pip", "wheel", "--use-pep517", "--no-deps",
                        "--no-cache-dir", "--wheel-dir", str(output), str(root / name)],
                       cwd=root, env=isolated_env(), check=True)
        wheels = list(output.glob("*.whl"))
        require(len(wheels) == 1, "Expected exactly one wheel built from sdist")
        built = inspect_wheel(wheels[0], payload)
        check_install(wheels[0], built)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+", help="Wheel(s) and/or .tar.gz sdist(s)")
    args = parser.parse_args()
    expected = source_payload(Path(__file__).resolve().parents[1])
    for artifact in args.artifacts:
        artifact = artifact.resolve()
        if artifact.name.endswith(".whl"):
            check_install(artifact, inspect_wheel(artifact, expected))
        elif artifact.name.endswith(".tar.gz"):
            check_sdist(artifact, expected)
        else:
            parser.error("Expected .whl or .tar.gz artifact")
    print("Exact package inventories, isolated builds/installations, entry points and notices passed")


if __name__ == "__main__":
    main()
