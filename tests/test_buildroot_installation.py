"""Build integration regressions with disposable roots; no image compilation."""
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "buildroot/external/board/satip"
APP = runpy.run_path(str(BOARD / "validate-app-build.py"))
DOCS = runpy.run_path(str(BOARD / "install-documentation.py"))


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo, self.target = self.root / "source", self.root / "target"
        self.repo.mkdir()
        self.target.mkdir()

    def source(self, name, content="fixture"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def application(self):
        for name in ("satip_controller/__init__.py", "satip_appliance/__init__.py",
                     "satip_controller/old.py", "satip_controller/static/old.js"):
            self.source(name, "# fixture\n")

    def test_app_reinstallation_removes_deleted_sources_assets_and_bytecode(self):
        self.application()
        APP["install_sources"](self.repo, self.target)
        package = self.target / "opt/satip/satip_controller"
        cache = package / "__pycache__/old.cpython-314.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"old cache")
        (self.repo / "satip_controller/old.py").unlink()
        (self.repo / "satip_controller/static/old.js").unlink()
        self.source("satip_controller/new.py", "# replacement\n")
        self.source("satip_controller/static/new.css", "/* replacement */\n")
        sibling = self.target / "opt/unrelated"
        sibling.write_text("preserve")
        APP["install_sources"](self.repo, self.target)
        APP["validate_sources"](self.target, self.repo)
        self.assertFalse((package / "old.py").exists())
        self.assertFalse((package / "static/old.js").exists())
        self.assertFalse(cache.exists())
        self.assertEqual(sibling.read_text(), "preserve")

    def test_app_validator_rejects_extra_modules_assets_and_orphan_bytecode(self):
        self.application()
        APP["install_sources"](self.repo, self.target)
        for name in ("old_removed.py", "static/removed.js", "__pycache__/removed.cpython-314.pyc"):
            with self.subTest(name=name):
                path = self.target / "opt/satip/satip_controller" / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"obsolete")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    APP["validate_sources"](self.target, self.repo)
                path.unlink()
        source = self.target / "opt/satip/satip_controller/old.py"
        source.write_text("changed")
        with self.assertRaisesRegex(ValueError, "differs"):
            APP["validate_sources"](self.target, self.repo)

    def test_app_linked_destination_and_invalid_source_preserve_existing_files(self):
        self.application()
        APP["install_sources"](self.repo, self.target)
        original = self.target / "opt/satip/satip_controller/old.py"
        (self.repo / "satip_controller/old.py").unlink()
        (self.repo / "satip_controller/old.py").symlink_to(original)
        with self.assertRaisesRegex(ValueError, "Linked"):
            APP["install_sources"](self.repo, self.target)
        self.assertEqual(original.read_text(), "# fixture\n")
        (self.repo / "satip_controller/old.py").unlink()
        self.source("satip_controller/old.py")
        shutil.rmtree(self.target / "opt/satip/satip_appliance")
        (self.target / "opt/satip/satip_appliance").symlink_to(self.repo / "satip_appliance")
        with self.assertRaisesRegex(ValueError, "Linked"):
            APP["install_sources"](self.repo, self.target)
        self.assertEqual(original.read_text(), "# fixture\n")

    def documentation(self):
        for name in ("README.md", "LICENSE", "buildroot/README.md", "validation/README.md",
                     "satip_controller/static/hls-LICENSE.txt",
                     "satip_controller/static/qrcode-LICENSE.txt", "doc/old.md", "doc/images/old.png",
                     "doc/user-manual.html", "doc/user-manual.css"):
            self.source(name)

    def test_documentation_exact_inventory_handles_retired_deploy_and_examples(self):
        self.documentation()
        destination = DOCS["install"](self.target, self.repo)
        retired = destination / "deploy/README.md"
        retired.parent.mkdir()
        retired.write_text("obsolete")
        (self.repo / "doc/old.md").unlink()
        (self.repo / "doc/images/old.png").unlink()
        self.source("doc/current.md")
        self.source("examples/README.md")
        sibling = destination.parent / "another-package.txt"
        sibling.write_text("preserve")
        DOCS["install"](self.target, self.repo)
        expected = {p.relative_to(self.repo) for p in DOCS["documentation_sources"](self.repo)}
        self.assertEqual({p.relative_to(destination) for p in destination.rglob("*") if p.is_file()}, expected)
        self.assertFalse(retired.exists())
        self.assertEqual(sibling.read_text(), "preserve")
        self.assertTrue((destination / "examples/README.md").exists())
        (self.repo / "examples/README.md").unlink()
        DOCS["install"](self.target, self.repo)
        self.assertFalse((destination / "examples").exists())

    def test_documentation_failed_preflight_and_symlink_do_not_replace_existing(self):
        self.documentation()
        destination = DOCS["install"](self.target, self.repo)
        before = (destination / "README.md").read_bytes()
        (self.repo / "LICENSE").unlink()
        with self.assertRaises(FileNotFoundError):
            DOCS["install"](self.target, self.repo)
        self.assertEqual((destination / "README.md").read_bytes(), before)
        self.source("LICENSE")
        shutil.rmtree(destination)
        destination.symlink_to(self.repo)
        with self.assertRaisesRegex(ValueError, "destination"):
            DOCS["install"](self.target, self.repo)
        self.assertTrue((self.repo / "README.md").is_file())


class BuildHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": str(self.bin) + ":" + os.environ["PATH"]}

    def executable(self, path, code):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!" + sys.executable + "\n" + code)
        path.chmod(0o755)

    def test_failed_initial_configuration_retries_but_existing_config_is_preserved(self):
        source = self.root / "source/buildroot"
        source.mkdir(parents=True)
        shutil.copy(ROOT / "buildroot/build.sh", source)
        shutil.copy(ROOT / "buildroot/validate-config.py", source)
        shutil.copy(ROOT / "buildroot/validate-security.py", source)
        validator = source / "external/board/satip/validate-app-build.py"
        validator.parent.mkdir(parents=True)
        shutil.copy(BOARD / "validate-app-build.py", validator)
        check = source / "validate-checkout.sh"
        check.write_text("#!/bin/sh\nexit 0\n")
        check.chmod(0o755)
        config = source / "external/configs/satip_rpi4_64_defconfig"
        config.parent.mkdir(parents=True)
        config.write_text("BR2_aarch64=y\nBR2_TOOLCHAIN_USES_GLIBC=y\n")
        checkout, output = self.root / "checkout", self.root / "output"
        checkout.mkdir()
        fail = self.root / "fail-once"
        fail.touch()
        self.executable(self.bin / "make", r"""import os,sys
from pathlib import Path
import runpy
root=Path(os.environ['FIXTURE_ROOT'])
with (root/'calls').open('a') as stream: stream.write('make\n')
if 'printvars' in sys.argv:
    external = Path(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('BR2_EXTERNAL=')))
    versions = runpy.run_path(str(external/'board/satip/validate-app-build.py'))['SECURITY_VERSIONS']
    names = next(arg.removeprefix('VARS=') for arg in sys.argv if arg.startswith('VARS=')).split()
    for key in names:
        name, _, field = key.removeprefix('HOST_').removeprefix('PYTHON_').partition('_')
        name = name.lower(); version = versions[name]
        value = (str(external/f'patches/python-{name}/{version}/python-{name}.hash') if field == 'HASH_FILES' else
                 f'{name}-{version}.tar.gz' if field == 'SOURCE' else version)
        print(key + '=' + value)
    sys.exit(0)
if (root/'fail-once').exists():
    (root/'fail-once').unlink(); sys.exit(1)
output=Path(next(arg[2:] for arg in sys.argv if arg.startswith('O=')))
(output/'.config').write_text('BR2_aarch64=y\nBR2_TOOLCHAIN_USES_GLIBC=y\n')
""")
        env = {**self.env, "FIXTURE_ROOT": str(self.root), "BUILDROOT_DIR": str(checkout),
               "SATIP_OUTPUT_DIR": str(output), "BR2_DL_DIR": str(self.root / "dl")}
        def run():
            return subprocess.run(["sh", str(source / "build.sh"), "--configure-only", "--jobs", "1"],
                                  env=env, capture_output=True, text=True)
        self.assertNotEqual(run().returncode, 0)
        self.assertTrue((output / ".satip-foundation-output").is_file())
        self.assertFalse((output / ".config").exists())
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text(), "make\nmake\nmake\n")
        (output / ".config").write_text("interactive config\n")
        self.assertNotEqual(run().returncode, 0)
        self.assertEqual((output / ".config").read_text(), "interactive config\n")
        self.assertEqual((self.root / "calls").read_text(), "make\nmake\nmake\n")

    def test_post_image_refreshes_stale_boot_configuration_on_every_run(self):
        board, images = self.root / "board", self.root / "images"
        board.mkdir(); images.mkdir()
        shutil.copy(BOARD / "post-image.sh", board)
        for name in ("config.txt", "cmdline.txt", "satip-config.txt"):
            (board / name).write_text("current " + name)
        for name in ("Image", "bcm2711-rpi-4-b.dtb", "rootfs.ext4", "rpi-firmware/start4.elf",
                     "rpi-firmware/fixup4.dat", "rpi-firmware/config.txt", "rpi-firmware/cmdline.txt",
                     "rpi-firmware/overlays/disable-bt.dtbo", "rpi-firmware/overlays/vc4-kms-v3d.dtbo",
                     "rpi-firmware/overlays/gpio-ir.dtbo"):
            path = images / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale fixture")
        (self.root / "satip-build.json").write_text("{}")
        self.executable(self.bin / "genimage", """from pathlib import Path
import sys
images=Path(sys.argv[sys.argv.index('--outputpath')+1])
(images/'sdcard.img').write_bytes((images/'rpi-firmware/config.txt').read_bytes() +
                                (images/'rpi-firmware/cmdline.txt').read_bytes())
""")
        self.executable(self.bin / "sha256sum", """import hashlib,sys
from pathlib import Path
for name in sys.argv[1:]: print(hashlib.sha256(Path(name).read_bytes()).hexdigest()+'  '+name)
""")
        env = {**self.env, "HOST_DIR": str(self.root), "BASE_DIR": str(self.root),
               "BUILD_DIR": str(self.root)}
        for generation in ("first", "second"):
            (board / "config.txt").write_text(generation)
            result = subprocess.run(["sh", str(board / "post-image.sh"), str(images)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((images / "sdcard.img").read_text(), generation + "current cmdline.txt")
            self.assertTrue((images / "SHA256SUMS").is_file())


if __name__ == "__main__":
    unittest.main()
