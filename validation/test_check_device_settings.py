"""Safety of live diagnostic modes, with no receiver access."""
import contextlib
import io
import unittest
from unittest.mock import patch

from validation import check_device_settings as check


class Response:
    def __init__(self, status_code=200, data=None):
        self.status_code, self.data = status_code, data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("Unexpected diagnostic failure")

    def json(self):
        return self.data


class DiagnosticSafetyTests(unittest.TestCase):
    def run_mode(self, *flags):
        calls = []

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, path):
                calls.append(("GET", path))
                data = {"/api/device": {"hostname": "test", "timezone": "Etc/UTC", "trial": None},
                        "/api/settings": {"device_settings": True},
                        "/api/status": {"player": {"playing": "test-channel", "error": None}}}
                return Response(data=data[path])

            def post(self, path, json):
                action = json["action"]
                calls.append(("POST", action))
                if action == "reboot":
                    return Response(403)
                if action == "device" and json["args"]["hostname"] == "bad/name":
                    return Response(409)
                return Response(data={"networks": []})

        output = io.StringIO()
        with (patch.object(check.httpx, "Client", Client),
              patch.object(check.httpx, "get", return_value=Response(401)),
              patch.object(check.Path, "read_text", return_value='{"username":"test","password":"private-test-secret"}'),
              patch("sys.argv", ["check_device_settings.py", *flags]),
              contextlib.redirect_stdout(output)):
            check.main()
        self.assertNotIn("private-test-secret", output.getvalue())
        return calls

    def test_default_is_strictly_read_only(self):
        calls = self.run_mode()
        self.assertTrue(calls)
        self.assertTrue(all(method == "GET" for method, _ in calls))

    def test_exercise_never_disables_wifi_or_changes_passwords(self):
        calls = self.run_mode("--exercise")
        self.assertEqual([action for method, action in calls if method == "POST"],
                         ["reboot", "device", "device", "scan_wifi"])
