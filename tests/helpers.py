import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from satip_controller.config import Config
from satip_controller.state import atomic_json, State


class Fixture:
    def setup_files(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = json.loads((Path(__file__).parent / "fixtures/library.json").read_text())
        config = json.loads((Path(__file__).parent / "fixtures/config.json").read_text())
        for field in ("library", "credentials", "state_dir", "management_dir", "runtime_dir"):
            config[field] = str(self.root / field)
        atomic_json(self.root / "config.json", config)
        atomic_json(Path(config["library"]), self.data)
        atomic_json(Path(config["credentials"]), {"username": "playback", "password": "sensitive-test-password"})
        self.config = Config.load(self.root / "config.json")
        self.state = State(self.config.state_dir / "state.json", self.data["favourites"])
        self.player = SimpleNamespace(ipc=SimpleNamespace(command=AsyncMock(), get=AsyncMock(), closed=False),
            start=AsyncMock(), show_window=AsyncMock(), close=AsyncMock(), load=AsyncMock(),
            select_audio=AsyncMock(return_value=True), stop_stream=AsyncMock())
        self.web_config = {"version": 1, "private_dir": str(self.root/"web"), "management_dir": str(self.config.management_dir),
            "library": str(self.config.library), "player_state_dir": str(self.config.state_dir),
            "network_uuid": "a"*32, "playback_acl_uuid": "b"*32, "mux_timeout": .05,
            "web_credentials": str(self.root/"login.json"), "admin_credentials": str(self.root/"admin.json")}
        atomic_json(self.root/"login.json", {"username": "web", "password": "private-web-password"})
        atomic_json(self.root/"admin.json", {"username": "admin", "password": "private-admin-password"})
