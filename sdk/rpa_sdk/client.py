"""Dependency-free Python client for the platform v1 API."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class PlatformError(Exception):
    """A platform HTTP error or an unavailable/malformed transport response."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PlatformClient:
    def __init__(self, base_url="http://127.0.0.1:8765", token=None, timeout=5.0):
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.query or url.fragment):
            raise ValueError("Expected an HTTP(S) platform base URL without credentials or query")
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _decode(response):
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PlatformError("Platform response exceeds the size limit")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as error:
            raise PlatformError("Invalid JSON platform response") from error
        if not isinstance(result, dict):
            raise PlatformError("Platform response is not a JSON object")
        return result

    def _request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token is not None:
            headers["Authorization"] = "Bearer " + self.token
        req = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                return self._decode(response)
        except HTTPError as error:
            try:
                result = self._decode(error)
                message = str(result.get("error", "Platform request failed"))
            except (PlatformError, OSError):
                message = "Platform request failed"
            finally:
                error.close()
            raise PlatformError(message, status=error.code) from error
        except (URLError, TimeoutError, OSError) as error:
            raise PlatformError("Platform connection failed") from error

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise ValueError("Invalid platform identifier")
        return value

    def _operation(self, session_id, operation, payload=None):
        path = "/v1/sessions/" + self._identifier(session_id) + "/" + operation
        return self._request("POST", path, {} if payload is None else payload)

    def snapshot(self):
        return self._request("GET", "/v1/status")

    def robot_status(self, robot_id):
        return self._request("GET", "/v1/robots/" + self._identifier(robot_id) + "/health")

    def episode_status(self, episode_id):
        return self._request("GET", "/v1/episodes/" + self._identifier(episode_id))

    def open_session(self, controller_id, robot_id, workstation_id):
        return self._request("POST", "/v1/sessions", {
            "controller_id": controller_id,
            "robot_id": robot_id,
            "workstation_id": workstation_id,
        })

    def arm(self, session_id):
        return self._operation(session_id, "arm")

    def input(self, session_id, payload):
        return self._operation(session_id, "input", payload)

    def hold(self, session_id):
        return self._operation(session_id, "hold")

    def resume(self, session_id):
        return self._operation(session_id, "resume")

    def close(self, session_id):
        return self._operation(session_id, "close")

    def record_start(self, session_id, task):
        return self._operation(session_id, "record/start", {"task": task})

    def record_mark(self, session_id, label, details=None):
        body = {"label": label}
        if details is not None:
            body["details"] = details
        return self._operation(session_id, "record/mark", body)

    def record_stop(self, session_id, outcome="complete"):
        return self._operation(session_id, "record/stop", {"outcome": outcome})

    def play_action(self, session_id, action):
        return self._operation(session_id, "action", action)
