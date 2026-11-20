"""Loopback-only Tvheadend admin API, isolated from the playback process."""
import base64
import json
import urllib.parse
import urllib.request

from .audio import GERMAN


class BackendError(Exception):
    pass


class Backend:
    def __init__(self, credentials, timeout=10):
        self.timeout = timeout
        self.header = "Basic " + base64.b64encode(
            (credentials["username"] + ":" + credentials["password"]).encode()).decode()

    def _request(self, endpoint, values, write):
        # Blocking urllib (not the project's httpx) runs in a worker thread behind
        # the complete() cancellation barrier, isolating backend admin calls from
        # the async web stack and ensuring a mutation finishes before rollback.
        body = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list, bool)) else v
                                      for k, v in values.items()}).encode()
        address = "http://127.0.0.1:9981/api/" + endpoint
        request = urllib.request.Request(address if write else address + "?" + body.decode(),
                                         data=body if write else None,
                                         headers={"Authorization": self.header})
        # Redirects could leak the Authorization header; never follow them.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=self.timeout) as response:
                content = response.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024:
                    raise BackendError("Backend response too large")
                return json.loads(content) if content.strip() else {}
        except (OSError, ValueError):
            raise BackendError("Cannot connect to Tvheadend or request was rejected") from None

    async def request(self, endpoint, write=False, **values):
        import asyncio
        from .lifecycle import complete
        # Complete a mutating request before cancellation/rollback, preventing
        # an abandoned worker thread from changing the backend after restoration.
        task = asyncio.create_task(asyncio.to_thread(self._request, endpoint, values, write))
        return await complete(task)

    async def grid(self, endpoint):
        result = await self.request(endpoint, limit=10000, hidemode="none", all=True)
        entries = result.get("entries", [])
        if result.get("total", len(entries)) != len(entries):
            raise BackendError("Backend grid was truncated")
        return entries

    async def save(self, **node):
        return await self.request("idnode/save", write=True, node=node)


def candidate(service, streams):
    # Fail closed for unknown/encrypted status; CA components independently exclude.
    if service.get("encrypted") is not False or not service.get("enabled") or service.get("caid"):
        return None
    if any(s.get("type") == "CA" or s.get("caids") for s in streams):
        return None
    videos = [s for s in streams if s.get("type") in ("H264", "MPEG2VIDEO", "HEVC", "MPEG4VIDEO")]
    if len(videos) != 1 or videos[0]["type"] not in ("H264", "MPEG2VIDEO"):
        return None
    video = videos[0]
    if video.get("width", 0) > 1920 or video.get("height", 0) > 1080:
        return None
    # Prefer normal German audio (0); some broadcasters tag their sole
    # programme track as clean effects (1). Reject accessibility/unknown types.
    audio = sorted([s for s in streams if s.get("type") in ("MPEG2AUDIO", "AC3", "EAC3", "AAC")
                    and s.get("language") in GERMAN
                    and s.get("audio_type") in (0, 1)],
                   key=lambda s: (s["audio_type"], s.get("type") != "MPEG2AUDIO"))
    if not audio:
        return None
    return {"service_uuid": service["uuid"], "mux_uuid": service["multiplex_uuid"],
            "name": service.get("svcname") or "Unnamed channel",
            "video": "mpeg2" if video["type"] == "MPEG2VIDEO" else "h264",
            "audio_pid": audio[0]["pid"], "encrypted": False}
