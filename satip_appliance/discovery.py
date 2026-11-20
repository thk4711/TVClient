"""Bounded SAT-IP SSDP discovery and description reads on private IPv4 LANs."""
import concurrent.futures
import fcntl
import ipaddress
import re
import select
import socket
import struct
import time
import urllib.parse
import urllib.request
import uuid

DEVICE = "urn:ses-com:device:SatIPServer:1"
PORTS = (80, 8080, 38400)
MAX_DESCRIPTION = 65536


def description_url(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid SAT-IP description URL")
    try:
        address = urllib.parse.urlsplit(value)
        host = ipaddress.IPv4Address(address.hostname)
        port = address.port or 80
    except ValueError:
        raise ValueError("Use a private LAN IPv4 HTTP description URL") from None
    if (address.scheme != "http" or not host.is_private or host.is_loopback
            or host.is_link_local or host.is_unspecified or host.is_multicast
            or address.username or address.password or address.fragment or address.query
            or port not in PORTS or address.port == 0
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", address.path) or ".." in address.path
            or any(c.isspace() for c in value)):
        raise ValueError("Use a private LAN HTTP description URL on port 80, 8080 or 38400")
    return urllib.parse.urlunsplit(("http", str(host) + (f":{port}" if port != 80 else ""), address.path, "", ""))


def server_identity(value):
    if not isinstance(value, str) or not value.startswith("uuid:"):
        raise ValueError("Missing SAT-IP server identity")
    return "uuid:" + str(uuid.UUID(value[5:]))


def parse_description(content, url):
    # Lazy import also keeps the root supervisor independent of XML packages.
    from xml.parsers import expat
    if len(content) > MAX_DESCRIPTION:
        raise ValueError("SAT-IP description is too large")
    parser = expat.ParserCreate(namespace_separator="|")
    stack, fields = [], {}
    def start(name, attrs):
        stack.append(name.split("|")[-1])
        if len(stack) > 16:
            raise ValueError("SAT-IP description is too deeply nested")
    def text(value):
        if len(stack) == 3 and stack[:2] == ["root", "device"]:
            fields[stack[-1]] = fields.get(stack[-1], "") + value
    def reject(*args):
        raise ValueError("XML document types and entities are not supported")
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: stack.pop()
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(content, True)
    except expat.ExpatError:
        raise ValueError("Invalid SAT-IP XML description") from None
    fields = {k: v.strip() for k, v in fields.items()}
    if fields.get("deviceType") != DEVICE:
        raise ValueError("The description is not a SAT-IP server")
    capabilities = fields.get("X_SATIPCAP", "")
    satellite = sum(int(n) for n in re.findall(r"(?:^|,)\s*DVBS2?-(\d+)(?=\s*(?:,|$))", capabilities))
    if not 1 <= satellite <= 32:
        raise ValueError("This server does not advertise supported DVB-S/S2 tuners")
    return {"url": description_url(url), "udn": server_identity(fields.get("UDN")),
            "name": fields.get("friendlyName", "SAT-IP server")[:128],
            "manufacturer": fields.get("manufacturer", "")[:128],
            "model": fields.get("modelName", "")[:128],
            "address": urllib.parse.urlsplit(url).hostname,
            "capabilities": capabilities[:128], "tuners": satellite}


def describe(url):
    url = description_url(url)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(url, timeout=3) as response:
            # Bound the whole read as well as individual socket operations.
            end = time.monotonic() + 4
            content = bytearray()
            while len(content) <= MAX_DESCRIPTION:
                chunk = response.read1(min(8192, MAX_DESCRIPTION + 1 - len(content)))
                if not chunk:
                    return parse_description(bytes(content), url)
                content.extend(chunk)
                if time.monotonic() >= end:
                    raise ValueError("SAT-IP description download timed out")
            raise ValueError("SAT-IP description is too large")
    except OSError:
        raise ValueError("Server description is unreachable; check its address and port") from None


def interfaces():
    result = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                request = struct.pack("256s", name.encode()[:15])
                flags = struct.unpack_from("H", fcntl.ioctl(sock, 0x8913, request), 16)[0]
                address = socket.inet_ntoa(fcntl.ioctl(sock, 0x8915, request)[20:24])
                if flags & 1 and flags & 0x1000 and not flags & 8:
                    result.append((name, address))
            except OSError:
                continue
    return result[:8]


def response_location(packet):
    if len(packet) > 8192:
        return None
    lines = packet.decode("ascii", "replace").split("\r\n")
    if not lines or lines[0].strip() != "HTTP/1.1 200 OK":
        return None
    headers = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line
               for k, v in [line.split(":", 1)]}
    if DEVICE.lower() not in (headers.get("st", "") + headers.get("usn", "")).lower():
        return None
    try:
        return description_url(headers.get("location"))
    except ValueError:
        return None


def search_servers():
    sockets, locations, errors = [], set(), []
    available = interfaces()
    message = (f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
               f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {DEVICE}\r\n'
               'USER-AGENT: unix/1.0 UPnP/1.1 satip-receiver/1.0\r\n\r\n').encode()
    try:
        for name, address in available:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.bind((address, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sockets.append(sock)
            except OSError:
                sock.close()
                errors.append(f"Discovery unavailable on {name}")
        end, next_send, attempts = time.monotonic() + 4, 0, 0
        while sockets and time.monotonic() < end and len(locations) < 16:
            if attempts < 3 and time.monotonic() >= next_send:
                for sock in sockets:
                    try:
                        sock.sendto(message, ("239.255.255.250", 1900))
                    except OSError:
                        errors.append("Could not send LAN discovery request")
                attempts += 1
                next_send = time.monotonic() + .5
            for sock in select.select(sockets, [], [], .2)[0]:
                packet = sock.recv(8193)
                location = response_location(packet)
                if location:
                    locations.add(location)
    finally:
        for sock in sockets:
            sock.close()
    servers = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(describe, url) for url in sorted(locations)]
        for task in tasks:
            try:
                server = task.result()
                servers.setdefault(server["udn"], server)
            except (OSError, ValueError):
                errors.append("A discovered server description could not be read")
    if not servers:
        for server in direct_descriptions(available):
            servers.setdefault(server["udn"], server)
    return {"servers": sorted(servers.values(), key=lambda s: (s["name"], s["address"])),
            "interfaces": [name for name, _ in available], "warnings": sorted(set(errors))}


def local_targets(available):
    """Bound fallback discovery to directly attached home subnets, at most 254 hosts."""
    targets = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for name, address in available:
            try:
                mask = socket.inet_ntoa(fcntl.ioctl(sock, 0x891b, struct.pack("256s", name.encode()[:15]))[20:24])
                network = ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
                if network.prefixlen < 24 or not network.is_private:
                    continue
                targets.update(str(ip) for ip in network.hosts())
            except (OSError, ValueError):
                continue
    return sorted(targets - {ip for _, ip in available}, key=ipaddress.IPv4Address)[:254]


def direct_descriptions(available):
    # Some home tuners (including older Digibit Twins) stop answering SSDP while
    # their description and RTSP servers still work. Probe only two conventional
    # description endpoints on the directly connected subnet, once per search.
    def probe(address):
        for port, path in ((38400, "/description.xml"), (80, "/desc.xml")):
            try:
                with socket.create_connection((address, port), timeout=.35):
                    pass
                return describe(f"http://{address}:{port}{path}")
            except (OSError, ValueError):
                continue
        return None
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        for server in pool.map(probe, local_targets(available)):
            if server:
                found.append(server)
    return found
