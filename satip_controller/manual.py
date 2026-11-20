"""Read-only, allowlisted public documentation for setup and management."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse


IMAGES = tuple("manual-" + name + ".png" for name in (
    "welcome", "setup", "tuner", "selection", "order", "status", "live",
    "channels", "search", "settings-mobile", "sources", "flash", "connections",
    "wifi", "enigma", "bouquets", "ir-wiring"))
GUIDES = """
    README api architecture backup-validation boot-process buildroot-first-boot-setup
    buildroot-services-persistence configuration development device-settings enigma2
    channel-switching enigma2-internals timeshift-internals youtube-internals
    hdmi-startup-screen installation operations
    provisioning recovery reference-setup releasing image-release-plan security timeshift troubleshooting
    tvheadend usage validation youtube
    first-boot-validation timeshift-validation user-manual infrared
""".split()


def install(app, root=Path("/usr/share/doc/satip")):
    """Keep the application's authentication/host policy; expose no private files.

    Routes are registered even without installed appliance docs, so development
    applications return a normal 404 rather than failing startup.
    """
    root = Path(root)
    assets = {
        "/user-manual.html": ("doc/user-manual.html", "text/html"),
        "/user-manual.css": ("doc/user-manual.css", "text/css"),
        "/buildroot/README.md": ("buildroot/README.md", "text/plain"),
        "/validation/README.md": ("validation/README.md", "text/plain"),
        "/LICENSE": ("LICENSE", "text/plain"),
        **{"/" + name + ".md": ("doc/" + name + ".md", "text/plain")
           for name in GUIDES},
        **{"/images/" + name: ("doc/images/" + name, "image/png")
           for name in (*IMAGES, "manual-icon.png", "startbild.png")},
    }

    def handler(relative, media_type):
        def document():
            path = root / relative
            if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))):
                raise HTTPException(404, "Documentation not installed")
            return FileResponse(path, media_type=media_type)
        return document

    for route, (relative, media_type) in assets.items():
        app.get(route)(handler(relative, media_type))
