import hashlib
import json
import os
import re
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "buildroot/external/board/satip"
DOCUMENTATION = runpy.run_path(str(BOARD / "install-documentation.py"))
BUILD = runpy.run_path(str(ROOT / "scripts/build_image.py"))


class DocumentationTests(unittest.TestCase):
    def test_current_guides_have_explicit_audiences_and_no_dated_reports(self):
        users = {
            "user-manual", "installation", "buildroot-first-boot-setup",
            "usage", "device-settings", "infrared", "timeshift", "enigma2", "youtube",
        }
        index = (ROOT / "doc/README.md").read_text()
        user_index, developer_index = index.split("## Developing and administering", 1)
        for path in sorted((ROOT / "doc").glob("*.md")):
            if path.stem == "README":
                continue
            with self.subTest(path=path.name):
                text = path.read_text()
                audience = ("users" if path.stem in users
                            else "developers and advanced administrators")
                self.assertIn(f"**Audience: {audience}.**", text)
                self.assertIn(path.name, user_index if path.stem in users else developer_index)
                self.assertNotRegex(text, r"(?m)^#{1,6} .*20[0-9]{2}[- ]")
        for name in ("timeshift", "youtube", "enigma2", "installation"):
            text = (ROOT / f"doc/{name}.md").read_text()
            for internal in ("/run/satip", "/data/store", "SCM_RIGHTS", "compileall"):
                self.assertNotIn(internal, text)

    def test_image_release_documents_the_four_asset_layout(self):
        release = (ROOT / "doc/releasing.md").read_text()
        self.assertIn("exactly four assets", release)
        for asset in ("SHA256SUMS", "RELEASE_NOTES.md",
                      "satip-sources-and-licences-v0.1.1.tar.gz"):
            self.assertIn(asset, release)
        self.assertIn("copyleft corresponding sources", release)
        self.assertNotIn("RELEASE_SHA256SUMS", release)
        self.assertNotIn("VERIFY.md", release)
        manual = (ROOT / "doc/user-manual.md").read_text()
        self.assertIn("**SHA256SUMS**", manual)
        self.assertIn("both the ZIP and extracted", manual)

    def test_current_documentation_inventory_matches_build_and_routes(self):
        from satip_controller.manual import GUIDES
        from scripts.source_manifest import BUILD_GUIDES
        current = {path.stem for path in (ROOT / "doc").glob("*.md")}
        self.assertEqual(set(GUIDES), current)
        self.assertEqual(set(BUILD_GUIDES), current)
        sources = DOCUMENTATION["documentation_sources"](ROOT)
        self.assertFalse(any("records" in source.relative_to(ROOT).parts for source in sources))
        self.assertFalse(any("records" in Path(name).parts for name in BUILD["inputs"](ROOT)))
        for retired in ("channel-switch-optimization-plan", "channel-switch-experiments",
                        "relay-lifecycle-experiment", "same-transponder-session-preservation",
                        "project-assessment-2026-11-14"):
            self.assertNotIn(retired, GUIDES)
            self.assertFalse((ROOT / f"doc/{retired}.md").exists())

    def test_capability_and_retention_reference_does_not_regress(self):
        installation = (ROOT / "doc/installation.md").read_text()
        self.assertIn("Enigma2", installation)
        self.assertIn("stereo or surround", installation)
        api = (ROOT / "doc/api.md").read_text()
        self.assertIn("retained-history time limit", api)
        self.assertNotIn("elapsed-time limit", api)
        self.assertIn("| 415 |", api)
        validation = (ROOT / "doc/validation.md").read_text()
        self.assertIn("evicts old content without ending the session", validation)
        self.assertIn("backup/restore are available", validation)
        self.assertNotIn("discards history at limits", validation)

    def test_readme_is_an_appliance_overview(self):
        readme = (ROOT / "README.md").read_text()
        for implementation in ("Debian", "Security", "DRM/KMS", "EDID",
                               "deinterlacing", "watchdog", "unittest"):
            self.assertNotIn(implementation, readme)
        for section in ("## Features", "## Getting started", "## Licence"):
            self.assertIn(section, readme)
        development = (ROOT / "doc/development.md").read_text()
        for detail in ("DRM/KMS", "EDID", "deinterlacing", "watchdog"):
            self.assertIn(detail, development)

    def test_public_guides_do_not_reintroduce_german_prose(self):
        # Catch the former headings and UI quotations, not proper names such
        # as German broadcasters or the historical HDMI guide's filename.
        german = re.compile(
            r"[äöüÄÖÜß]|\b(?:Sender|Sendung|Einstellungen|Wiedergabe|"
            r"Programminformationen|Ablauf|Abnahme)\b")
        for source in [ROOT / "README.md", *sorted((ROOT / "doc").glob("*.md"))]:
            with self.subTest(path=source.relative_to(ROOT)):
                self.assertIsNone(german.search(source.read_text()))

    def test_public_guide_links_and_heading_anchors_resolve(self):
        sources = [*sorted(ROOT.glob("*.md")), *sorted((ROOT / "doc").glob("*.md")),
                   ROOT / "buildroot/README.md", ROOT / "validation/README.md",
                   *sorted((ROOT / "examples").glob("*.md"))]
        for source in sources:
            for link in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", source.read_text()):
                if ":" in link:
                    continue
                name, _, anchor = link.partition("#")
                target = source.parent / name if name else source
                with self.subTest(source=source.relative_to(ROOT), link=link):
                    self.assertTrue(target.exists(), f"Missing link target: {link}")
                    if anchor and target.suffix == ".md" and target.exists():
                        headings = re.findall(r"^#+ (.+)$", target.read_text(), re.M)
                        slugs = [re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                                 for heading in headings]
                        self.assertIn(anchor, slugs)

    def test_settings_guide_matches_ui_and_separates_internal_details(self):
        settings = (ROOT / "doc/device-settings.md").read_text()
        ui = (ROOT / "satip_controller/static/index.html").read_text().replace("&amp;", "&")
        for label in ("Save device settings", "Try Wi-Fi settings", "Keep settings",
                      "Change web password", "Change root password", "Enable SSH",
                      "Choose device", "Use existing ext4 filesystem", "Format & use device",
                      "Storage limit (GiB)", "Safely remove USB", "Forget USB configuration", "Bottom bar timeout (seconds)",
                      "Link with TV code", "Restart TV playback", "Shut down"):
            with self.subTest(label=label):
                self.assertIn(label, settings)
                self.assertIn(label, ui)
        for internal in ("/data/store", "control.sock", "deploy_device_settings.py"):
            self.assertNotIn(internal, settings)
        self.assertIn("90 seconds", settings)
        self.assertIn("Formatting permanently erases the entire selected disk", settings)
        self.assertIn("no typed phrase or password re-entry", settings)
        self.assertIn("device-settings.md#time-shift-and-usb-storage",
                      (ROOT / "doc/timeshift.md").read_text())
        self.assertNotIn("## Allowlisted preferences",
                         (ROOT / "doc/buildroot-first-boot-setup.md").read_text())
        self.assertIn("## Allowlisted preferences", (ROOT / "doc/provisioning.md").read_text())

    def test_mit_metadata_and_notices(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license"], "MIT AND Apache-2.0")
        self.assertTrue({"LICENSE", "satip_controller/static/hls-LICENSE.txt",
                         "satip_controller/static/qrcode-LICENSE.txt"}
                        <= set(project["license-files"]))
        licence = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", licence)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', licence)
        package = (ROOT / "buildroot/external/package/satip-app/satip-app.mk").read_text()
        self.assertIn("SATIP_APP_LICENSE = MIT", package)
        self.assertIn("SATIP_APP_LICENSE_FILES = LICENSE", package)
        self.assertIn("--include=/LICENSE", package)

    def test_public_documentation_is_installed_and_hashed(self):
        inputs = BUILD["inputs"](ROOT)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve()
            destination = DOCUMENTATION["install"](target, ROOT)
            for source in DOCUMENTATION["documentation_sources"](ROOT):
                relative = source.relative_to(ROOT)
                installed = destination / relative
                self.assertEqual(installed.read_bytes(), source.read_bytes())
                self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
                self.assertEqual(inputs[relative.as_posix()],
                                 hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertFalse((destination / "doc/history").exists())
            self.assertFalse((destination / "deploy").exists())
        self.assertIn('python3 "$board/install-documentation.py" "$target"',
                      (BOARD / "post-build.sh").read_text())

    def test_documentation_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            (repo / "README.md").symlink_to(ROOT / "README.md")
            with self.assertRaises(ValueError):
                DOCUMENTATION["install"](root / "target", repo)

    def test_public_directories_with_private_umask_and_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve()
            destination = target / "usr/share/doc/satip"
            previous = os.umask(0o077)
            try:
                (destination / "doc/images").mkdir(parents=True)
                DOCUMENTATION["install"](target, ROOT)
            finally:
                os.umask(previous)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            for path in target.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777,
                                 0o755 if path.is_dir() else 0o644, str(path))

    def test_wrapper_and_image_metadata_use_identical_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            (output / ".config").write_text("BR2_aarch64=y\n")
            subprocess.run([sys.executable, str(ROOT / "buildroot/build-metadata.py"),
                            str(ROOT), str(ROOT), str(output)], check=True)
            metadata = json.loads((output / "satip-build.json").read_text())
            self.assertEqual(metadata["build_inputs"], BUILD["inputs"](ROOT))


if __name__ == "__main__":
    unittest.main()
