"""Shared controller wire format and robot integration contract."""

from math import sqrt
import re
from typing import Protocol


class RobotAdapter(Protocol):
    """Device-local boundary. All operations have bounded execution time.

    A hold or release is confirmed only after the adapter verifies the
    corresponding device state. An unconfirmed stop retains ownership.
    """

    robot_id: str
    robot_type: str

    def capabilities(self) -> dict: ...
    def readiness(self) -> dict: ...
    def apply(self, frame: dict) -> dict: ...
    def hold(self) -> dict: ...
    def release(self) -> dict: ...
    def snapshot(self) -> dict: ...


def _numbers(values, length, lower, upper, field):
    if not isinstance(values, (list, tuple)) or len(values) != length:
        raise ValueError(f"{field} must contain {length} numbers")
    for value in values:
        if type(value) not in (int, float) or not lower <= value <= upper:
            raise ValueError(f"invalid value in {field}")
    return [float(value) for value in values]


def validate_frame(payload):
    if not isinstance(payload, dict):
        raise ValueError("input frame must be an object")
    if payload.get("schema", "rpa.input.v1") != "rpa.input.v1":
        raise ValueError("unsupported input schema")
    sequence = payload.get("sequence")
    if type(sequence) is not int or not 0 <= sequence < 2**63:
        raise ValueError("sequence must be a non-negative int64")
    capture = payload.get("capture_time_ns")
    if capture is not None and (type(capture) is not int or not 0 <= capture < 2**63):
        raise ValueError("capture_time_ns must be a non-negative int64")
    epoch = payload.get("input_epoch")
    if epoch is not None and (not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", epoch)):
        raise ValueError("invalid input_epoch")
    tracking = payload.get("tracking")
    if type(tracking) is not bool:
        raise ValueError("tracking must be a boolean")
    grip = payload.get("grip", [False, False])
    if not isinstance(grip, (list, tuple)) or len(grip) != 2 or any(type(v) is not bool for v in grip):
        raise ValueError("grip must contain two booleans")
    buttons = payload.get("buttons", {})
    if not isinstance(buttons, dict) or len(buttons) > 32 or any(
        not isinstance(key, str) or not 1 <= len(key) <= 48 or type(value) is not bool
        for key, value in buttons.items()
    ):
        raise ValueError("invalid buttons")
    poses = payload.get("poses", {})
    if not isinstance(poses, dict) or len(poses) > 16:
        raise ValueError("invalid poses")
    clean_poses = {}
    for name, pose in poses.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 48 or not isinstance(pose, dict):
            raise ValueError("invalid pose role")
        position = _numbers(pose.get("position"), 3, -1e3, 1e3, "pose.position in meters")
        rotation = _numbers(pose.get("rotation"), 4, -1.001, 1.001, "pose.rotation xyzw")
        norm = sqrt(sum(v*v for v in rotation))
        if not 0.99 <= norm <= 1.01:
            raise ValueError("pose rotation must be normalized")
        clean_poses[name] = {"position": position, "rotation": [v / norm for v in rotation]}
    return {
        "schema": "rpa.input.v1", "sequence": sequence, "capture_time_ns": capture, "input_epoch": epoch,
        "tracking": tracking, "poses": clean_poses, "grip": list(grip), "buttons": dict(buttons),
        "trigger": _numbers(payload.get("trigger", [0, 0]), 2, 0, 1, "trigger"),
        "sticks": _numbers(payload.get("sticks", [0, 0, 0, 0]), 4, -1, 1, "sticks"),
    }


def is_neutral(frame):
    return (
        not any(frame["grip"]) and not any(frame["buttons"].values())
        and all(abs(value) <= 0.05 for value in frame["trigger"] + frame["sticks"])
    )


def action_preview(action):
    if not isinstance(action, dict) or action.get("mode", "preview") not in ("preview", "execute"):
        raise ValueError("action mode must be preview or execute")
    frames = action.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= 200:
        raise ValueError("action must have 1–200 frames")
    clean = []
    width = None
    previous_t = -1.0
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("action frame must be an object")
        target = frame.get("joint_positions_rad")
        if not isinstance(target, list) or not 1 <= len(target) <= 64:
            raise ValueError("invalid joint_positions_rad")
        if width is None:
            width = len(target)
        target = _numbers(target, width, -20, 20, "joint_positions_rad")
        t = frame.get("t_s")
        if type(t) not in (float, int) or not 0 <= t <= 120 or t <= previous_t:
            raise ValueError("action time must increase and be between 0 and 120 seconds")
        clean.append({"t_s": float(t), "joint_positions_rad": target})
        previous_t = t
    return clean
