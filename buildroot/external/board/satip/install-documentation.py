#!/usr/bin/env python3
"""Install public appliance guides and licence notices without private assets."""
from pathlib import Path
import shutil
import sys
import tempfile


def documentation_sources(repo):
    repo = Path(repo)
    paths = [repo / name for name in (
        "README.md", "LICENSE", "buildroot/README.md",
        "validation/README.md", "satip_controller/static/hls-LICENSE.txt",
        "satip_controller/static/qrcode-LICENSE.txt")]
    # Examples are optional during source-tree migrations; deploy/ is retired.
    examples = repo / "examples/README.md"
    if examples.exists() or examples.is_symlink():
        paths.append(examples)
    paths += sorted((repo / "doc").glob("*.md"))
    paths += [repo / "doc/user-manual.html", repo / "doc/user-manual.css"]
    paths += sorted((repo / "doc/images").glob("*.png"))
    return paths


def install(target, repo):
    target, repo = Path(target), Path(repo)
    destination = target / "usr/share/doc/satip"
    contents = {}
    for source in documentation_sources(repo):
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError("Linked documentation source refused")
        contents[source.relative_to(repo)] = source.read_bytes()
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Linked documentation destination refused")
        if path == target:
            break
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Build the exact inventory before replacing the package-owned directory.
    # This removes renamed/deleted guides, images and the retired deploy guide.
    with tempfile.TemporaryDirectory(prefix=".satip-docs-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "satip"
        for relative, content in contents.items():
            installed = staged / relative
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(content)
            installed.chmod(0o644)
            directory = installed.parent
            while directory != Path(temporary):
                directory.chmod(0o755)
                directory = directory.parent
        if destination.exists():
            shutil.rmtree(destination)
        staged.rename(destination)
    # Public guides remain traversable even with a root umask of 077.
    directory = destination.parent
    while directory != target and directory.is_relative_to(target):
        directory.chmod(0o755)
        directory = directory.parent
    return destination


if __name__ == "__main__":
    target = Path(sys.argv[1])
    repo = Path(__file__).resolve().parents[4]
    print(f"SAT-IP documentation installed in {install(target, repo)}")
