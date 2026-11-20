"""Savvy2 and Pico wire compatibility without implicit actuator authority."""

import copy
import ipaddress
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_TRACKING_BYTES = 32768
MAX_SIDECAR_BYTES = 65536
_ROLES = ("head", "left", "right")
_BUTTONS = {
    "axisClick": "axis_click",
    "primaryButton": "primary",
    "secondaryButton": "secondary",
    "menuButton": "menu",
}


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _decode_json(data: bytes, limit: int) -> dict:
    if len(data) > limit:
        raise ValueError("message exceeds byte limit")
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid JSON message") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON message must be an object")
    return value


def _mapping(value: Any, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(name + " must be an object")
    return value


def _int64(value: Any, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(name + " must be a non-negative int64")
    return value


def _number(value: Any, name: str, bounds: Optional[Tuple[float, float]] = None) -> float:
    if type(value) not in (int, float):
        raise ValueError(name + " must be a number")
    try:
        num = float(value)
    except OverflowError as exc:
        raise ValueError(name + " must be finite") from exc
    if not math.isfinite(num) or (bounds and not bounds[0] <= num <= bounds[1]):
        raise ValueError(name + " is outside its valid range")
    return num


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(name + " must be a boolean")
    return value


def _vector(value: Any, length: int, field: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{field} must contain {length} values")
    return [_number(item, field) for item in value]


def _pose7(value: Any, field: str) -> list:
    if isinstance(value, str):
        try:
            value = [float(item) for item in value.split(",")]
        except ValueError as exc:
            raise ValueError(field + " is not a valid pose string") from exc
    pose = _vector(value, 7, field)
    if any(abs(component) > 1000.0 for component in pose[:3]):
        raise ValueError(field + " position exceeds the canonical protocol range")
    norm = math.hypot(*pose[3:])
    if not 0.5 <= norm <= 1.5:
        raise ValueError(field + " quaternion norm is invalid")
    return pose[:3] + [item / norm for item in pose[3:]]


def legacy_pico_to_frame(
    packet: Any,
    *,
    tracking_valid: bool = False,
    grip_threshold: float = 0.5,
    use_timestamp_sequence: bool = False,
) -> Dict[str, Any]:
    """Decode original Pico TrackingData. Tracking comes from a trusted caller.

    The compact packet carries head status but lacks verified per-hand tracking
    and focus. A network packet alone is insufficient to assert tracking.
    """
    if isinstance(packet, (bytes, bytearray)):
        packet = _decode_json(bytes(packet), MAX_TRACKING_BYTES)
    packet = _mapping(packet, "tracking packet")
    tracking_valid = _boolean(tracking_valid, "tracking_valid")
    use_timestamp_sequence = _boolean(use_timestamp_sequence, "use_timestamp_sequence")
    threshold = _number(grip_threshold, "grip_threshold", (0.0, 1.0))
    if threshold <= 0:
        raise ValueError("grip_threshold must be positive")
    capture_ns = _int64(packet.get("timeStampNs"), "timeStampNs")
    seq = packet.get("sampleSequence")
    if "sampleSequence" not in packet and use_timestamp_sequence:
        seq = capture_ns
    sequence = _int64(seq, "sampleSequence")

    head = _mapping(packet.get("Head"), "Head")
    pair = _mapping(packet.get("Controller"), "Controller")
    poses = {}
    grip, trigger, sticks, buttons = [], [], [], {}
    for role in _ROLES:
        source = head if role == "head" else _mapping(pair.get(role), "Controller." + role)
        pose = _pose7(source.get("pose"), role + ".pose")
        poses[role] = {"position": pose[:3], "rotation": pose[3:]}
        if role == "head":
            continue
        grip.append(_number(source.get("grip", 0), role + ".grip", (0.0, 1.0)) >= threshold)
        trigger.append(_number(source.get("trigger", 0), role + ".trigger", (0.0, 1.0)))
        for axis in ("axisX", "axisY"):
            sticks.append(_number(source.get(axis, 0), role + "." + axis, (-1.0, 1.0)))
        for wire, normalized in _BUTTONS.items():
            buttons[role + "_" + normalized] = _boolean(source.get(wire, False), wire)

    app_state = _mapping(packet.get("appState", {}), "appState")
    focused = _boolean(app_state.get("focus", True), "appState.focus")
    return {
        "sequence": sequence,
        "capture_time_ns": capture_ns,
        "poses": poses,
        "grip": grip,
        "trigger": trigger,
        "sticks": sticks,
        "tracking": tracking_valid and focused,
        "buttons": buttons,
    }


def frame_to_legacy_pico(
    frame: Mapping, *, timestamp_ns: Optional[int] = None, head_status: int = 0,
) -> Dict[str, Any]:
    """Encode TrackingData without transport, frame or clock-domain conversion."""
    frame = _mapping(frame, "frame")
    sequence = _int64(frame.get("sequence"), "sequence")
    timestamp = _int64(
        frame.get("capture_time_ns") if timestamp_ns is None else timestamp_ns,
        "capture_time_ns",
    )
    tracked = _boolean(frame.get("tracking"), "tracking")
    if not tracked:
        raise ValueError("untracked input has no safe legacy actuator representation")
    poses = _mapping(frame.get("poses"), "poses")
    packed = {}
    for role in _ROLES:
        pose = _mapping(poses.get(role), "poses." + role)
        packed[role] = _pose7(
            _vector(pose.get("position"), 3, "position")
            + _vector(pose.get("rotation"), 4, "rotation"),
            "poses." + role,
        )

    grip = frame.get("grip", [False, False])
    if not isinstance(grip, (list, tuple)) or len(grip) != 2:
        raise ValueError("grip must contain two booleans")
    trigger = _vector(frame.get("trigger", [0, 0]), 2, "trigger")
    sticks = _vector(frame.get("sticks", [0, 0, 0, 0]), 4, "sticks")
    buttons = _mapping(frame.get("buttons", {}), "buttons")
    controllers = {}
    for i, side in enumerate(("left", "right")):
        ctrl = {
            "pose": packed[side],
            "axisX": _number(sticks[2 * i], "axisX", (-1.0, 1.0)),
            "axisY": _number(sticks[2 * i + 1], "axisY", (-1.0, 1.0)),
            "grip": float(_boolean(grip[i], "grip")),
            "trigger": _number(trigger[i], "trigger", (0.0, 1.0)),
        }
        for wire, normalized in _BUTTONS.items():
            ctrl[wire] = _boolean(buttons.get(side + "_" + normalized, False), wire)
        controllers[side] = ctrl
    return {
        "Head": {"pose": packed["head"], "status": _int64(head_status, "head_status")},
        "Controller": controllers,
        "sampleSequence": sequence,
        "timeStampNs": timestamp,
    }


class LegacyPicoReceiver:
    """Bounded loopback UDP reader for synthetic/replayed TrackingData."""

    def __init__(self, port: int = 0, *, peer_port: Optional[int] = None):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port is invalid")
        if peer_port is not None and (
            type(peer_port) is not int or not 1 <= peer_port <= 65535
        ):
            raise ValueError("peer_port is invalid")
        self.peer_port = peer_port
        self._last_sequence = -1
        self._last_capture_ns = -1
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind(("127.0.0.1", port))
            self._socket.setblocking(False)
        except OSError:
            self._socket.close()
            raise
        self.address = self._socket.getsockname()

    def read_latest(
        self, *, max_datagrams: int = 8, tracking_valid: bool = False,
        use_timestamp_sequence: bool = False,
    ) -> Optional[dict]:
        """Consume a bounded batch. Recreate the receiver for a new sender epoch."""
        if type(max_datagrams) is not int or not 1 <= max_datagrams <= 64:
            raise ValueError("max_datagrams must be in [1, 64]")
        _boolean(tracking_valid, "tracking_valid")
        _boolean(use_timestamp_sequence, "use_timestamp_sequence")
        latest = None
        for _ in range(max_datagrams):
            try:
                packet, peer = self._socket.recvfrom(MAX_TRACKING_BYTES + 1)
            except BlockingIOError:
                break
            if peer[0] != "127.0.0.1" or (self.peer_port is not None and peer[1] != self.peer_port):
                continue
            try:
                frame = legacy_pico_to_frame(
                    packet, tracking_valid=tracking_valid,
                    use_timestamp_sequence=use_timestamp_sequence,
                )
            except ValueError:
                continue
            seq, capture_ns = frame["sequence"], frame["capture_time_ns"]
            if seq <= self._last_sequence or capture_ns <= self._last_capture_ns:
                continue
            self._last_sequence, self._last_capture_ns = seq, capture_ns
            latest = frame
        return latest

    def close(self):
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _validate_id(value, name):
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 128
        or not value.isascii() or not all(c.isalnum() or c in "-_." for c in value)
    ):
        raise ValueError(name + " must be a bounded identifier")
    return value


class LegacyAgentRegistry:
    """Read-only management plane for existing Pico and Savvy2 agents."""

    def __init__(self, *, max_devices: int = 64):
        if type(max_devices) is not int or not 1 <= max_devices <= 1024:
            raise ValueError("max_devices must be in [1,1024]")
        self._devices = {}
        self._lock = threading.Lock()
        self._max_devices = max_devices

    def register(self, data: Mapping) -> dict:
        device_id = _validate_id(data.get("device_id"), "device_id")
        kind = data.get("device_type")
        if kind not in ("robot", "pico"):
            raise ValueError("unsupported device type")
        capabilities = _mapping(data.get("capabilities", {}), "capabilities")
        with self._lock:
            known = self._devices.get(device_id)
            if known is None and len(self._devices) >= self._max_devices:
                raise ValueError("registry capacity reached")
            if known is not None and known["device_type"] != kind:
                raise ValueError("device type changed")
            entry = {
                "device_type": kind,
                "capabilities": copy.deepcopy(dict(capabilities)),
                "last_seen_ns": time.monotonic_ns(),
            }
            if known and "report" in known:
                entry["report"] = known["report"]
            self._devices[device_id] = entry
        return {"ok": True}

    def poll(self, device_id: str) -> dict:
        device_id = _validate_id(device_id, "device_id")
        with self._lock:
            entry = self._devices.get(device_id)
            if entry is not None:
                entry["last_seen_ns"] = time.monotonic_ns()
            return {"ok": True, "known": entry is not None, "commands": []}

    def report(self, data: Mapping) -> dict:
        device_id = _validate_id(data.get("device_id"), "device_id")
        state = _validate_id(data.get("state"), "state")
        conn_id = data.get("conn_id")
        if conn_id is not None and conn_id != "":
            conn_id = _validate_id(conn_id, "conn_id")
        with self._lock:
            entry = self._devices.get(device_id)
            if entry is None:
                return {"ok": False, "known": False}
            entry["last_seen_ns"] = time.monotonic_ns()
            entry["report"] = {"state": state, "conn_id": conn_id}
        return {"ok": True}

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._devices)


def make_legacy_server(
    registry: LegacyAgentRegistry, host: str = "127.0.0.1", port: int = 0,
) -> ThreadingHTTPServer:
    """Serve the unauthenticated legacy control-plane protocol on loopback only."""
    if host != "127.0.0.1":
        raise ValueError("legacy server must bind to IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, _format, *_args):
            # Paths can contain device identifiers from legacy agents.
            pass

        def _respond(self, status, value):
            body = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_SIDECAR_BYTES:
                raise ValueError("invalid request length")
            if not length:
                return {}
            return _decode_json(self.rfile.read(length), MAX_SIDECAR_BYTES)

        def _handle(self, method):
            try:
                route = urlsplit(self.path)
                if route.path == "/api/poll" and method in ("GET", "POST"):
                    device_id = parse_qs(route.query).get("device_id", [""])[0]
                    result = registry.poll(device_id)
                elif route.path == "/api/register" and method == "POST":
                    result = registry.register(self._body())
                elif route.path == "/api/report" and method == "POST":
                    result = registry.report(self._body())
                else:
                    self._respond(404, {"ok": False, "error": "not_found"})
                    return
                self._respond(200, result)
            except (ValueError, TimeoutError):
                self._respond(400, {"ok": False, "error": "invalid_request"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Savvy2RecorderClient:
    """Local Recorder API with explicit single-owner write opt-in."""

    def __init__(self, base_url: str, *, allow_write: bool = False, timeout_s: float = 3.0):
        url = urlsplit(base_url)
        try:
            address = ipaddress.ip_address(url.hostname or "")
        except ValueError as exc:
            raise ValueError("Recorder URL must use a loopback IP literal") from exc
        if (
            url.scheme != "http" or not address.is_loopback or not url.port
            or url.username is not None or url.password is not None
            or url.path not in ("", "/") or url.query or url.fragment
        ):
            raise ValueError("Recorder URL must be an HTTP loopback origin")
        self._origin = base_url.rstrip("/")
        self._timeout = _number(timeout_s, "timeout_s", (0.05, 120.0))
        self._allow_write = _boolean(allow_write, "allow_write")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._episode_id = None
        self._lock = threading.Lock()

    def _request(self, path: str, body: Optional[dict] = None) -> dict:
        if body is None:
            req = Request(self._origin + path)
        else:
            if not self._allow_write:
                raise RuntimeError("Recorder writes require explicit exclusive-owner opt-in")
            encoded = json.dumps(body, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_SIDECAR_BYTES:
                raise ValueError("Recorder request exceeds byte limit")
            req = Request(
                self._origin + path, data=encoded,
                headers={"Content-Type": "application/json"}, method="POST",
            )
        try:
            response = self._opener.open(req, timeout=self._timeout)
        except HTTPError as exc:
            if exc.code != 409:
                exc.close()
                raise
            response = exc
        with response:
            return _decode_json(response.read(MAX_SIDECAR_BYTES + 1), MAX_SIDECAR_BYTES)

    def status(self) -> dict:
        result = self._request("/rec/status")
        if result.get("recorder") != "savvy2" or type(result.get("recording")) is not bool:
            raise ValueError("endpoint is not a Savvy2 Recorder")
        return result

    def start(
        self, task_name: str, *, conn_id: str, operator_id: str = "",
        transport: str = "lan", with_chassis: bool = False,
        with_gripper: bool = False, with_waist: bool = False,
        with_head: bool = False,
    ) -> dict:
        if not isinstance(task_name, str) or not 1 <= len(task_name.strip()) <= 200:
            raise ValueError("task_name must contain 1–200 characters")
        if not isinstance(operator_id, str) or len(operator_id) > 128:
            raise ValueError("operator_id must be a bounded string")
        if transport not in ("lan", "agora"):
            raise ValueError("unknown Savvy2 Recorder transport")
        body = {
            "task_name": task_name,
            "conn_id": _validate_id(conn_id, "conn_id"),
            "operator_id": operator_id,
            "transport": transport,
            "with_chassis": _boolean(with_chassis, "with_chassis"),
            "with_gripper": _boolean(with_gripper, "with_gripper"),
            "with_waist": _boolean(with_waist, "with_waist"),
            "with_head": _boolean(with_head, "with_head"),
        }
        with self._lock:
            if not self._allow_write:
                raise RuntimeError("Recorder writes require explicit exclusive-owner opt-in")
            if self._episode_id is not None or self.status()["recording"]:
                raise RuntimeError("Recorder is already recording")
            result = self._request("/rec/start", body)
            if result.get("ok") is True:
                self._episode_id = _validate_id(result.get("episode_id"), "episode_id")
            return result

    def _check_owned_episode(self):
        if not self._allow_write or self._episode_id is None:
            raise RuntimeError("no owned Recorder episode")
        status = self.status()
        if not status["recording"] or status.get("episode_id") != self._episode_id:
            raise RuntimeError("Recorder episode ownership changed")

    def mark(self, mark: str, note: str = "") -> dict:
        with self._lock:
            self._check_owned_episode()
            return self._request("/rec/mark", {"mark": mark, "note": note})

    def stop(self, mark: str = "unmarked", note: str = "") -> dict:
        with self._lock:
            self._check_owned_episode()
            reply = self._request("/rec/stop", {"mark": mark, "note": note})
            if reply.get("ok") is True:
                self._episode_id = None
            return reply


class Savvy2RobotAdapter:
    """Discovery and diagnostics for legacy deployments. Motion is unavailable."""

    robot_type = "savvy2"

    def __init__(
        self, robot_id: str, *,
        registry: Optional[LegacyAgentRegistry] = None,
        recorder: Optional[Savvy2RecorderClient] = None,
    ):
        self.robot_id = _validate_id(robot_id, "robot_id")
        self._registry = registry
        self._recorder = recorder

    def capabilities(self) -> dict:
        return {
            "motion": False,
            "actions": False,
            "legacy_registry": self._registry is not None,
            "legacy_recorder": self._recorder is not None,
            "integration": "legacy_observation",
        }

    def readiness(self) -> dict:
        return {"ready": False, "reason": "verified_actuator_gateway_required"}

    def apply(self, _frame: dict) -> dict:
        raise RuntimeError("Savvy2 actuator gateway is not configured")

    def hold(self) -> dict:
        return {"confirmed": False, "reason": "verified_actuator_gateway_required"}

    def release(self) -> dict:
        return self.hold()

    def snapshot(self) -> dict:
        entry = None
        if self._registry is not None:
            entry = self._registry.snapshot().get(self.robot_id)
        return {
            "robot_id": self.robot_id,
            "robot_type": self.robot_type,
            "readiness": self.readiness(),
            "agent": entry,
        }

    def recorder_status(self) -> dict:
        if self._recorder is None:
            raise RuntimeError("Recorder endpoint is not configured")
        return self._recorder.status()
