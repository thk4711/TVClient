"""Read-only, bounded Linux and Tvheadend telemetry. No tuning or root commands."""
import asyncio
import fcntl
import json
import math
from pathlib import Path
import platform
import re
import shutil
import socket
import struct
import subprocess
import time

from .backend import BackendError


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def signal(value, scale, unit):
    value = number(value)
    if value is None or scale not in (1, 2):
        return {"value": None, "unit": None}
    if scale == 1:
        return {"value": round(max(0, min(65535, value)) * 100 / 65535, 1), "unit": "%"}
    return {"value": round(value / 1000, 1), "unit": unit}


def text(path):
    try:
        return Path(path).read_text().strip().strip("\x00")
    except (OSError, UnicodeError):
        return None


def numeric(path):
    try:
        return float(text(path))
    except (ValueError, TypeError):
        return None


def command(*args):
    executable = shutil.which(args[0])
    if not executable:
        return ""
    try:
        result = subprocess.run([executable, *args[1:]], capture_output=True,
                                text=True, timeout=2, check=False, env={"LC_ALL": "C"})
        return result.stdout[:16384] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ""


def wifi(interface):
    output = command("iw", "dev", interface, "link")
    def match(pattern, cast=str):
        found = re.search(pattern, output, re.MULTILINE)
        return cast(found[1]) if found else None
    return {"ssid": match(r"^\s*SSID: (.*)$"),
            "signal_dbm": match(r"signal: (-?[\d.]+) dBm", float),
            "frequency_mhz": match(r"freq: (\d+)", int),
            "tx_rate_mbps": match(r"tx bitrate: ([\d.]+)", float),
            "rx_rate_mbps": match(r"rx bitrate: ([\d.]+)", float),
            "state": "connected" if "Connected to " in output else
                     "disconnected" if "Not connected" in output else "unavailable"}


class Telemetry:
    def __init__(self, backend, storage):
        self.backend, self.storage = backend, Path(storage)
        self.cache = {}
        self.locks = {name: asyncio.Lock() for name in ("system", "network", "reception", "performance")}
        from .performance import Performance
        self.performance = Performance()
        self.cpu = None
        self.traffic = {}
        self.errors = {}

    async def sample(self, name, collect, threaded=False):
        async with self.locks[name]:
            previous = self.cache.get(name)
            if previous and time.monotonic() - previous[0] < 3:
                return previous[1]
            try:
                data = await asyncio.to_thread(collect) if threaded else await collect()
                result = {"state": "ok", "sampled_at": time.time(), **data}
            except (OSError, ValueError, TypeError, KeyError, BackendError, TimeoutError):
                result = {**(previous[1] if previous else {}),
                          "state": "stale" if previous and previous[1].get("sampled_at") else "unavailable"}
            self.cache[name] = (time.monotonic(), result)
            return result

    async def snapshot(self):
        sections = await asyncio.gather(self.sample("system", self.system, True),
                                        self.sample("network", self.network, True),
                                        self.sample("reception", self.reception),
                                        self.sample("performance", self.performance.collect, True))
        return {"version": 1, **dict(zip(("system", "network", "reception", "performance"), sections))}

    def system(self):
        cpu_percent = None
        stats = text("/proc/stat")
        if stats:
            values = [int(v) for v in stats.splitlines()[0].split()[1:9]]
            total, idle = sum(values), sum(values[3:5])
            if self.cpu and total > self.cpu[0]:
                cpu_percent = round(100 * (1 - (idle-self.cpu[1])/(total-self.cpu[0])), 1)
            self.cpu = total, idle
        memory = {}
        for line in (text("/proc/meminfo") or "").splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) * 1024
        temperatures = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            kind, value = text(zone / "type"), numeric(zone / "temp")
            if kind and ("cpu" in kind.lower() or "bcm" in kind.lower()) and value is not None:
                temperatures.append(value / 1000)
        uptime = text("/proc/uptime")
        disk = shutil.disk_usage(self.storage)
        build = {}
        try:
            metadata = json.loads(text("/etc/satip-build.json") or "{}")
            # Publish only explicit identity fields, never configuration paths.
            build = {key: metadata[key] for key in ("schema", "profile", "release", "build_inputs_sha256") if key in metadata}
        except (ValueError, TypeError):
            pass
        power = command("vcgencmd", "get_throttled")
        found = re.fullmatch(r"throttled=0x([0-9a-fA-F]+)\s*", power)
        flags = int(found[1], 16) if found else None
        return {"hostname": socket.gethostname(), "model": text("/proc/device-tree/model") or platform.machine(),
                "os": platform.system() + " " + platform.release(), "build": build,
                "uptime_seconds": float(uptime.split()[0]) if uptime else None,
                "cpu_percent": cpu_percent, "temperature_c": max(temperatures) if temperatures else None,
                "memory_total": memory.get("MemTotal"), "memory_available": memory.get("MemAvailable"),
                "storage_total": disk.total, "storage_free": disk.free,
                "undervoltage": bool(flags & 1) if flags is not None else None,
                "throttled": bool(flags & 4) if flags is not None else None,
                "past_undervoltage": bool(flags & 0x10000) if flags is not None else None}

    def network(self):
        defaults = set()
        for line in (text("/proc/net/route") or "").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 1:
                defaults.add(fields[0])
        ipv6 = {}
        for line in (text("/proc/net/if_inet6") or "").splitlines():
            fields = line.split()
            if len(fields) == 6:
                ipv6.setdefault(fields[5], []).append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(fields[0])))
        now, interfaces = time.monotonic(), []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            for _, name in socket.if_nameindex():
                if name == "lo" or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", name):
                    continue
                path = Path("/sys/class/net") / name
                addresses = list(ipv6.get(name, []))
                try:
                    result = fcntl.ioctl(connection.fileno(), 0x8915, struct.pack("256s", name.encode()))
                    addresses.insert(0, socket.inet_ntoa(result[20:24]))
                except OSError:
                    pass
                counters = {key: numeric(path / "statistics" / key) for key in
                            ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")}
                rates = {}
                previous = self.traffic.get(name)
                for key in ("rx_bytes", "tx_bytes"):
                    value, old = counters[key], previous[1].get(key) if previous else None
                    rates[key.replace("bytes", "bps")] = round((value-old)*8/(now-previous[0])) if (
                        previous and value is not None and old is not None and value >= old and now > previous[0]) else None
                self.traffic[name] = now, counters
                wireless = wifi(name) if (path / "wireless").exists() else None
                speed = number(numeric(path / "speed"))
                if speed is None or speed <= 0:
                    speed = None
                    # Wi-Fi drivers usually do not expose the Ethernet sysfs speed.
                    # Use the current TX PHY rate, or RX when only that is reported.
                    if wireless and wireless["state"] == "connected":
                        for key in ("tx_rate_mbps", "rx_rate_mbps"):
                            rate = number(wireless.get(key))
                            if rate is not None and rate > 0:
                                speed = rate
                                break
                interfaces.append({"name": name, "addresses": addresses, "default_route": name in defaults,
                                   "link": text(path / "operstate") or "unavailable",
                                   "speed_mbps": speed,
                                   "wifi": wireless,
                                   **counters, **rates})
        self.traffic = {name: value for name, value in self.traffic.items() if name in {i["name"] for i in interfaces}}
        return {"interfaces": interfaces}

    async def reception(self):
        rows = await self.backend.grid("status/inputs")
        if not isinstance(rows, list):
            raise BackendError("Invalid input status")
        streams, current = [], {}
        for row in rows[:32]:
            if not isinstance(row, dict):
                continue
            identity = str(row.get("uuid", ""))
            counters = {key: number(row.get(key)) for key in ("cc", "te", "unc", "ber")}
            previous = self.errors.get(identity, {})
            changes = {key: value-previous[key] if value is not None and previous.get(key) is not None
                       and value >= previous[key] else None for key, value in counters.items()}
            current[identity] = counters
            streams.append({"input": str(row.get("input", "Unknown tuner"))[:512],
                            "transponder": str(row.get("stream", ""))[:512],
                            "signal": signal(row.get("signal"), row.get("signal_scale"), "dBm"),
                            "quality": signal(row.get("snr"), row.get("snr_scale"), "dB"),
                            "bitrate_bps": number(row.get("bps")),
                            "subscriptions": number(row.get("subs")),
                            "errors": counters, "error_changes": changes})
        self.errors = current
        return {"streams": streams, "activity": "active" if streams else "idle"}
