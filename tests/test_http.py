"""Exercise the public transport with a hardware-free runtime stand-in."""

import http.client
import json
import threading
import unittest
from contextlib import contextmanager

from rpa_sdk.client import PlatformClient, PlatformError
from rpa_sdk.http_api import MAX_REQUEST_BYTES, make_server


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"controllers": [], "robots": [], "sessions": []}

    def robot_status(self, robot_id):
        if robot_id != "mock":
            raise KeyError(robot_id)
        return {"robot_id": "mock", "ready": True}

    def open_session(self, controller_id, robot_id, workstation_id):
        values = (controller_id, robot_id, workstation_id)
        self.calls.append(("open_session", *values))
        return {"session_id": "example"}

    def arm(self, session_id):
        if session_id == "conflict":
            raise RuntimeError("Already held")
        self.calls.append(("arm", session_id))
        return {"state": "armed"}

    def input(self, session_id, payload):
        self.calls.append(("input", session_id, payload))
        return {"accepted": True}

    def hold(self, session_id):
        self.calls.append(("hold", session_id))
        return {"state": "held"}

    def resume(self, session_id):
        self.calls.append(("resume", session_id))
        return {"state": "armed"}

    def close(self, session_id):
        self.calls.append(("close", session_id))
        return {"state": "closed"}

    def record_start(self, session_id, task):
        self.calls.append(("record_start", session_id, task))
        return {"episode_id": "episode1"}

    def record_mark(self, session_id, label, details=None):
        self.calls.append(("record_mark", session_id, label, details))
        return {"marked": True}

    def record_stop(self, session_id, outcome="complete"):
        self.calls.append(("record_stop", session_id, outcome))
        return {"outcome": outcome}

    def play_action(self, session_id, action):
        self.calls.append(("play_action", session_id, action))
        return {"accepted": True}


@contextmanager
def endpoint(token=None):
    runtime = FakeRuntime()
    server = make_server(runtime, port=0, token=token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield runtime, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class PlatformTransportTests(unittest.TestCase):
    def test_client_dispatches_runtime_operations(self):
        with endpoint() as (runtime, port):
            client = PlatformClient("http://127.0.0.1:" + str(port))
            self.assertEqual(client.snapshot()["sessions"], [])
            self.assertTrue(client.robot_status("mock")["ready"])
            session_id = client.open_session("pico", "mock", "bench-1")["session_id"]
            frame = {"tracking": True, "sequence": 17}
            self.assertEqual(client.arm(session_id)["state"], "armed")
            self.assertTrue(client.input(session_id, frame)["accepted"])
            self.assertEqual(client.hold(session_id)["state"], "held")
            self.assertEqual(client.resume(session_id)["state"], "armed")
            self.assertEqual(client.record_start(session_id, "pick")["episode_id"], "episode1")
            self.assertTrue(client.record_mark(session_id, "grasp", {"ok": True})["marked"])
            self.assertEqual(client.record_stop(session_id)["outcome"], "complete")
            action = {"name": "stationary", "frames": []}
            self.assertTrue(client.play_action(session_id, action)["accepted"])
            self.assertEqual(client.close(session_id)["state"], "closed")
            self.assertEqual(runtime.calls, [
                ("open_session", "pico", "mock", "bench-1"),
                ("arm", "example"),
                ("input", "example", frame),
                ("hold", "example"),
                ("resume", "example"),
                ("record_start", "example", "pick"),
                ("record_mark", "example", "grasp", {"ok": True}),
                ("record_stop", "example", "complete"),
                ("play_action", "example", action),
                ("close", "example"),
            ])

    def test_token_required_on_json_routes_when_configured(self):
        with endpoint(token="test-token") as (runtime, port):
            base = "http://127.0.0.1:" + str(port)
            with self.assertRaises(PlatformError) as failure:
                PlatformClient(base).open_session("pico", "mock", "bench-1")
            self.assertEqual(failure.exception.status, 401)
            self.assertEqual(runtime.calls, [])
            self.assertEqual(PlatformClient(base, "test-token").snapshot()["sessions"], [])

    def test_workbench_shell_has_security_headers(self):
        with endpoint(token="test-token") as (_, port):
            status, body, headers = request(port, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("机器人渐进式自主平台", body.decode())
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn(b"test-token", body)

    def test_non_loopback_needs_token(self):
        with self.assertRaisesRegex(ValueError, "non-loopback"):
            make_server(FakeRuntime(), "0.0.0.0", 0)

    def test_rejects_cross_origin_and_rebound_hosts(self):
        with endpoint() as (_, port):
            for headers in (
                {"Origin": "https://example.invalid"},
                {"Host": "example.invalid:" + str(port)},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                status, _, _ = request(port, "GET", "/v1/status", headers=headers)
                self.assertEqual(status, 403)

    def test_rejects_malformed_or_excessive_json(self):
        with endpoint() as (runtime, port):
            target = "/v1/sessions/example/input"
            headers = {"Content-Type": "application/json"}
            for body in (
                b'{"tracking": true',
                b'["not an object"]',
                b'{"x": 1, "x": 2}',
                b'{"x": NaN}',
                b'{"x": 1e309}',
            ):
                self.assertEqual(request(port, "POST", target, body, headers)[0], 400)
            self.assertEqual(request(port, "POST", target, b'x' * (MAX_REQUEST_BYTES + 1), headers)[0], 413)
            self.assertEqual(request(port, "POST", target, b'{}')[0], 415)
            self.assertEqual(runtime.calls, [])

    def test_reports_not_found_and_precondition_errors(self):
        with endpoint() as (_, port):
            client = PlatformClient("http://127.0.0.1:" + str(port))
            with self.assertRaises(PlatformError) as missing:
                client.robot_status("missing")
            self.assertEqual(missing.exception.status, 404)
            with self.assertRaises(PlatformError) as conflict:
                client.arm("conflict")
            self.assertEqual(conflict.exception.status, 409)
            status, raw, _ = request(port, "GET", "/v1/not-a-route")
            self.assertEqual(status, 404)
            self.assertIn("error", json.loads(raw))


if __name__ == "__main__":
    unittest.main()
