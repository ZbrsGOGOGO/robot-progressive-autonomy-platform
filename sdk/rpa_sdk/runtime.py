"""Controller/robot ownership, input freshness, and recorder orchestration."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import queue
import re
import threading
import time
from typing import Optional
from uuid import uuid4

from .contracts import action_preview, is_neutral, validate_frame


@dataclass
class Session:
    session_id: str
    controller_id: str
    robot_id: str
    workstation_id: str
    state: str = "assigned"
    episode_id: Optional[str] = None
    last_episode_id: Optional[str] = None
    last_sequence: int = -1
    last_capture_ns: Optional[int] = None
    last_input_ns: Optional[int] = None
    input_epoch: Optional[str] = None
    neutral_required: bool = True
    fault: Optional[str] = None
    record_state: str = "idle"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError("invalid device or workstation identifier")
    return value


class _RecordPump:
    """Drop bounded telemetry under pressure without blocking command dispatch."""

    def __init__(self, store, capacity):
        self.store = store
        self.queue = queue.Queue(maxsize=capacity)
        self.errors = {}
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._run, daemon=True, name="episode-events")
        self.worker.start()

    def _error(self, episode, reason):
        with self.lock:
            self.errors.setdefault(episode, reason)

    def submit(self, episode, stream, data, timestamp):
        try:
            self.queue.put_nowait((episode, stream, deepcopy(data), timestamp))
        except queue.Full:
            self._error(episode, "recorder_queue_full")

    def barrier(self):
        done = threading.Event()
        self.queue.put(done, timeout=5)
        if not done.wait(5):
            raise RuntimeError("recorder queue did not drain")

    def take_error(self, episode):
        with self.lock:
            return self.errors.pop(episode, None)

    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, threading.Event):
                    item.set()
                    continue
                episode, stream, data, timestamp = item
                try:
                    self.store.append(episode, stream, data, t_mono_ns=timestamp)
                except Exception as error:
                    self._error(episode, f"recorder_write_failed:{type(error).__name__}")
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.put(None, timeout=5)
        self.worker.join(timeout=5)


class Runtime:
    def __init__(
        self, store=None, *, input_timeout_ms=350, clock_ns=time.monotonic_ns,
        record_capacity=2048, allow_physical_motion=False,
    ):
        if type(input_timeout_ms) is not int or not 20 <= input_timeout_ms <= 5000:
            raise ValueError("input_timeout_ms must be in [20, 5000]")
        if type(record_capacity) is not int or record_capacity < 1:
            raise ValueError("record_capacity must be positive")
        self._lock = threading.RLock()
        self._record_lock = threading.Lock()
        self._clock_ns = clock_ns
        self._timeout_ns = input_timeout_ms * 1_000_000
        self._allow_physical_motion = allow_physical_motion is True
        self._controllers = {}
        self._robots = {}
        self._sessions = {}
        self._store = store
        self._record_pump = _RecordPump(store, record_capacity) if store is not None else None
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("platform is shutting down")

    def register_controller(self, controller_id, kind, capabilities=None):
        controller_id = _identifier(controller_id)
        _identifier(kind)
        if capabilities is not None and not isinstance(capabilities, dict):
            raise ValueError("controller capabilities must be an object")
        with self._lock:
            self._ensure_open()
            if controller_id in self._controllers:
                raise ValueError("controller already registered")
            info = {"controller_id": controller_id, "kind": kind, "capabilities": deepcopy(capabilities or {})}
            self._controllers[controller_id] = info
            return deepcopy(info)

    def register_robot(self, adapter):
        robot_id = _identifier(adapter.robot_id)
        _identifier(adapter.robot_type)
        with self._lock:
            self._ensure_open()
            if robot_id in self._robots:
                raise ValueError("robot already registered")
            self._robots[robot_id] = adapter
            return self._robot_info(adapter)

    @staticmethod
    def _robot_info(adapter):
        return {
            "robot_id": adapter.robot_id, "robot_type": adapter.robot_type,
            "capabilities": deepcopy(adapter.capabilities()),
        }

    def snapshot(self):
        with self._lock:
            return {
                "now_ns": self._clock_ns(),
                "controllers": deepcopy(list(self._controllers.values())),
                "robots": [self._robot_info(a) for a in self._robots.values()],
                "sessions": [asdict(s) for s in self._sessions.values()],
            }

    def robot_status(self, robot_id):
        with self._lock:
            adapter = self._robots[robot_id]
            return {**self._robot_info(adapter), "readiness": adapter.readiness(), "status": adapter.snapshot()}

    def episode_status(self, episode_id):
        if self._store is None:
            raise RuntimeError("episode store not configured")
        return self._store.status(episode_id)

    def open_session(self, controller_id, robot_id, workstation_id):
        for value in (controller_id, robot_id, workstation_id):
            _identifier(value)
        with self._lock:
            self._ensure_open()
            if controller_id not in self._controllers or robot_id not in self._robots:
                raise KeyError("unknown controller or robot")
            caps = self._robots[robot_id].capabilities()
            if caps.get("motion") is not True:
                raise RuntimeError("robot adapter has no motion capability")
            if caps.get("simulation") is not True and (
                not self._allow_physical_motion or caps.get("device_local_safety") is not True
            ):
                raise RuntimeError("physical dispatch requires an explicitly configured device-local safety adapter")
            for session in self._sessions.values():
                if session.state != "released" and (
                    session.robot_id == robot_id or session.controller_id == controller_id
                    or session.workstation_id == workstation_id
                ):
                    raise RuntimeError("controller, robot or workstation is in use")
            session = Session(uuid4().hex, controller_id, robot_id, workstation_id)
            self._sessions[session.session_id] = session
            return asdict(session)

    def _hold_locked(self, session, reason=None):
        session.input_epoch = None
        try:
            result = self._robots[session.robot_id].hold()
            if not isinstance(result, dict) or result.get("confirmed") is not True:
                raise RuntimeError("hold not confirmed by adapter")
        except Exception as error:
            session.state = "fault"
            session.fault = f"hold_unconfirmed:{type(error).__name__}"
            raise RuntimeError("robot hold was not confirmed; ownership retained") from error
        session.state = "hold"
        session.fault = reason
        session.neutral_required = True

    def _enable(self, session_id, expected):
        with self._lock:
            self._ensure_open()
            session = self._sessions[session_id]
            if session.state != expected:
                raise RuntimeError(f"expected session state {expected}")
            adapter = self._robots[session.robot_id]
            if adapter.readiness().get("ready") is not True:
                raise RuntimeError("robot adapter is not ready")
            self._hold_locked(session)
            session.state = "armed"
            session.input_epoch = uuid4().hex
            session.last_input_ns = self._clock_ns()
            return asdict(session)

    def arm(self, session_id):
        return self._enable(session_id, "assigned")

    def resume(self, session_id):
        return self._enable(session_id, "hold")

    def input(self, session_id, payload):
        frame = validate_frame(payload)
        with self._lock:
            self._ensure_open()
            session = self._sessions[session_id]
            if session.state not in ("armed", "live"):
                raise RuntimeError("session is not armed")
            now = self._clock_ns()
            if now - session.last_input_ns >= self._timeout_ns:
                self._hold_locked(session, "input_expired")
                raise RuntimeError("input expired; explicit resume required")
            if frame["sequence"] <= session.last_sequence:
                raise ValueError("input sequence did not advance")
            adapter = self._robots[session.robot_id]
            epoch = frame["input_epoch"]
            requires_epoch = epoch is not None or adapter.capabilities().get("simulation") is not True
            if requires_epoch and epoch != session.input_epoch:
                raise ValueError("input epoch does not match current authorization")
            capture = frame["capture_time_ns"]
            if session.last_capture_ns is not None and (capture is None or capture <= session.last_capture_ns):
                raise ValueError("capture time did not advance")
            if not frame["tracking"]:
                self._hold_locked(session, "tracking_lost")
                raise RuntimeError("tracking lost; explicit resume required")
            if session.neutral_required and not is_neutral(frame):
                raise ValueError("first frame after arm/resume must be neutral")
            try:
                receipt = adapter.apply(frame)
                if not isinstance(receipt, dict) or receipt.get("accepted") is False:
                    raise RuntimeError("adapter did not accept input")
            except Exception as error:
                self._hold_locked(session, "apply_failed")
                session.state = "fault"
                raise RuntimeError("robot rejected dispatch") from error
            session.state = "live"
            session.neutral_required = False
            session.last_input_ns = now
            session.last_sequence = frame["sequence"]
            session.last_capture_ns = capture
            if session.episode_id and self._record_pump:
                self._record_pump.submit(session.episode_id, "intended", frame, now)
                for stream in ("commanded", "applied", "measured", "images"):
                    if isinstance(receipt.get(stream), dict):
                        self._record_pump.submit(session.episode_id, stream, receipt[stream], now)
            return {"session": asdict(session), "receipt": deepcopy(receipt)}

    def hold(self, session_id):
        with self._lock:
            session = self._sessions[session_id]
            if session.state in ("released", "closing"):
                raise RuntimeError("session is closing or released")
            self._hold_locked(session)
            return asdict(session)

    def tick(self):
        results = []
        with self._lock:
            now = self._clock_ns()
            for session in self._sessions.values():
                if session.state in ("armed", "live") and now - session.last_input_ns >= self._timeout_ns:
                    try:
                        self._hold_locked(session, "input_expired")
                    except RuntimeError:
                        pass  # Fault state retains ownership until an explicit confirmed hold.
                    results.append(asdict(session))
        return results

    def close(self, session_id):
        with self._lock:
            session = self._sessions[session_id]
            if session.state == "released":
                return asdict(session)
            if session.state == "closing":
                raise RuntimeError("session is already closing")
            self._hold_locked(session)
            session.state = "closing"
        record_fault = None
        try:
            self._stop_recording(session_id, "aborted", missing_ok=True)
        except Exception as error:
            record_fault = f"record_finalize_failed:{type(error).__name__}"
        with self._lock:
            try:
                result = self._robots[session.robot_id].release()
                if not isinstance(result, dict) or result.get("confirmed") is not True:
                    raise RuntimeError("release not confirmed")
            except Exception as error:
                session.state = "fault"
                session.fault = "release_unconfirmed"
                raise RuntimeError("robot release was not confirmed; ownership retained") from error
            session.state = "released"
            session.fault = record_fault
            return asdict(session)

    def record_start(self, session_id, task):
        if self._store is None:
            raise RuntimeError("episode store not configured")
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 200:
            raise ValueError("task must contain 1–200 characters")
        with self._record_lock:
            with self._lock:
                self._ensure_open()
                s = self._sessions[session_id]
                if s.state not in ("armed", "live", "hold") or s.episode_id or s.record_state != "idle":
                    raise RuntimeError("session cannot start recording")
                s.record_state = "starting"
                meta = {"robot_id": s.robot_id, "controller_id": s.controller_id, "workstation_id": s.workstation_id}
            try:
                result = self._store.start(session_id, task.strip(), metadata=meta)
            except Exception:
                with self._lock:
                    s.record_state = "idle"
                raise
            with self._lock:
                s.episode_id = result["episode_id"]
                s.last_episode_id = s.episode_id
                s.record_state = "recording"
            return result

    def record_mark(self, session_id, label, details=None):
        with self._record_lock:
            with self._lock:
                s = self._sessions[session_id]
                if s.record_state != "recording" or s.state in ("closing", "released"):
                    raise RuntimeError("no active recording")
                episode = s.episode_id
            return self._store.mark(episode, label, details)

    def record_stop(self, session_id, outcome="complete"):
        return self._stop_recording(session_id, outcome)

    def _stop_recording(self, session_id, outcome, missing_ok=False):
        if outcome not in ("complete", "aborted", "failed"):
            raise ValueError("outcome must be complete, aborted or failed")
        with self._record_lock:
            with self._lock:
                s = self._sessions[session_id]
                episode = s.episode_id
                if episode is None:
                    if missing_ok:
                        return {}
                    raise RuntimeError("no active recording")
                s.episode_id = None  # No new hot-path events can enqueue for this episode.
                s.record_state = "finalizing"
            try:
                self._record_pump.barrier()
                failure = self._record_pump.take_error(episode)
                if failure is not None:
                    self._store.mark(episode, failure)
                    result = self._store.stop(episode, "failed", capture_error=failure)
                else:
                    result = self._store.stop(episode, outcome)
            except Exception:
                with self._lock:
                    s.record_state = "failed"
                abandon = getattr(self._store, "abandon", None)
                if callable(abandon):
                    abandon(episode)
                raise
            with self._lock:
                s.record_state = "idle"
            return result

    def play_action(self, session_id, action):
        frames = action_preview(action)
        with self._lock:
            self._ensure_open()
            s = self._sessions[session_id]
            if s.state not in ("assigned", "hold"):
                raise RuntimeError("action preview requires assigned or held session")
            adapter = self._robots[s.robot_id]
            validate = getattr(adapter, "validate_action", None)
            checked = validate(frames) if callable(validate) else {"adapter_checked": False}
            if action.get("mode", "preview") == "preview":
                return {"mode": "preview", "frames": len(frames), "duration_s": frames[-1]["t_s"], **checked}
            if s.state != "hold" or adapter.capabilities().get("simulation") is not True:
                raise RuntimeError("action execution is available only in held simulated sessions")
            execute = getattr(adapter, "play_action", None)
            if not callable(execute):
                raise RuntimeError("adapter does not implement action execution")
            return execute(frames)

    def shutdown(self):
        with self._lock:
            self._closed = True
            active = [s.session_id for s in self._sessions.values() if s.state != "released"]
        faults = {}
        for session_id in active:
            try:
                self.close(session_id)
            except RuntimeError as error:
                faults[session_id] = str(error)
        if self._record_pump:
            self._record_pump.close()
        return faults
