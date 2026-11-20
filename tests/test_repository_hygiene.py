"""Check ignore policy without creating real secrets or generated files."""
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def ignored(self, paths):
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--no-index", "-z", "--stdin"],
            input="\0".join(paths) + "\0", capture_output=True, text=True, check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return set(result.stdout.rstrip("\0").split("\0")) - {""}

    def test_private_and_generated_paths_are_ignored(self):
        paths = [
            ".baseline-private/originals.tar", ".venv/bin/python",
            "validation/records/documentation-review/notes.md",
            "deploy/provision-production.py",
            "satip_controller/__pycache__/player.pyc", ".mypy_cache/cache.json",
            ".ruff_cache/cache", "artifacts/sdcard.img", "dist/package.whl",
            "satip_controller.egg-info/PKG-INFO", "buildroot/output/rootfs.ext4",
            "buildroot/dl/source.tar.gz", "scripts/build-image.ini", "doc/.DS_Store",
            ".env", ".env.local", "private/server.key", "captures/stream.ts",
            "browser-profiles/Default/Cookies", "screenshots/settings.png",
            "credentials.json", "preview-credentials.json", "login.json",
            "youtube-identity.json", "wpa_supplicant.conf", "id_ed25519",
            "backups/before.tar", "player.log", "config.json.bak",
            "identity/factory.json", "identity/device.json", "identity/development.txt",
            "data/store/identity/factory.json", "store/config/web/login.json",
            "export/tvheadend/passwd/user", "export/identity/factory.json",
            "shadow", "etc/shadow", "etc/shadow-", "etc/gshadow", "etc/gshadow-",
            "etc/satip-dev-provisioned", "satip-config.txt",
            "id_ecdsa", "id_dsa", "id_ecdsa_sk", "id_ed25519_sk",
            "etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_ed25519_key.pub",
            "etc/dropbear/dropbear_ed25519_host_key", "known_hosts",
            "cookies.txt", "cookies.json", "youtube-cookies.txt", "browser_cookies.json",
            "Cookies", "Cookies-journal", "cookies.sqlite", "cookies.sqlite-wal",
            "browser-profile/Default/Cookies", "backup.zip", "snapshot.tar.zst",
            "snapshot.7z", "sdcard.img.xz", "sdcard.img.zst", "capture.pcap", "capture.pcapng",
        ]
        self.assertEqual(self.ignored(paths), set(paths))

    def test_source_examples_fixtures_and_notices_remain_visible(self):
        paths = [
            "README.md", ".github/workflows/checks.yml", "pyproject.toml",
            ".env.example", "tests/fixtures/config.json", "tests/fixtures/library.json",
            "examples/README.md", "examples/config.json", "examples/library.json",
            "examples/satip-config.txt", "scripts/build-image.ini.example",
            "satip_controller/player.py", "satip_appliance/provision.py",
            "satip_controller/static/qrcode.js", "satip_controller/static/qrcode-LICENSE.txt",
            "satip_controller/static/hls.min.js", "satip_controller/static/hls-LICENSE.txt",
            "doc/images/startbild.png", "tests/test_core.py", "tests/test_ui.cjs",
            "validation/check_device_settings.py",
            "buildroot/external/patches/ffmpeg/0001-rpi-v4l2-m2m.patch",
            "buildroot/external/board/satip/satip-config.txt",
        ]
        self.assertEqual(self.ignored(paths), set())


if __name__ == "__main__":
    unittest.main()
