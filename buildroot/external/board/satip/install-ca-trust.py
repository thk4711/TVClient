#!/usr/bin/env python3
"""Distrust the compromised emSign G3 root in the image's system CA store.

Only the reviewed DER fingerprint is removed. This also protects stdlib/OpenSSL
clients that do not use certifi. No target executables or host CA stores are used.
"""
import base64
import hashlib
import os
from pathlib import Path
import re
import sys


REVOKED = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
CERTIFICATE = re.compile(rb"-----BEGIN CERTIFICATE-----\s+(.*?)\s+-----END CERTIFICATE-----(?:\r?\n)?", re.S)


def revoked(match):
    der = base64.b64decode(re.sub(rb"\s+", b"", match[1]), validate=True)
    return hashlib.sha256(der).hexdigest() == REVOKED


def checked(path, target):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Linked CA store destination refused")
        if parent == target:
            return path
    raise ValueError("CA store destination outside target")


def install(target):
    target = Path(target).absolute()
    bundle = checked(target / "etc/ssl/certs/ca-certificates.crt", target)
    data = bundle.read_bytes()
    if not CERTIFICATE.search(data):
        raise ValueError("Missing system CA certificates")
    filtered = CERTIFICATE.sub(lambda match: b"" if revoked(match) else match[0], data)
    if filtered != data:
        bundle.write_bytes(filtered)

    # Remove the source certificate and its OpenSSL directory/hash aliases too;
    # otherwise a client using capath could still trust it or reintroduce it.
    sources = checked(target / "usr/share/ca-certificates", target)
    removed = set()
    for path in sorted(sources.rglob("*.crt")):
        checked(path, target)
        if any(revoked(match) for match in CERTIFICATE.finditer(path.read_bytes())):
            path.unlink()
            removed.add(path)
    certs = bundle.parent
    links = [path for path in certs.iterdir() if path.is_symlink()]
    while links:
        pending = []
        for path in links:
            link = os.readlink(path)
            destination = Path(os.path.normpath(str(target / link.lstrip("/"))
                               if link.startswith("/") else str(path.parent / link)))
            if destination in removed:
                path.unlink()
                removed.add(path)
            else:
                pending.append(path)
        if len(pending) == len(links):
            break
        links = pending
    if any(revoked(match) for match in CERTIFICATE.finditer(bundle.read_bytes())):
        raise ValueError("Compromised system CA root remains")


if __name__ == "__main__":
    install(Path(sys.argv[1]))
    print("System CA store excludes the compromised emSign G3 root")
