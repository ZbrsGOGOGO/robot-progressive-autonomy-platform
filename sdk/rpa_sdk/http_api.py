"""HTTP transport for the platform runtime, with a same-origin workbench."""

import hmac
import ipaddress
import json
import math
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 64 * 1024
SESSION_ROUTE = re.compile(r"/v1/sessions/([A-Za-z0-9_.-]{1,128})(?:/(.+))?")
ROBOT_HEALTH_ROUTE = re.compile(r"/v1/robots/([A-Za-z0-9_.-]{1,128})/health")
EPISODE_ROUTE = re.compile(r"/v1/episodes/(ep_[0-9a-f]{32})")
SESSION_OPERATIONS = {
    "arm": ("arm", ()),
    "input": ("input", None),
    "hold": ("hold", ()),
    "resume": ("resume", ()),
    "close": ("close", ()),
    "record/start": ("record_start", ("task",)),
    "record/mark": ("record_mark", ("label", "details?")),
    "record/stop": ("record_stop", ("outcome?",)),
    "action": ("play_action", None),
}


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _object(items):
    data = {}
    for key, value in items:
        if key in data:
            raise ValueError("Duplicate JSON property")
        data[key] = value
    return data


def _reject_constant(_):
    raise ValueError("Non-finite JSON constant")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is out of range")
    return parsed


def _fields(data, names):
    allowed = {name.rstrip("?") for name in names}
    if set(data) - allowed:
        raise RequestError(400, "Unexpected request fields")
    result = {}
    for name in names:
        key = name.rstrip("?")
        if key in data:
            result[key] = data[key]
        elif not name.endswith("?"):
            raise RequestError(400, "Missing field: " + key)
    return result


class PlatformHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, runtime, host, port, token):
        self.runtime = runtime
        self.token = token
        self.loopback_only = _loopback(host)
        self.workbench = Path(__file__).with_name("workbench.html").read_bytes()
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), PlatformHandler)


class PlatformHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "RPA-Platform/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        # Request metadata may contain session identifiers. Application-level
        # auditing belongs to the runtime and does not log authorization headers.
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET", send_body=False)

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _check_origin(self):
        host = self.headers.get("Host")
        if len(self.headers.get_all("Host", ())) > 1:
            raise RequestError(400, "Invalid Host header")
        if not host or any(char in host for char in "/?#@\\ \t\r\n"):
            raise RequestError(400, "Invalid Host header")
        try:
            destination = urlsplit("//" + host)
            hostname = destination.hostname
            port = destination.port
        except ValueError:
            raise RequestError(400, "Invalid Host header") from None
        if not hostname or (self.server.loopback_only and not _loopback(hostname)):
            raise RequestError(403, "Host not allowed")
        if self.server.loopback_only and port not in (None, self.server.server_port):
            raise RequestError(403, "Host port not allowed")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host, "https://" + host}:
            raise RequestError(403, "Cross-origin request rejected")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "Cross-site request rejected")

    def _authorize(self):
        if self.server.token is None:
            return
        authorization = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise RequestError(401, "Valid bearer token required")

    def _read_object(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestError(400, "Transfer-Encoding is not supported")
        lengths = self.headers.get_all("Content-Length", ())
        if len(lengths) > 1:
            raise RequestError(400, "Invalid Content-Length")
        if not lengths:
            return {}
        if not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RequestError(400, "Invalid Content-Length")
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            raise RequestError(413, "Request body too large")
        if not length:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, "Incomplete request body")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_float=_finite_float,
                               parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise RequestError(400, "Malformed JSON body") from None
        if not isinstance(value, dict):
            raise RequestError(400, "JSON body must be an object")
        return value

    def _dispatch(self, method, send_body=True):
        try:
            self._check_origin()
            if "?" in self.path or "#" in self.path:
                raise RequestError(404, "Unknown route")
            if self.path == "/" and method == "GET":
                self._send(200, "text/html; charset=utf-8", self.server.workbench, send_body)
                return
            self._authorize()
            status, result = self._route(method)
        except RequestError as error:
            status, result = error.status, {"error": str(error)}
        except KeyError:
            status, result = 404, {"error": "Requested resource was not found"}
        except ValueError as error:
            status, result = 400, {"error": str(error)}
        except RuntimeError as error:
            status, result = 409, {"error": str(error)}
        except (TimeoutError, OSError):
            status, result = 503, {"error": "Operation unavailable"}
        except Exception:
            status, result = 500, {"error": "Internal platform error"}
        try:
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            status, raw = 500, b'{"error":"Invalid runtime response"}'
        self._send(status, "application/json; charset=utf-8", raw, send_body)

    def _route(self, method):
        runtime = self.server.runtime
        if method == "GET" and self.path == "/v1/status":
            return 200, runtime.snapshot()
        health = ROBOT_HEALTH_ROUTE.fullmatch(self.path)
        if method == "GET" and health:
            return 200, runtime.robot_status(health[1])
        episode = EPISODE_ROUTE.fullmatch(self.path)
        if method == "GET" and episode:
            return 200, runtime.episode_status(episode[1])
        if method == "POST" and self.path == "/v1/sessions":
            data = _fields(self._read_object(), ("controller_id", "robot_id", "workstation_id"))
            if any(not isinstance(value, str) or not value.strip() for value in data.values()):
                raise RequestError(400, "Controller, robot, and workstation identifiers are required")
            return 201, runtime.open_session(**data)
        match = SESSION_ROUTE.fullmatch(self.path)
        if not match:
            raise RequestError(404, "Unknown route")
        session_id, operation = match.groups()
        if method == "DELETE" and operation is None:
            return 200, runtime.close(session_id)
        if method != "POST" or operation not in SESSION_OPERATIONS:
            raise RequestError(404, "Unknown route")
        function, fields = SESSION_OPERATIONS[operation]
        data = self._read_object()
        invoke = getattr(runtime, function)
        if fields is None:
            return 200, invoke(session_id, data)
        return 200, invoke(session_id, **_fields(data, fields))

    def _send(self, status, content_type, raw, send_body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="robot-platform"')
            self.end_headers()
            if send_body:
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def make_server(runtime, host="127.0.0.1", port=8000, token=None):
    """Create a server. Only a loopback listener may run without a bearer token."""
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,512}", token)):
        raise ValueError("Token must contain 1–512 visible ASCII characters")
    if not _loopback(host) and token is None:
        raise ValueError("A bearer token is required on non-loopback interfaces")
    return PlatformHTTPServer(runtime, host, port, token)
