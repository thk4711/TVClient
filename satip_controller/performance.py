"""Bounded, unprivileged Linux performance counters; never read process secrets."""
import os
from pathlib import Path
import time

from .telemetry import text


class Performance:
    def __init__(self, root=Path("/"), clock=time.monotonic):
        self.root, self.clock = Path(root), clock
        self.cpus, self.disks, self.processes = {}, {}, {}
        self.previous_time = None
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")

    def read(self, path):
        return text(self.root / path)

    def collect(self):
        now = self.clock()
        elapsed = now - self.previous_time if self.previous_time is not None else None
        valid_interval = elapsed is not None and 0 < elapsed <= 15
        cpus, readings = {}, []
        for line in (self.read("proc/stat") or "").splitlines():
            fields = line.split()
            if not fields or not (fields[0] == "cpu" or fields[0][3:].isdigit() and fields[0].startswith("cpu")):
                continue
            values = [int(v) for v in fields[1:9]]
            if len(values) < 5:
                continue
            name = fields[0]
            total, idle = sum(values), sum(values[3:5])
            previous = self.cpus.get(name)
            percent = None
            if valid_interval and previous and total > previous[0] and idle >= previous[1]:
                percent = round(max(0, min(100, 100 * (1 - (idle - previous[1]) / (total - previous[0])))), 1)
            cpus[name] = total, idle
            frequency = self.read("sys/devices/system/cpu/" + name + "/cpufreq/scaling_cur_freq") if name != "cpu" else None
            readings.append({"name": name, "percent": percent,
                             "frequency_mhz": int(frequency) / 1000 if frequency and frequency.isdigit() else None})
        if not cpus:
            raise OSError("CPU counters unavailable")
        memory = {}
        for line in (self.read("proc/meminfo") or "").splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) * 1024
        total, available = memory.get("MemTotal"), memory.get("MemAvailable")
        swap_total, swap_free = memory.get("SwapTotal"), memory.get("SwapFree")
        load = (self.read("proc/loadavg") or "").split()
        disks, disk_readings = {}, []
        # Whole physical devices only: do not double-count partitions or loop/RAM disks.
        for path in sorted((self.root / "sys/block").glob("*"))[:32]:
            if not (path / "device").exists():
                continue
            fields = (text(path / "stat") or "").split()
            if len(fields) < 11:
                continue
            read_bytes, write_bytes = int(fields[2]) * 512, int(fields[6]) * 512
            old = self.disks.get(path.name)
            rates = [None, None]
            if valid_interval and old:
                rates = [round((current - before) / elapsed) if current >= before else None
                         for current, before in zip((read_bytes, write_bytes), old)]
            disks[path.name] = read_bytes, write_bytes
            disk_readings.append({"name": path.name, "read_bytes_per_second": rates[0],
                                  "write_bytes_per_second": rates[1]})
        processes, process_readings = {}, []
        scanned, truncated = 0, False
        # Limit both count and wall time even on hosts with very many processes.
        with os.scandir(self.root / "proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                if scanned >= 512 or self.clock() - now > .25:
                    truncated = True
                    break
                scanned += 1
                try:
                    stat = text(Path(entry.path) / "stat") or ""
                    start, end = stat.index("("), stat.rindex(")")
                    fields = stat[end + 2:].split()
                    ticks = int(fields[11]) + int(fields[12])
                    identity = (int(entry.name), int(fields[19]))  # PID + start time prevents reuse spikes.
                    rss = int(fields[21]) * self.page_size
                    if rss <= 0:
                        continue
                    old = self.processes.get(identity)
                    percent = round(100 * (ticks - old) / self.ticks / elapsed, 1) if (
                        valid_interval and old is not None and ticks >= old) else None
                    processes[identity] = ticks
                    process_readings.append({"pid": identity[0], "name": stat[start + 1:end][:64],
                                             "cpu_percent": percent, "memory_bytes": rss})
                except (OSError, ValueError, IndexError):
                    continue  # Processes may exit at any point during collection.
        process_readings.sort(key=lambda p: (p["cpu_percent"] or 0, p["memory_bytes"]), reverse=True)
        self.cpus, self.disks, self.processes = cpus, disks, processes
        self.previous_time = now
        return {"cpu_percent": readings[0]["percent"], "cores": [r for r in readings if r["name"] != "cpu"],
                "load_averages": [float(v) for v in load[:3]],
                "memory_total": total, "memory_available": available,
                "memory_used": max(0, total - available) if total is not None and available is not None else None,
                "memory_cached": memory.get("Cached"),
                "swap_total": swap_total,
                "swap_used": max(0, swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
                "disks": disk_readings, "processes": process_readings[:12], "processes_truncated": truncated}
