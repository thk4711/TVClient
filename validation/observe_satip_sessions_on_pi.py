#!/usr/bin/env python3
"""Passive, bounded RTSP observation. Outputs no URLs, raw packets or credentials."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import time


def rtsp_event(packet, host):
    # AF_PACKET Ethernet IPv4/TCP; reject truncated, fragmented and VLAN packets.
    if len(packet) < 54 or packet[12:14] != bytes.fromhex('0800'):
        return None
    ip = packet[14:]
    size = (ip[0] & 15) * 4
    if ip[0] >> 4 != 4 or size < 20 or ip[9] != 6 or int.from_bytes(ip[6:8], 'big') & 0x3fff:
        return None
    tcp = ip[size:]
    if len(tcp) < 20:
        return None
    source, target = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
    sport, dport = int.from_bytes(tcp[:2], 'big'), int.from_bytes(tcp[2:4], 'big')
    if not (source == host and sport == 554 or target == host and dport == 554):
        return None
    offset = (tcp[12] >> 4) * 4
    if offset < 20:
        return None
    body = tcp[offset:]
    match = re.match(rb'(SETUP|PLAY|TEARDOWN|OPTIONS|DESCRIBE|RTSP/1.0) ', body)
    if not match:
        return None
    session = re.search(rb'(?im)^Session: *([^;\r\n]+)', body)
    return {'method': match[1].decode(),
            'session': hashlib.sha256(session[1]).hexdigest()[:12] if session else None,
            'sequence': int.from_bytes(tcp[4:8], 'big'),
            'client_port': dport if source == host else sport,
            'direction': 'response' if source == host else 'request'}


def observe(host, seconds):
    if os.geteuid() != 0:
        raise ValueError('Root required for passive packet observation')
    seen = set()
    end = time.monotonic() + seconds
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)) as stream:
        stream.settimeout(.25)
        while time.monotonic() < end:
            try:
                packet = stream.recv(65536)
            except socket.timeout:
                continue
            event = rtsp_event(packet, host)
            if event:
                identity = (event['direction'], event['client_port'], event['sequence'])
                if identity in seen:
                    continue
                seen.add(identity)
                print(json.dumps({'monotonic': time.monotonic(), **event}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=lambda value: str(ipaddress.IPv4Address(value)))
    parser.add_argument('--seconds', type=int, choices=range(1, 1801), default=900)
    args = parser.parse_args()
    observe(args.host, args.seconds)
