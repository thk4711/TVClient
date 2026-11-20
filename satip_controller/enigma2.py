"""Bounded, read-only OpenWebif access; no zap, remote control or playlist URLs."""
import ipaddress
import re
import time
import urllib.parse
import urllib.request
from xml.parsers import expat

from .backend import BackendError

MAX_RESPONSE = 2 * 1024 * 1024
PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
REFERENCE = re.compile(r"1:0:(?:1|19):[0-9A-Fa-f]{1,4}:[0-9A-Fa-f]{1,4}:[0-9A-Fa-f]{1,4}:C00000:0:0:0:", re.I)
BOUQUET = re.compile(r'1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.[A-Za-z0-9_.-]+.tv" ORDER BY bouquet')


def address(value):
    try:
        host = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError("Use the receiver's private LAN IPv4 address") from None
    if not any(host in network for network in PRIVATE):
        raise ValueError("Use the receiver's private LAN IPv4 address")
    return str(host)


def service_reference(value):
    if not isinstance(value, str) or not REFERENCE.fullmatch(value):
        raise ValueError("Only Astra 19.2E DVB HD/SD service references are supported")
    parts = value.split(":")
    if not 1 <= int(parts[3], 16) <= 65535:
        raise ValueError("Invalid DVB service ID")
    return ":".join(format(int(p, 16), "X") for p in parts[:-1]) + ":"


def xml_rows(content, row_name):
    """Reject entities/DTDs and bound both nesting and total HTTP response size."""
    if len(content) > MAX_RESPONSE:
        raise ValueError("Receiver response too large")
    parser = expat.ParserCreate()
    stack, rows, row = [], [], None
    def start(name, attrs):
        nonlocal row
        stack.append(name)
        if len(stack) > 16:
            raise ValueError("Receiver XML too deeply nested")
        if name == row_name:
            row = {}
    def text(value):
        if row is not None and len(stack) >= 2 and stack[-2] == row_name:
            row[stack[-1]] = row.get(stack[-1], "") + value
    def end(name):
        nonlocal row
        if name == row_name:
            rows.append(row)
            row = None
        stack.pop()
    def reject(*args):
        raise ValueError("Receiver XML entities are not supported")
    parser.StartElementHandler, parser.CharacterDataHandler, parser.EndElementHandler = start, text, end
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(content, True)
    except expat.ExpatError:
        raise ValueError("Invalid receiver XML") from None
    return rows


class Enigma2:
    def __init__(self, host, web_port=80, stream_port=8001, timeout=5):
        self.host = address(host)
        for port in (web_port, stream_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid receiver port")
        self.web_port, self.stream_port, self.timeout = web_port, stream_port, timeout

    def read(self, endpoint, row_name, **query):
        if endpoint not in ("about", "getservices", "epgservice"):
            raise ValueError("Unsupported read-only receiver endpoint")
        url = f"http://{self.host}:{self.web_port}/web/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        try:
            with opener.open(url, timeout=self.timeout) as response:
                deadline = time.monotonic() + self.timeout
                content = bytearray()
                while len(content) <= MAX_RESPONSE:
                    chunk = response.read1(min(8192, MAX_RESPONSE + 1 - len(content)))
                    if not chunk:
                        return xml_rows(bytes(content), row_name)
                    content.extend(chunk)
                    if time.monotonic() >= deadline:
                        raise ValueError("Receiver response timed out")
                raise ValueError("Receiver response too large")
        except (OSError, ValueError):
            raise BackendError("Enigma2 API unavailable or rejected; check address, port and OpenWebif access") from None

    def about(self):
        rows = self.read("about", "e2about")
        if len(rows) != 1 or not rows[0].get("e2model"):
            raise BackendError("The receiver does not expose the expected OpenWebif API")
        row = rows[0]
        return {"address": self.host, "model": row["e2model"][:128], "image": row.get("e2imageversion", "")[:128],
                "webif": row.get("e2webifversion", "")[:128]}

    def bouquets(self):
        found = {}
        for row in self.read("getservices", "e2service"):
            name = row.get("e2servicename", "").replace("\x86", "").replace("\x87", "").strip()
            reference = row.get("e2servicereference", "")
            if (not name or len(name) > 150 or any(ord(c) < 32 for c in name)
                    or len(reference) > 512 or not BOUQUET.fullmatch(reference)):
                continue
            found.setdefault(reference, {"name": name, "reference": reference})
        if len(found) > 500:
            raise BackendError("Receiver exposes too many TV bouquets")
        return list(found.values())

    def services(self, names):
        if not names or len(names) > 16 or len(names) != len(set(names)):
            raise ValueError("Select 1–16 distinct free-to-air TV bouquets")
        bouquets = self.bouquets()
        selected = [b for b in bouquets if b["name"] in names]
        if len(selected) != len(names):
            raise ValueError("A selected bouquet is missing or ambiguous")
        found = {}
        for bouquet in selected:
            for row in self.read("getservices", "e2service", sRef=bouquet["reference"]):
                name = row.get("e2servicename", "").replace("\x86", "").replace("\x87", "").strip()
                if not name or name == "<n/a>" or len(name) > 150 or any(ord(c) < 32 for c in name):
                    continue
                try:
                    ref = service_reference(row.get("e2servicereference"))
                except ValueError:
                    continue  # Excludes radio, markers, folders, UHD and arbitrary URL inputs.
                found.setdefault(ref, {"name": name, "reference": ref})
        if not 1 <= len(found) <= 500:
            raise ValueError("No supported references or oversized receiver bouquet selection")
        return list(found.values())

    def stream_url(self, reference):
        return f"http://{self.host}:{self.stream_port}/" + service_reference(reference)

    def events(self, reference):
        from .epg import events
        rows = []
        for row in self.read("epgservice", "e2event", sRef=service_reference(reference)):
            try:
                start = int(row["e2eventstart"])
                duration = int(row["e2eventduration"])
            except (KeyError, ValueError):
                continue
            rows.append({"start": start, "stop": start + duration, "title": row.get("e2eventtitle", ""),
                         "summary": row.get("e2eventdescription", "").replace("\x8a", "\n"),
                         "description": row.get("e2eventdescriptionextended", "").replace("\x8a", "\n")})
        return events(rows)


def profile_networks(directory, default):
    """Previously managed source networks may be revalidated during ACL growth."""
    from pathlib import Path
    from .config import library, uuid
    from .state import read_json
    result = {default}
    for source in ("satip", "enigma2"):
        path = Path(directory) / ("tv-source-" + source + ".json")
        if path.exists():
            node = read_json(path)
            if node.get("version") != 1:
                raise ValueError("Invalid TV source profile")
            library(node["library"])
            result.add(uuid(node["network_uuid"]))
    return result
