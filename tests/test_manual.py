import importlib.util
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from satip_controller.manual import GUIDES, IMAGES, install


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_manual", ROOT / "scripts/render_manual.py")
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []
        self.ids = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs["href"])
        if tag == "link":
            self.links.append(attrs["href"])
        if tag == "img":
            self.images.append(attrs)
        if tag == "script":
            self.scripts += 1


class ManualTests(unittest.TestCase):
    def test_html_is_current_accessible_and_self_contained(self):
        markdown = (ROOT / "doc/user-manual.md").read_text()
        html = (ROOT / "doc/user-manual.html").read_text()
        self.assertEqual(RENDERER.render(markdown), html)
        parsed = Links()
        parsed.feed(html)
        self.assertEqual(parsed.scripts, 0)
        self.assertEqual({Path(image["src"]).name for image in parsed.images}, set(IMAGES))
        self.assertEqual(len(IMAGES), 17)
        self.assertEqual(len(parsed.ids), len(set(parsed.ids)))
        for image in parsed.images:
            self.assertTrue(image["alt"])
            self.assertIn(Path(image["src"]).name, IMAGES)
        for link in parsed.links + [image["src"] for image in parsed.images]:
            with self.subTest(link=link):
                if link == "/":
                    continue
                if link.startswith("https://"):
                    self.assertIn(link, {"https://www.raspberrypi.com/software/", "https://argon40.com/"})
                    continue
                self.assertNotIn("://", link, "Images/styles must be local; external links are optional references")
                filename, _, anchor = link.partition("#")
                if filename:
                    self.assertTrue((ROOT / "doc" / filename).is_file())
                else:
                    self.assertIn(anchor, parsed.ids)

    def test_setup_is_self_service_and_distinguishes_sources(self):
        manual = (ROOT / "doc/user-manual.md").read_text()
        for referral in ("installer", "support person", "ask support", "Mute-labelled", "multimedia Mute"):
            self.assertNotIn(referral, manual)
        steps = ["1. Flash the microSD card", "2. Connect the TV and your home network",
                 "3. Open setup on your phone or computer", "4. Choose your source and load channels",
                 "5. Select your favourites", "6. Arrange favourites and start watching",
                 "7. Set the receiver name, timezone and preferences"]
        positions = [manual.index("### " + step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        for label in ("Use custom", "Scan", "Load TV bouquets", "Import selected bouquets",
                      "Confirm favourites", "Save channels and watch TV", "Save device settings"):
            self.assertIn(label, manual)
        self.assertIn("4A. SAT-IP", manual)
        self.assertIn("4B. Enigma2", manual)
        self.assertIn("No preliminary SAT-IP scan", manual)
        self.assertIn("channel groups", manual)

    def test_features_are_concise_stable_and_procedures_have_one_home(self):
        manual = (ROOT / "doc/user-manual.md").read_text()
        overview = manual.split("## Main features\n", 1)[1].split("## Contents", 1)[0]
        features = [line[2:] for line in overview.splitlines() if line.startswith("- ")]
        self.assertGreaterEqual(len(features), 10)
        self.assertTrue(all(len(feature.split()) <= 18 for feature in features))
        for feature in ("SAT-IP servers and Enigma2", "USB time shifting", "YouTube",
                        "backup/restore", "programme information"):
            self.assertIn(feature, overview)
        self.assertNotIn("experimental", manual.lower())
        self.assertNotIn("About this edition", manual)
        self.assertNotIn("SAT.1, ProSieben or kabel eins is missing", manual)
        requirements = manual.split("### What you need", 1)[1].split("## Quick start", 1)[0]
        self.assertIn("USB stick", requirements)
        self.assertIn("time shifting", requirements)
        self.assertIn("formatting", requirements)
        for detail in ("USB capture starts", "RAM-only", "backend subscription", "mpv"):
            self.assertNotIn(detail, manual)
        self.assertEqual(manual.count("](images/manual-settings-mobile.png)"), 1)
        self.assertNotIn("### Alternative television source: Enigma2", manual)
        self.assertEqual(manual.count("| Pause / resume |"), 1)
        settings = (ROOT / "satip_controller/static/index.html").read_text()
        self.assertNotIn("Experimental", settings)

    def test_ir_wiring_matches_firmware_and_warns_about_pinout(self):
        manual = (ROOT / "doc/user-manual.md").read_text()
        self.assertIn("dtoverlay=gpio-ir,gpio_pin=23", (ROOT / "buildroot/external/board/satip/config.txt").read_text())
        for row in ("| VCC / VS — power | 3.3 V | Pin 1 |",
                    "| GND — ground | Ground | Pin 6 |",
                    "| OUT / S — signal | GPIO23 | Pin 16 |"):
            self.assertIn(row, manual)
        for caution in ("not 5 V tolerant", "physical order varies", "Disconnect all Pi power",
                        "Do not attach a second receiver", "Argon ONE V2", "38 kHz"):
            self.assertIn(caution, manual)
        self.assertIn("infrared", GUIDES)

    def test_renderer_escapes_text_and_rejects_unsafe_links(self):
        self.assertIn("&lt;script&gt;", RENDERER.render("# Test\n\n<script> & text"))
        self.assertIn("<strong>Warning</strong>", RENDERER.inline("**Warning**"))
        self.assertIn('<ol start="5">', RENDERER.render("# Test\n\n5. Import\n6. Wait"))
        self.assertIn('target="_blank" rel="noopener noreferrer"',
                      RENDERER.inline("[Image writer](https://www.raspberrypi.com/software/)"))
        self.assertIn("<code>a_b</code>", RENDERER.inline(chr(96) + "a_b" + chr(96)))
        with self.assertRaises(ValueError):
            RENDERER.inline("[bad](javascript:alert)")
        with self.assertRaises(ValueError):
            RENDERER.render("# Test\n\n    hidden code")
        with self.assertRaises(ValueError):
            RENDERER.render("# Test\n\n| A |\n|---|\n| B | C |")

    def test_routes_serve_installed_inventory_and_only_get(self):
        client = TestClient(self.app(ROOT))
        response = client.get("/user-manual.html", headers={"x-test-login": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.text, (ROOT / "doc/user-manual.html").read_text())
        for name in (*IMAGES, "manual-icon.png"):
            with self.subTest(image=name):
                response = client.get("/images/" + name, headers={"x-test-login": "yes"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.content, (ROOT / "doc/images" / name).read_bytes())
        for name in GUIDES:
            self.assertEqual(client.get("/" + name + ".md", headers={"x-test-login": "yes"}).status_code, 200)
        self.assertEqual(client.post("/user-manual.html").status_code, 405)

    @staticmethod
    def app(root):
        def authenticate(request: Request):
            if request.headers.get("x-test-login") != "yes":
                raise HTTPException(401)
        app = FastAPI(dependencies=[Depends(authenticate)])
        install(app, root)
        return app

    def test_docs_follow_parent_authentication(self):
        client = TestClient(self.app(ROOT))
        for route in ("/user-manual.html", "/user-manual.css", "/images/manual-setup.png", "/operations.md"):
            self.assertEqual(client.get(route).status_code, 401)
        for filename in ("satip_controller/static/setup.html", "satip_controller/static/index.html"):
            html = (ROOT / filename).read_text()
            self.assertIn('href="/user-manual.html" target="_blank" rel="noopener"', html)
        for filename in ("satip_controller/web.py", "satip_appliance/web.py"):
            self.assertIn("install_manual(app)", (ROOT / filename).read_text())

    def test_missing_symlinked_and_unlisted_docs_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "doc").mkdir()
            (root / "doc/user-manual.html").symlink_to(ROOT / "doc/user-manual.html")
            client = TestClient(self.app(root))
            for route in ("/user-manual.html", "/user-manual.css", "/images/private.png",
                          "/images/../../etc/passwd", "/%2e%2e/etc/passwd", "/history/private.md",
                          "/etc/satip-web/login.json"):
                self.assertEqual(client.get(route, headers={"x-test-login": "yes"}).status_code, 404)
