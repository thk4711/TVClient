#!/usr/bin/env python3
"""Build and collect verified SAT-IP foundation images locally or over SSH."""

from __future__ import annotations

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

REPOSITORY = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name("build-image.ini")
ARTIFACTS = ("sdcard.img", "rootfs.ext4", "Image", "bcm2711-rpi-4-b.dtb",
             "satip-build.json")
PIN = "72d9d4fa636a371ef9eb99c92a735ce9f6d829d5"
KEYS = {"execution", "repository", "host", "ssh_port", "remote_root",
        "buildroot_dir", "output_dir", "download_cache", "artifacts_dir",
        "jobs", "zip_image", "install_host_packages"}
HOST_PACKAGES = ("build-essential", "bc", "bison", "flex", "libncurses-dev",
                 "libssl-dev", "rsync", "cpio", "unzip", "wget", "file", "git",
                 "python3", "gawk", "perl", "patch", "gzip", "bzip2", "xz-utils",
                 "tar", "ca-certificates", "openssh-client")


class BuildError(RuntimeError):
    """A configuration, build or verification failure."""


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(repository, expected_inputs):
    """Carry only public Git provenance, bound to the staged input digest."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(repository), *args],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    canonical = json.dumps(expected_inputs, sort_keys=True, separators=(",", ":")).encode()
    return {"source_commit": commit, "source_dirty": None if status is None else bool(status),
            "build_inputs_sha256": hashlib.sha256(canonical).hexdigest()}


def inputs(repository):
    """Use the same reviewed inventory as local and staged image metadata."""
    inventory = runpy.run_path(str(Path(__file__).with_name("source_manifest.py")))
    try:
        return inventory["build_inputs"](repository)
    except ValueError as error:
        raise BuildError(str(error)) from None


def validate_build_path(value):
    if not value or not value.startswith("/") or value == "/" or any(c.isspace() or c == ":" or ord(c) < 32 for c in value):
        raise BuildError("build paths must be absolute, non-root, without whitespace or colons")
    if ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
        raise BuildError("build paths must be normalized")


def overlaps(left, right):
    a, b = PurePosixPath(left), PurePosixPath(right)
    return a == b or a in b.parents or b in a.parents


def parse_args(argv=None):
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path)
    first, _ = preliminary.parse_known_args(argv)
    config = first.config.expanduser() if first.config else CONFIG
    values = {}
    if first.config or config.exists():
        ini = configparser.ConfigParser(interpolation=None)
        try:
            with config.open() as stream:
                ini.read_file(stream)
            if ini.defaults() or ini.sections() != ["build_image"]:
                raise BuildError("INI must contain only [build_image], without DEFAULT values")
            values = dict(ini["build_image"])
        except (OSError, configparser.Error) as exc:
            raise BuildError(f"cannot read configuration: {exc}") from exc
        if set(values) - KEYS:
            raise BuildError("unknown configuration keys: " + ", ".join(sorted(set(values) - KEYS)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--execution", choices=("local", "remote"), default=values.get("execution", "local"))
    parser.add_argument("--repository", type=Path, default=values.get("repository", str(REPOSITORY)))
    parser.add_argument("--host", default=values.get("host", ""))
    parser.add_argument("--ssh-port", type=int, default=values.get("ssh_port", "22"))
    parser.add_argument("--remote-root", default=values.get("remote_root", ""))
    for key in ("buildroot_dir", "output_dir", "download_cache"):
        parser.add_argument("--" + key.replace("_", "-"), default=values.get(key))
    parser.add_argument("--artifacts-dir", type=Path, default=values.get("artifacts_dir", str(REPOSITORY / "artifacts")))
    parser.add_argument("--jobs", type=int, default=values.get("jobs", "16"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adopt-source", action="store_true", help="adopt unmarked remote source only when all build-input hashes already match")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--zip", dest="zip_image", action="store_true")
    group.add_argument("--no-zip", dest="zip_image", action="store_false")
    apt_group = parser.add_mutually_exclusive_group()
    apt_group.add_argument("--install-host-packages", dest="install_host_packages",
                           action="store_true", help="install Debian prerequisites on the build host (requires non-interactive sudo)")
    apt_group.add_argument("--no-apt", dest="install_host_packages", action="store_false",
                           help="do not install build-host packages (default)")
    for key, default in (("zip_image", "true"), ("install_host_packages", "false")):
        boolean = values.get(key, default).lower()
        if boolean not in configparser.ConfigParser.BOOLEAN_STATES:
            raise BuildError(f"{key} must be a boolean")
        parser.set_defaults(**{key: configparser.ConfigParser.BOOLEAN_STATES[boolean]})
    args = parser.parse_args(argv)
    if args.execution not in ("local", "remote") or args.jobs < 1 or not 1 <= args.ssh_port <= 65535:
        raise BuildError("invalid execution, jobs or SSH port")
    if args.execution == "remote" and (args.host.startswith("-") or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", args.host)):
        raise BuildError("remote host must be a plain SSH hostname or user@hostname")
    args.repository = Path(args.repository).expanduser().resolve()
    args.artifacts_dir = Path(args.artifacts_dir).expanduser().resolve()
    root = args.remote_root if args.execution == "remote" else str(Path.home() / "embedded/satip")
    if args.execution == "remote":
        validate_build_path(root)
    args.source = root.rstrip("/") + "/source" if args.execution == "remote" else str(args.repository)
    defaults = {"buildroot_dir": root + "/buildroot", "output_dir": root + "/output/rpi4-64",
                "download_cache": str(PurePosixPath(root).parent / "dl")}
    for key, default in defaults.items():
        value = getattr(args, key) or default
        if args.execution == "local":
            value = str(Path(value).expanduser().absolute())
        validate_build_path(value)
        setattr(args, key, value)
    validate_build_path(args.source)
    locations = [args.source, args.buildroot_dir, args.output_dir, args.download_cache]
    for i, left in enumerate(locations):
        for right in locations[i + 1:]:
            if overlaps(left, right):
                raise BuildError("source, checkout, output and cache must be disjoint")
    if args.execution == "local" and any(overlaps(str(args.artifacts_dir), path) for path in locations[1:]):
        raise BuildError("artifacts must not overlap checkout, output or cache")
    return args


def run(argv, *, capture=False, log=None, env=None):
    display = [str(x) if len(str(x)) < 300 else "<inline script or manifest>" for x in argv]
    print("+", shlex.join(display), flush=True)
    try:
        if log is not None:
            with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env) as process:
                with Path(log).open("w") as stream:
                    for line in process.stdout:
                        print(line, end="", flush=True)
                        stream.write(line)
                code = process.wait()
            if code:
                raise BuildError(f"build failed (exit {code}); retained log: {log}")
            return ""
        result = subprocess.run(argv, text=True, capture_output=capture, env=env, check=False)
    except OSError as exc:
        raise BuildError(f"cannot execute {argv[0]}: {exc}") from exc
    if result.returncode:
        raise BuildError(f"command failed (exit {result.returncode}): {argv[0]}" + ("; " + result.stderr.strip() if capture else ""))
    return result.stdout if capture else ""


def ssh(args, command, **kwargs):
    return run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                "-p", str(args.ssh_port), args.host, command], **kwargs)


def prepare_host(args):
    """Check the platform, optionally provision it, then check build tools."""
    def execute(command, *, capture=True):
        if args.execution == "remote":
            return ssh(args, shlex.join(command), capture=capture)
        return run(command, capture=capture)

    platform_check = "import platform; from pathlib import Path; assert platform.system()=='Linux' and platform.machine()=='x86_64' and Path('/etc/debian_version').is_file(), 'native amd64 Debian required'"
    execute(["python3", "-c", platform_check])
    if args.install_host_packages:
        # SSH has no stdin: fail rather than hang on a sudo password prompt.
        install = """set -eu
command -v apt-get >/dev/null || { echo 'apt-get is required to install host packages' >&2; exit 1; }
if [ "$(id -u)" -eq 0 ]; then
    set -- env DEBIAN_FRONTEND=noninteractive apt-get
else
    command -v sudo >/dev/null || { echo 'host package installation requires sudo; install prerequisites manually or use --no-apt' >&2; exit 1; }
    sudo -n true || { echo 'host package installation requires non-interactive sudo; install prerequisites manually or use --no-apt' >&2; exit 1; }
    set -- sudo -n env DEBIAN_FRONTEND=noninteractive apt-get
fi
"$@" update
"$@" install -y """ + shlex.join(HOST_PACKAGES)
        execute(["sh", "-c", install], capture=False)
    tools_check = "import shutil; assert all(shutil.which(t) for t in ['python3','make','git','rsync']), 'missing build tools; install prerequisites manually or use --install-host-packages'"
    execute(["python3", "-c", tools_check])


# Same guards locally and remotely. mkdir locks fail closed after a crash;
# never steal stale locks or delete foreign files automatically.
GUARD = r'''
import hashlib,json,sys
from pathlib import Path
request=json.loads(sys.argv[1])
def safe(path):
    path=Path(path)
    for item in [path,*path.parents]:
        if item.is_symlink(): raise RuntimeError('symlink in managed path refused')
    return path
locks=[safe(p) for p in request['locks']]
token=request['token']
if request['action']=='release':
    for lock in reversed(locks):
        if (lock/'token').read_text()!=token: raise RuntimeError('lock ownership mismatch')
        (lock/'token').unlink(); lock.rmdir()
else:
    acquired=[]
    try:
        for lock in locks:
            lock.parent.mkdir(parents=True,exist_ok=True)
            lock.mkdir(); acquired.append(lock); (lock/'token').write_text(token)
        for path in request['paths']: safe(path)
        output=Path(request['output']); output_marker=output/'.satip-foundation-output'
        if output.exists() and any(output.iterdir()):
            if output_marker.is_symlink() or not output_marker.is_file():
                raise RuntimeError('nonempty unowned output refused')
            if output_marker.read_text().strip()!=request['output_owner']:
                raise RuntimeError('output belongs to another source/config; use a fresh output')
        if request.get('source'):
            source=safe(request['source']); marker=source/'.satip-build-source.json'
            expected=request['inputs']; owner=request['owner']
            if source.exists():
                actual={}
                for p in source.rglob('*'):
                    if p.is_symlink(): raise RuntimeError('symlink in staged source refused')
                    if p.is_file() and '__pycache__' not in p.parts and p!=marker:
                        name=p.relative_to(source).as_posix()
                        if name=='buildroot/README.md' and name not in expected: continue
                        actual[name]=hashlib.sha256(p.read_bytes()).hexdigest()
                if set(actual)-set(expected): raise RuntimeError('unexpected staged files; use a fresh source/output')
                if marker.exists():
                    if json.loads(marker.read_text())!={'schema':1,'owner':owner}:
                        raise RuntimeError('staged source belongs to another repository')
                elif actual and (not request['adopt'] or actual!=expected):
                    raise RuntimeError('unowned source; --adopt-source requires matching input hashes')
            source.mkdir(parents=True,exist_ok=True)
            marker.write_text(json.dumps({'schema':1,'owner':owner}))
    except BaseException:
        for lock in reversed(acquired):
            (lock/'token').unlink(missing_ok=True); lock.rmdir()
        raise
'''


def guard(args, request):
    command = ["python3", "-c", GUARD, json.dumps(request)]
    if args.execution == "remote":
        ssh(args, shlex.join(command), capture=True)
    else:
        run(command, capture=True)


def verify(directory, expected_inputs, identity=None):
    sums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in sums or match[2] not in ARTIFACTS:
            raise BuildError("invalid artifact checksum manifest")
        sums[match[2]] = match[1]
    if set(sums) != set(ARTIFACTS):
        raise BuildError("artifact checksum manifest is incomplete")
    for name, digest in sums.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0 or sha256(path) != digest:
            raise BuildError(f"artifact checksum mismatch: {name}")
    metadata = json.loads((directory / "satip-build.json").read_text())
    canonical = json.dumps(expected_inputs, sort_keys=True, separators=(",", ":")).encode()
    if (metadata.get("product") != "satip" or metadata.get("release") is not False
            or metadata.get("hardware") != "rpi4-aarch64"
            or metadata.get("profile") != "development-foundation"
            or metadata.get("buildroot_commit") != PIN
            or metadata.get("build_inputs") != expected_inputs
            or metadata.get("build_inputs_sha256") != hashlib.sha256(canonical).hexdigest()):
        raise BuildError("image metadata does not match requested foundation inputs")
    if identity is not None and any(metadata.get(key) != value for key, value in identity.items()):
        raise BuildError("image Git provenance does not match the originating repository")
    config = directory / "resolved.config"
    if config.is_symlink() or sha256(config) != metadata.get("resolved_config_sha256"):
        raise BuildError("resolved configuration does not match image metadata")
    return sums


def package_image(directory, expected):
    image = directory / "sdcard.img"
    archive = directory / "sdcard.img.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, allowZip64=True) as zipped:
        zipped.write(image, "sdcard.img")
    with zipfile.ZipFile(archive) as zipped:
        if zipped.namelist() != ["sdcard.img"] or zipped.testzip() is not None:
            raise BuildError("ZIP integrity check failed")
        digest = hashlib.sha256()
        with zipped.open("sdcard.img") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise BuildError("ZIP image checksum mismatch")
    (directory / "ZIP_SHA256SUMS").write_text(f"{sha256(archive)}  sdcard.img.zip\n")


WORKER = r'''
import subprocess,sys
from pathlib import Path
log=Path(sys.argv[1]); log.parent.mkdir(parents=True,exist_ok=True)
with log.open('xb') as output:
    with subprocess.Popen(sys.argv[2:],stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as process:
        for line in process.stdout:
            output.write(line); output.flush()
            sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
        code=process.wait()
sys.exit(code)
'''


def main(argv=None):
    args = parse_args(argv)
    expected = inputs(args.repository)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    destination = args.artifacts_dir / ("satip-foundation-" + stamp)
    environment = {"BUILDROOT_DIR": args.buildroot_dir, "SATIP_OUTPUT_DIR": args.output_dir,
                   "BR2_DL_DIR": args.download_cache}
    build = [args.source + "/buildroot/build.sh", "--jobs", str(args.jobs)]
    print(json.dumps({"execution": args.execution, "host": args.host, "source": args.source,
                      "jobs": args.jobs, "install_host_packages": args.install_host_packages,
                      **environment, "artifacts": str(destination),
                      "build": build, "staged_files": sorted(expected)}, indent=2))
    if args.dry_run:
        print("Dry run: no host contact, package installation, staging, locks, build or artifact writes.")
        return 0
    identity = source_identity(args.repository, expected)
    environment["SATIP_SOURCE_IDENTITY"] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    prepare_host(args)
    token = uuid.uuid4().hex
    output = PurePosixPath(args.output_dir)
    source = PurePosixPath(args.source)
    locks = [str(source.parent / ("." + source.name + ".satip-source.lock")),
             str(output.parent / ("." + output.name + ".satip-build.lock"))]
    signature = expected["buildroot/external/configs/satip_rpi4_64_defconfig"]
    request = {"action": "acquire", "locks": locks, "token": token,
               "paths": [args.source, args.buildroot_dir, args.output_dir, args.download_cache], "output": args.output_dir,
               "output_owner": args.source + ":" + signature}
    if args.execution == "remote":
        request["paths"].append(args.remote_root + "/logs")
        request.update(source=args.source, inputs=expected, adopt=args.adopt_source,
                       owner=hashlib.sha256(str(args.repository).encode()).hexdigest())
    guard(args, request)
    remote_inflight = False
    try:
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "source-inputs.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
        if args.execution == "remote":
            transport = shlex.join(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", "-p", str(args.ssh_port)])
            with tempfile.TemporaryDirectory(prefix="satip-stage-") as temporary:
                manifest = Path(temporary) / "files"
                manifest.write_text("\n".join(sorted(expected)) + "\n")
                run(["rsync", "-azr", "--files-from=" + str(manifest), "-e", transport,
                     str(args.repository) + "/", args.host + ":" + shlex.quote(args.source + "/")])
            check = "from pathlib import Path; import hashlib,json,sys; r=Path(sys.argv[1]); e=json.loads(sys.argv[2]); assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==h for p,h in e.items()), 'staged input mismatch'"
            ssh(args, shlex.join(["python3", "-c", check, args.source, json.dumps(expected)]), capture=True)
            remote_log = args.remote_root + "/logs/" + stamp + ".log"
            command = "env " + shlex.join([f"{k}={v}" for k, v in environment.items()]) + " "
            command += shlex.join(["python3", "-c", WORKER, remote_log, *build])
            remote_inflight = True
            ssh(args, command, log=destination / "build.log")
            remote_inflight = False
            ssh(args, shlex.join([args.source + "/buildroot/validate-artifacts.sh", args.output_dir + "/images"]))
            for name in (*ARTIFACTS, "SHA256SUMS"):
                run(["rsync", "-azL", "-e", transport,
                     args.host + ":" + shlex.quote(args.output_dir + "/images/" + name), str(destination / name)])
            run(["rsync", "-azL", "-e", transport,
                 args.host + ":" + shlex.quote(args.output_dir + "/.config"), str(destination / "resolved.config")])
        else:
            run(build, env={**os.environ, **environment}, log=destination / "build.log")
            for name in (*ARTIFACTS, "SHA256SUMS"):
                shutil.copyfile(Path(args.output_dir) / "images" / name, destination / name)
            shutil.copyfile(Path(args.output_dir) / ".config", destination / "resolved.config")
        sums = verify(destination, expected, identity)
        if inputs(args.repository) != expected:
            raise BuildError("local build inputs changed during the build")
        if source_identity(args.repository, expected) != identity:
            raise BuildError("originating Git identity changed during the build")
        if args.zip_image:
            package_image(destination, sums["sdcard.img"])
        (destination / "VERIFIED.json").write_text(json.dumps({"schema": 1, "profile": "development-foundation",
            "release": False, "jobs": args.jobs, "execution": args.execution,
            "artifacts": sums}, indent=2) + "\n")
        print(f"Verified foundation image: {destination / 'sdcard.img'}")
        print("No flashing, application playback qualification or SWU generation performed.")
    finally:
        if remote_inflight:
            print("Remote build did not return successfully; locks retained for safety: " + ", ".join(locks), file=sys.stderr)
        else:
            guard(args, {"action": "release", "locks": locks, "token": token})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"build_image.py: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
