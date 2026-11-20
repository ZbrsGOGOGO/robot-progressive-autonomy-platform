import threading
import tempfile
import unittest
from pathlib import Path

from rpa_sdk.client import PlatformClient
from rpa_sdk.dataset import EpisodeStore, clean_episode, verify_episode
from rpa_sdk.http_api import make_server
from rpa_sdk.runtime import Runtime
from rpa_sdk.sim import SimRobot


def sample(sequence, **fields):
    return {"sequence": sequence, "tracking": True, "capture_time_ns": sequence, **fields}


class Clock:
    def __init__(self):
        self.now = 10**9

    def __call__(self):
        return self.now


class RefusesHold(SimRobot):
    refuses = False

    def hold(self):
        if self.refuses:
            return {"confirmed": False}
        return super().hold()


class PhysicalContractFixture(SimRobot):
    def capabilities(self):
        return {**super().capabilities(), "simulation": False, "device_local_safety": True}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.runtime = Runtime(clock_ns=self.clock)
        self.arm = RefusesHold("arm")
        self.runtime.register_controller("pico", "xr")
        self.runtime.register_controller("web", "web")
        self.runtime.register_robot(self.arm)
        self.runtime.register_robot(SimRobot("base", "sim-mobile"))
        self.session = self.runtime.open_session("pico", "arm", "one")["session_id"]
        self.addCleanup(self.runtime.shutdown)

    def test_exclusive_resources_and_release(self):
        for combo in (("web", "arm", "two"), ("pico", "base", "two"), ("web", "base", "one")):
            with self.assertRaises(RuntimeError):
                self.runtime.open_session(*combo)
        self.assertEqual(self.runtime.close(self.session)["state"], "released")
        self.assertEqual(self.runtime.open_session("web", "arm", "one")["state"], "assigned")

    def test_duplicate_and_malformed_frames_do_not_extend_watchdog(self):
        self.runtime.arm(self.session)
        self.runtime.input(self.session, sample(10))
        self.clock.now += 340_000_000
        with self.assertRaises(ValueError):
            self.runtime.input(self.session, sample(10))
        with self.assertRaises(ValueError):
            self.runtime.input(self.session, sample(11, trigger=[float("nan"), 0]))
        self.clock.now += 10_000_000
        changed, = self.runtime.tick()
        self.assertEqual(changed["state"], "hold")
        self.assertEqual(changed["fault"], "input_expired")
        with self.assertRaises(RuntimeError):
            self.runtime.input(self.session, sample(11))

    def test_lost_tracking_and_neutral_resume(self):
        self.runtime.arm(self.session)
        with self.assertRaises(ValueError):
            self.runtime.input(self.session, sample(0, grip=[True, False]))
        self.runtime.input(self.session, sample(1))
        with self.assertRaisesRegex(RuntimeError, "tracking lost"):
            self.runtime.input(self.session, sample(2, tracking=False))
        self.runtime.resume(self.session)
        with self.assertRaises(ValueError):
            self.runtime.input(self.session, sample(2, grip=[True, False]))
        with self.assertRaises(ValueError):
            self.runtime.input(self.session, sample(2, capture_time_ns=1))
        self.assertEqual(self.runtime.input(self.session, sample(3))["session"]["state"], "live")

    def test_fault_retains_owner_when_stop_is_unconfirmed(self):
        self.runtime.arm(self.session)
        self.arm.refuses = True
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            self.runtime.close(self.session)
        with self.assertRaises(RuntimeError):
            self.runtime.open_session("web", "arm", "another")
        self.assertEqual(self.runtime.snapshot()["sessions"][0]["state"], "fault")
        self.arm.refuses = False
        self.assertEqual(self.runtime.close(self.session)["state"], "released")

    def test_action_defaults_to_preview_and_requires_hold_to_execute(self):
        frames = [{"t_s": 0, "joint_positions_rad": [0.2] * 14}]
        self.assertEqual(self.runtime.play_action(self.session, {"frames": frames})["mode"], "preview")
        self.assertEqual(self.arm.joints, [0.0] * 14)
        with self.assertRaises(RuntimeError):
            self.runtime.play_action(self.session, {"mode": "execute", "frames": frames})
        self.runtime.hold(self.session)
        receipt = self.runtime.play_action(self.session, {"mode": "execute", "frames": frames})
        self.assertEqual(receipt["measured"]["joint_positions_rad"], [0.2] * 14)

    def test_resume_rejects_frame_from_previous_authorization(self):
        old = self.runtime.arm(self.session)["input_epoch"]
        self.runtime.input(self.session, sample(1, input_epoch=old))
        self.runtime.hold(self.session)
        new = self.runtime.resume(self.session)["input_epoch"]
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(ValueError, "epoch"):
            self.runtime.input(self.session, sample(2, input_epoch=old))
        self.runtime.input(self.session, sample(2, input_epoch=new))

    def test_physical_dispatch_is_opt_in_and_requires_epoch(self):
        self.runtime.register_robot(PhysicalContractFixture("external"))
        with self.assertRaisesRegex(RuntimeError, "physical dispatch"):
            self.runtime.open_session("web", "external", "two")
        runtime = Runtime(clock_ns=self.clock, allow_physical_motion=True)
        self.addCleanup(runtime.shutdown)
        runtime.register_controller("web", "web")
        runtime.register_robot(PhysicalContractFixture("external"))
        session = runtime.open_session("web", "external", "one")["session_id"]
        epoch = runtime.arm(session)["input_epoch"]
        with self.assertRaisesRegex(ValueError, "epoch"):
            runtime.input(session, sample(0))
        runtime.input(session, sample(0, input_epoch=epoch))


class FullWorkflowTest(unittest.TestCase):
    def test_http_to_committed_and_cleaned_episode(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = EpisodeStore(root / "raw")
            clock = Clock()
            runtime = Runtime(store, clock_ns=clock)
            runtime.register_controller("test", "python")
            runtime.register_robot(SimRobot("base", "sim-mobile"))
            with make_server(runtime, "127.0.0.1", 0, token="local-test-token-only") as server:
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                client = PlatformClient(f"http://127.0.0.1:{server.server_port}", token="local-test-token-only")
                try:
                    session = client.open_session("test", "base", "station")["session_id"]
                    client.arm(session)
                    episode = client.record_start(session, "base-demo")["episode_id"]
                    self.assertEqual(client.episode_status(episode)["status"], "recording")
                    client.record_mark(session, "prepare")
                    for sequence in range(5):
                        clock.now += 33_333_334
                        client.input(session, sample(sequence, grip=[sequence > 0, False],
                                                       sticks=[0, 0.25 if sequence else 0, 0, 0]))
                    client.hold(session)
                    self.assertEqual(client.robot_status("base")["status"]["base_velocity"], [0.0, 0.0])
                    self.assertEqual(client.record_stop(session)["status"], "committed")
                    self.assertEqual(client.episode_status(episode)["status"], "committed")
                    self.assertEqual(client.close(session)["state"], "released")
                    self.assertTrue(verify_episode(root / "raw" / episode))
                    clean_episode(root / "raw" / episode, root / "derived")
                    self.assertTrue((root / "derived" / "frames.jsonl").exists())
                finally:
                    server.shutdown()
                    server_thread.join(1)
                    runtime.shutdown()
