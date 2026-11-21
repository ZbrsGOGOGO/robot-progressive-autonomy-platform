import json
import socket
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from rpa_sdk.contracts import validate_frame
from rpa_sdk.legacy import (
    LegacyAgentRegistry,
    LegacyPicoReceiver,
    LegacyRecorderClient,
    LegacyRobotAdapter,
    frame_to_legacy_pico,
    legacy_pico_to_frame,
    make_legacy_server,
)


def _tracking(seq=1, capture=100):
    return {
        "sampleSequence": seq,
        "timeStampNs": capture,
        "Head": {"pose": [0, 1.5, 0, 0, 0, 0, 1], "status": 1},
        "Controller": {
            "left": {
                "pose": "0.1,1.2,0,0,0,0,1", "grip": 0.8, "trigger": 0.2,
                "axisX": -0.4, "axisY": 0.3, "primaryButton": True,
            },
            "right": {"pose": [0.5, 1.2, 0, 0, 0, 0, 1], "axisY": 0.7},
        },
    }


@contextmanager
def _serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _request(url, data=None, method=None):
    request = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=3) as response:
        return json.load(response)


class PicoInteropTests(unittest.TestCase):
    def test_array_and_string_pose_round_trip(self):
        source = json.dumps(_tracking()).encode()
        frame = legacy_pico_to_frame(source, tracking_valid=True)
        self.assertEqual(set(frame["poses"]), {"head", "left", "right"})
        self.assertEqual(frame["poses"]["left"]["position"], [0.1, 1.2, 0.0])
        self.assertEqual(frame["grip"], [True, False])
        self.assertEqual(frame["sticks"], [-0.4, 0.3, 0.0, 0.7])
        self.assertTrue(frame["buttons"]["left_primary"])
        self.assertEqual(validate_frame(frame)["schema"], "rpa.input.v1")
        self.assertEqual(
            legacy_pico_to_frame(frame_to_legacy_pico(frame), tracking_valid=True),
            frame,
        )

    def test_tracking_cannot_be_derived_from_head_status(self):
        source = _tracking()
        self.assertFalse(legacy_pico_to_frame(source)["tracking"])
        source["appState"] = {"focus": False}
        frame = legacy_pico_to_frame(source, tracking_valid=True)
        self.assertFalse(frame["tracking"])
        with self.assertRaises(ValueError):
            frame_to_legacy_pico(frame)

    def test_source_timestamp_as_explicit_historical_sequence(self):
        source = _tracking()
        del source["sampleSequence"]
        with self.assertRaises(ValueError):
            legacy_pico_to_frame(source)
        frame = legacy_pico_to_frame(source, use_timestamp_sequence=True)
        self.assertEqual(frame["sequence"], frame["capture_time_ns"])

    def test_malformed_packet_fields(self):
        for update in (
            {"sampleSequence": True},
            {"timeStampNs": -1},
            {"Head": {"pose": [0, 0, 0, 0, 0, 0, 0]}},
            {"Head": {"pose": [0, 0, 0, 0, 0, 0, float("inf")]}},
            {"Controller": {"left": {"pose": [0, 0, 0, 0, 0, 0, 1]}}},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                legacy_pico_to_frame(dict(_tracking(), **update))
        with self.assertRaises(ValueError):
            legacy_pico_to_frame(b'{"timeStampNs": 1, "timeStampNs": 2}')
        with self.assertRaises(ValueError):
            legacy_pico_to_frame(b'{"timeStampNs": NaN}')

    def test_udp_peer_batch_and_replay_gate(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.bind(("127.0.0.1", 0))
            with LegacyPicoReceiver(peer_port=sender.getsockname()[1]) as receiver:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                    other.sendto(json.dumps(_tracking(99, 200)).encode(), receiver.address)
                sender.sendto(json.dumps(_tracking(1, 100)).encode(), receiver.address)
                sender.sendto(json.dumps(_tracking(2, 100)).encode(), receiver.address)
                sender.sendto(json.dumps(_tracking(3, 103)).encode(), receiver.address)
                self.assertIsNone(receiver.read_latest(max_datagrams=1))
                frame = receiver.read_latest()
                self.assertEqual(frame["sequence"], 3)
                self.assertFalse(frame["tracking"])
                sender.sendto(json.dumps(_tracking(3, 106)).encode(), receiver.address)
                self.assertIsNone(receiver.read_latest())


class AgentInteropTests(unittest.TestCase):
    def test_agent_handshake_has_no_motion_commands(self):
        registry = LegacyAgentRegistry()
        with _serving(make_legacy_server(registry)) as base:
            poll = base + "/api/poll?device_id=robot-fixture"
            self.assertFalse(_request(poll)["known"])
            self.assertTrue(_request(base + "/api/register", {
                "device_id": "robot-fixture",
                "device_type": "robot",
                "capabilities": {"controls": ["dual_arm"]},
            })["ok"])
            self.assertEqual(_request(poll, method="POST"), {
                "ok": True, "known": True, "commands": [],
            })
            self.assertTrue(_request(base + "/api/report", {
                "device_id": "robot-fixture", "conn_id": "local-fixture", "state": "running",
            })["ok"])
        robot = LegacyRobotAdapter("robot-fixture", registry=registry)
        self.assertEqual(robot.snapshot()["robot_type"], "robot")
        self.assertEqual(robot.snapshot()["agent"]["report"]["state"], "running")
        self.assertFalse(robot.readiness()["ready"])
        self.assertFalse(robot.hold()["confirmed"])
        self.assertFalse(robot.release()["confirmed"])
        with self.assertRaises(RuntimeError):
            robot.apply({})
        with self.assertRaises(ValueError):
            make_legacy_server(registry, host="0.0.0.0")


class _FakeRecorderHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.assert_status_path()
        self._send({
            "recorder": self.server.recorder_id,
            "recording": self.server.episode_id is not None,
            "episode_id": self.server.episode_id,
        })

    def assert_status_path(self):
        if self.path != "/rec/status":
            raise AssertionError(self.path)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw)
        self.server.calls.append((self.path, payload))
        if self.path == "/rec/start":
            self.server.episode_id = "ep_fixture"
            self._send({"ok": True, "episode_id": self.server.episode_id})
        elif self.path == "/rec/stop":
            self.server.episode_id = None
            self._send({"ok": True})
        elif self.path == "/rec/mark":
            self._send({"ok": True, "mark": payload["mark"]})
        else:
            raise AssertionError(self.path)


class RecorderInteropTests(unittest.TestCase):
    def _recorder_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRecorderHandler)
        server.recorder_id = "lab-recorder"
        server.episode_id = None
        server.calls = []
        return server

    def test_local_read_and_owned_write_through_real_http(self):
        server = self._recorder_server()
        with _serving(server) as base:
            readonly = LegacyRecorderClient(base, recorder_id="lab-recorder")
            self.assertFalse(readonly.status()["recording"])
            with self.assertRaises(RuntimeError):
                readonly.start("demo", conn_id="session-fixture")
            writer = LegacyRecorderClient(base, recorder_id="lab-recorder", allow_write=True)
            self.assertEqual(writer.start("demo", conn_id="session-fixture")["episode_id"], "ep_fixture")
            self.assertEqual(writer.mark("reach")["mark"], "reach")
            self.assertTrue(writer.stop("success")["ok"])
            self.assertFalse(readonly.status()["recording"])
        self.assertEqual(
            [call[0] for call in server.calls], ["/rec/start", "/rec/mark", "/rec/stop"],
        )

    def test_recorder_identity_mismatch_prevents_writes(self):
        server = self._recorder_server()
        with _serving(server) as base:
            unmatched = LegacyRecorderClient(base, recorder_id="Lab-recorder", allow_write=True)
            with self.assertRaisesRegex(ValueError, "identity"):
                unmatched.status()
            with self.assertRaisesRegex(ValueError, "identity"):
                unmatched.start("demo", conn_id="session-fixture")
            self.assertEqual(server.calls, [])

            writer = LegacyRecorderClient(base, recorder_id="lab-recorder", allow_write=True)
            writer.start("demo", conn_id="session-fixture")
            server.recorder_id = "lab-replacement"
            with self.assertRaisesRegex(ValueError, "identity"):
                writer.mark("reach")
            with self.assertRaisesRegex(ValueError, "identity"):
                writer.stop()
        self.assertEqual([call[0] for call in server.calls], ["/rec/start"])

    def test_reject_non_loopback(self):
        for endpoint in ("http://example.test:7005", "http://127.0.0.1:7005/redirect"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                LegacyRecorderClient(endpoint, recorder_id="lab-recorder")

    def test_reject_invalid_recorder_id(self):
        for recorder_id in ("", "lab recorder", "lab/recorder", "a" * 129):
            with self.subTest(recorder_id=recorder_id), self.assertRaisesRegex(ValueError, "recorder_id"):
                LegacyRecorderClient("http://127.0.0.1:7005", recorder_id=recorder_id)
