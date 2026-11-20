#!/usr/bin/env python3
"""Install the pinned pure-Python resolver into a NEW in-place directory."""
import argparse
import hashlib
import io
from pathlib import Path, PurePosixPath
import urllib.request
import zipfile

VERSION = "2026.10.26"
URL = ("https://files.pythonhosted.org/packages/7f/3d/16ea7d5638cf585991f2c5705e8083688dde633d32d0871f2404e0f4d827/"
       "yt_dlp-2026.10.26-py3-none-any.whl")
SHA256 = "a05c5b5d8562f7e8a62f5334348b406af4930d56d3a65f8021d14af0e22339ae"


def install(destination):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination exists; refusing to replace an installed resolver")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(URL, timeout=30) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Resolver checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = []
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if not member.filename.startswith("yt_dlp/") or member.is_dir():
                continue
            if name.is_absolute() or ".." in name.parts or member.file_size > 8 * 1024 * 1024:
                raise ValueError("Unsafe resolver archive")
            members.append(member)
        if not any(member.filename == "yt_dlp/__main__.py" for member in members):
            raise ValueError("Incomplete resolver archive")
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        for member in members:
            path = destination / member.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(member))
            path.chmod(0o644)
        # mkdir's mode is subject to umask, including intermediate directories.
        for path in destination.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
    print(f"Installed checksum-verified yt-dlp {VERSION} in {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default="/opt/satip-youtube")
    args = parser.parse_args()
    try:
        install(args.destination)
    except (OSError, ValueError, zipfile.BadZipFile):
        parser.exit(1, "Resolver installation failed; check destination, TLS/network and checksum.\n")


if __name__ == "__main__":
    main()
