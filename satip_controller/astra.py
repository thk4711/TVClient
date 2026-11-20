"""Complete Astra coverage without relying on Tvheadend's colliding scanfile IDs."""
from pathlib import Path

from .backend import BackendError

TABLES = Path("/usr/share/dvb/dvb-s")


def sections(text):
    # DVBv5 tables repeat broadcaster section names: ConfigParser would merge
    # distinct transponders. Treat every section as an independent tuning record.
    record = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            if record:
                yield record
            record = {}
        elif record is not None and "=" in line:
            key, value = line.split("=", 1)
            record[key.strip()] = value.strip()
    if record:
        yield record


def transponders(directory=TABLES):
    files = sorted(directory.glob("Astra-*-19.2E"))
    if not files:
        raise BackendError("Installed Astra 19.2E satellite tables are missing")
    result = {}
    for path in files:
        for record in sections(path.read_text()):
            try:
                system = {"DVBS": "DVB-S", "DVBS2": "DVB-S2"}[record["DELIVERY_SYSTEM"]]
                pol = {"HORIZONTAL": "H", "VERTICAL": "V"}[record["POLARIZATION"]]
                frequency, rate = int(record["FREQUENCY"]), int(record["SYMBOL_RATE"])
                if not 10000000 <= frequency <= 30000000 or not 100000 <= rate <= 60000000:
                    raise ValueError("Invalid satellite tuning range")
                if not 10700000 <= frequency <= 12750000:
                    continue  # Tables also contain Ka-band feeds; conventional LNB is Ku-only.
                modulation = record.get("MODULATION", "QPSK" if system == "DVB-S" else "AUTO")
                modulation = {"APSK/16": "16APSK", "APSK/32": "32APSK"}.get(modulation, modulation)
                node = {"delsys": system, "frequency": frequency, "symbolrate": rate,
                        "polarisation": pol, "modulation": modulation,
                        "fec": record.get("INNER_FEC", "AUTO")}
            except (KeyError, ValueError):
                raise BackendError("Invalid installed Astra satellite table") from None
            # Several spacecraft tables overlap. Preserve the first definition,
            # like Tvheadend import, rather than creating duplicate RF muxes.
            result.setdefault((frequency, pol), node)
    if not 1 <= len(result) <= 256:
        raise BackendError("Missing or oversized Astra satellite table")
    return list(result.values())


async def supplement(backend, network, directory=TABLES):
    """Add missing RF muxes only; caller must hold the scan/playback lease."""
    networks = await backend.grid("mpegts/network/grid")
    if not any(n["uuid"] == network and n.get("orbital_pos") == "19.2E" for n in networks):
        return 0  # Never apply Astra definitions to another satellite.
    desired = transponders(directory)
    muxes = [m for m in await backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
    known = {(int(m.get("frequency", 0)), m.get("polarisation")) for m in muxes}
    missing = [m for m in desired if (m["frequency"], m["polarisation"]) not in known]
    if len(muxes) + len(missing) > 256:
        raise BackendError("Oversized Astra network")
    for mux in missing:
        await backend.request("mpegts/network/mux_create", write=True, uuid=network,
                              conf={**mux, "enabled": 0, "epg": 0, "scan_state": 0})
    # Check readback before scanning; retries fill an interrupted import without
    # replacing UUIDs, mappings, tuning parameters or enable/EPG flags.
    actual = {(int(m.get("frequency", 0)), m.get("polarisation"))
              for m in await backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network}
    if any((m["frequency"], m["polarisation"]) not in actual for m in desired):
        raise BackendError("Incomplete Astra multiplex import")
    return len(missing)
