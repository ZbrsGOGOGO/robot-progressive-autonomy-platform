"""Deterministic stand-in adapters. They never open device sockets."""

from copy import deepcopy


class SimRobot:
    """Small kinematic mock, not a dynamics engine or hardware safety model."""

    def __init__(self, robot_id, robot_type="sim-dual-arm"):
        if robot_type not in ("sim-dual-arm", "sim-mobile"):
            raise ValueError("unknown simulation profile")
        self.robot_id = robot_id
        self.robot_type = robot_type
        self.joints = [0.0] * (14 if robot_type == "sim-dual-arm" else 2)
        self.velocity = [0.0, 0.0]
        self.gripper = [0.0, 0.0]
        self.state = "hold"
        self.sequence = -1

    def capabilities(self):
        return {
            "simulation": True, "motion": True, "actions": True,
            "parts": ["left_arm", "right_arm", "grippers"] if len(self.joints) == 14 else ["base"],
            "joint_count": len(self.joints), "video": [],
        }

    def readiness(self):
        return {"ready": True, "simulation": True}

    def snapshot(self):
        return {
            "state": self.state, "last_sequence": self.sequence,
            "joint_positions_rad": list(self.joints), "base_velocity": list(self.velocity),
            "gripper_position": list(self.gripper), "faults": [],
        }

    @staticmethod
    def _clip(value, limit):
        return max(-limit, min(limit, value))

    def apply(self, frame):
        if type(frame.get("sequence")) is not int or frame.get("tracking") is not True:
            raise ValueError("simulated robot requires a validated, tracked frame")
        target = deepcopy(self.snapshot())
        if len(self.joints) == 2:
            target["base_velocity"] = [self._clip(frame["sticks"][1], 0.35),
                                       self._clip(frame["sticks"][0], 0.5)] if any(frame["grip"]) else [0.0, 0.0]
            self.velocity = target["base_velocity"]
            self.joints = [self._clip(q + v * 0.02, 2.0) for q, v in zip(self.joints, self.velocity)]
        else:
            for side, role in enumerate(("left", "right")):
                pose = frame["poses"].get(role)
                if frame["grip"][side] and pose:
                    for axis, value in enumerate(pose["position"]):
                        index = side * 7 + axis
                        target["joint_positions_rad"][index] = self._clip(value, 2.0)
                target["gripper_position"][side] = frame["trigger"][side]
            self.gripper = list(target["gripper_position"])
            self.joints = [
                old + self._clip(desired - old, 0.03)
                for old, desired in zip(self.joints, target["joint_positions_rad"])
            ]
        self.sequence = frame["sequence"]
        self.state = "live"
        result = self.snapshot()
        return {"commanded": target, "applied": deepcopy(result), "measured": deepcopy(result)}

    def hold(self):
        self.state = "hold"
        self.velocity = [0.0, 0.0]
        return {"confirmed": True, "state": self.state}

    def release(self):
        self.hold()
        self.state = "released"
        return {"confirmed": True, "state": self.state}

    def validate_action(self, frames):
        for frame in frames:
            target = frame["joint_positions_rad"]
            if len(target) != len(self.joints) or any(abs(value) > 2 for value in target):
                raise ValueError("action outside simulated joint envelope")
        return {"adapter_checked": True, "simulation": True}

    def play_action(self, frames):
        self.validate_action(frames)
        # This is an offline simulated end-state, not time-driven interpolation.
        self.joints = list(frames[-1]["joint_positions_rad"])
        self.hold()
        return {"mode": "execute", "simulation": True, "frames": len(frames), "measured": self.snapshot()}
