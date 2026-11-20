"""Tvheadend's native hourly snapshots, on the persistent data filesystem."""
import json
import os
from pathlib import Path
import stat

from satip_controller.state import atomic_json


class EPGPersistence:
    def __init__(self, directory=Path("/data/store/tvheadend")):
        self.directory = directory
        self.synced = None

    def configure(self, uid, gid):
        """Call only before spawning a backend generation, never on live config."""
        directory = self.directory / "epggrab"
        if directory.is_symlink():
            raise ValueError("Linked EPG configuration directory refused")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, uid, gid)
        path = directory / "config"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            # Creating epggrab/config suppresses upstream's default module
            # activation, so retain its default passive EIT/PSIP grabbers.
            config = {"modules": {"eit": {"enabled": True},
                                  "psip": {"enabled": True}},
                      "cron": "4 */12 * * *"}
        else:
            with os.fdopen(fd) as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Ordinary EPG configuration file required")
                config = json.load(source)
            if not isinstance(config, dict):
                raise ValueError("Invalid EPG configuration")
        if type(config.get("epgdb_periodicsave")) is int and config["epgdb_periodicsave"] == 1:
            return
        config["epgdb_periodicsave"] = 1
        atomic_json(path, config)
        os.chown(path, uid, gid, follow_symlinks=False)

    def flush(self):
        """Flush completed atomic replacements, not Tvheadend's .tmp writer."""
        path = self.directory / "epgdb.v3"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Ordinary EPG database file required")
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if identity == self.synced:
                return False
            os.fsync(source.fileno())
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.synced = identity
        print("EPG database flushed to persistent storage", flush=True)
        return True
