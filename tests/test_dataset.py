import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from rpa_sdk.dataset import EpisodeStore, clean_episode, export_dataset, import_savvy2, verify_episode


def _json(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _manifest(root):
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    return "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
        for path in paths
    )


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = EpisodeStore(self.root / "raw")

    def _episode(self, streams=("intended", "commanded", "applied", "measured"), *, clock=True):
        episode = self.store.start("session-test", "pick", {"clock_verified": clock})["episode_id"]
        for item in streams:
            self.store.append(episode, item, {"q_rad": [1]}, 0)
            self.store.append(episode, item, {"q_rad": [2]}, 33_333_333)
        self.store.stop(episode)
        return self.store.root / episode

    def test_capture_commit_and_idempotent_stop(self):
        metadata = {"robot_type": "example"}
        episode = self.store.start("s1", "practice", metadata)["episode_id"]
        metadata["robot_type"] = "mutation"
        active = self.store.root / (episode + ".inprogress")
        self.assertTrue(active.is_dir())
        with self.assertRaisesRegex(ValueError, "committed"):
            verify_episode(active)
        self.store.append(episode, "intended", {"seq": 1}, 2)
        self.store.mark(episode, "approach", {"phase": 1})
        commit = self.store.stop(episode)
        self.assertEqual(commit, self.store.stop(episode))
        self.assertEqual(commit["metadata"], {"robot_type": "example"})
        self.assertEqual(commit["streams"], {"intended": 1, "marks": 1})
        self.assertFalse(active.exists())
        self.assertEqual(verify_episode(self.store.root / episode)["checksums"], 3)

    def test_reject_nonfinite_invalid_timestamp_and_backwards_per_stream(self):
        episode = self.store.start("s1", "practice")["episode_id"]
        self.store.append(episode, "intended", {"seq": 1}, 100)
        self.store.append(episode, "measured", {"seq": 1}, 80)
        with self.assertRaisesRegex(ValueError, "regressed"):
            self.store.append(episode, "intended", {"seq": 2}, 99)
        with self.assertRaises(ValueError):
            self.store.append(episode, "applied", {"q": math.nan}, 110)
        with self.assertRaises(ValueError):
            self.store.append(episode, "applied", {"q": 1}, True)
        with self.assertRaises(ValueError):
            self.store.append(episode, "../unsafe", {"q": 1}, 110)
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            self.store.append(episode, "applied", {"q": circular}, 110)
        self.assertEqual(self.store.stop(episode)["event_count"], 2)

    def test_causal_alignment_has_no_future_fill_and_expires_previous_value(self):
        episode = self.store.start("s1", "practice", {"required_streams": ["intended", "applied"]})["episode_id"]
        self.store.append(episode, "intended", {"seq": 1}, 0)
        self.store.append(episode, "applied", {"q": 2}, 40_000_000)
        self.store.append(episode, "intended", {"seq": 3}, 100_000_000)
        self.store.stop(episode)
        raw = self.store.root / episode
        old = (raw / "events.jsonl").read_bytes()
        destination = self.root / "aligned"
        result = clean_episode(raw, destination, max_age_ms=35)
        frames = [json.loads(line) for line in (destination / "frames.jsonl").read_text().splitlines()]
        self.assertEqual([row["t_mono_ns"] for row in frames], [0, 33_333_333, 66_666_666, 100_000_000])
        self.assertIsNone(frames[1]["samples"]["applied"]["data"])
        self.assertEqual(frames[2]["samples"]["applied"]["data"], {"q": 2})
        self.assertIsNone(frames[2]["samples"]["intended"]["data"])
        self.assertFalse(frames[3]["samples"]["applied"]["valid"])
        self.assertEqual((raw / "events.jsonl").read_bytes(), old)
        self.assertEqual(result["quality"]["grade"], "C")
        with self.assertRaises(FileExistsError):
            clean_episode(raw, destination)

    def test_media_is_verified_and_copied_to_derived_episode(self):
        episode = self.store.start(
            "s1", "camera", {"clock_verified": True, "required_streams": ["images.head"]}
        )["episode_id"]
        image = b"\xff\xd8test-image\xff\xd9"
        self.store.append_image(episode, "head", image, 100)
        self.store.stop(episode)
        raw = self.store.root / episode
        clean = self.root / "clean"
        report = clean_episode(raw, clean)
        self.assertEqual(report["quality"]["grade"], "A")
        self.assertEqual((clean / "images/head/00000000.jpg").read_bytes(), image)
        (raw / "images/head/00000000.jpg").write_bytes(image+b"change")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            verify_episode(raw)

    def test_invalid_hashed_path_and_unlisted_file_are_rejected(self):
        source = self._episode()
        source.joinpath("extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unverified episode file"):
            verify_episode(source)
        with (source / "checksums.sha256").open("a", encoding="utf-8") as file:
            file.write("0" * 64 + "  ../other.json\n")
        with self.assertRaisesRegex(ValueError, "asset path"):
            verify_episode(source)

    def test_clean_output_cannot_mutate_raw(self):
        source = self._episode()
        with self.assertRaisesRegex(ValueError, "inside the source episode"):
            clean_episode(source, source / "forbidden")
        self.assertFalse((source / "forbidden.inprogress").exists())
        output = clean_episode(source, self.root / "aligned")
        self.assertEqual(output["quality"]["frame_count"], 2)
        self.assertEqual(output["quality"]["grade"], "A")

    def test_export_splits_whole_episodes_deterministically_and_gates_grade_b(self):
        first = self._episode()
        second = self._episode(clock=False)
        with self.assertRaisesRegex(ValueError, "grade B"):
            export_dataset([first, second], self.root / "rejected")
        self.assertFalse((self.root / "rejected").exists())
        a = export_dataset([first, second], self.root / "dataset-a", allow_b=True)
        b = export_dataset([second, first], self.root / "dataset-b", allow_b=True)
        self.assertEqual(a, b)
        self.assertEqual(len(a["episodes"]), 2)
        for row in a["episodes"]:
            self.assertTrue((self.root / "dataset-a" / row["path"] / "frames.jsonl").is_file())

    def test_abandon_retains_incomplete_data(self):
        episode = self.store.start("s1", "practice")["episode_id"]
        self.store.append(episode, "intended", {"sample": 1})
        self.store.abandon(episode)
        self.assertEqual(self.store.status(episode)["status"], "incomplete")
        self.assertTrue((self.store.root / (episode + ".inprogress") / "events.jsonl").exists())
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.store.stop(episode)

    def test_capture_fault_is_distinct_from_demo_outcome(self):
        metadata = {"clock_verified": True, "required_streams": ["measured"]}
        failed = self.store.start("s1", "practice", metadata)["episode_id"]
        self.store.append(failed, "measured", {"q_rad": [0]})
        self.store.stop(failed, "failed", capture_error="recorder_queue_full")
        derived = clean_episode(self.store.root / failed, self.root / "capture_failed")
        self.assertEqual(derived["quality"]["grade"], "C")
        demo = self.store.start("s1", "practice", metadata)["episode_id"]
        self.store.append(demo, "measured", {"q_rad": [0]})
        self.store.stop(demo, "failed")
        derived = clean_episode(self.store.root / demo, self.root / "demo_failed")
        self.assertEqual(derived["quality"]["grade"], "A")

    def test_import_savvy2_native_events_and_media(self):
        legacy = self.root / "legacy"
        image = legacy / "images/head/00000001.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\xff\xd8example\xff\xd9")
        _json(legacy / "episode_meta.json", {
            "schema_version": "savvy2.episode.meta.v1", "episode_id": "old",
            "session_id": "s1", "task_name": "pick", "result": "success",
            "required": {"arms": ["arm_a"], "cameras": ["head"]},
        })
        _json(legacy / "quality_manifest.json", {"grade": "B"})
        _jsonl(legacy / "events.jsonl", [
            {"event_type": "pico_intent", "stream": None, "mono_ns": 1, "payload": {}},
            {"event_type": "arm_applied", "stream": "arm_a", "mono_ns": 4, "payload": {"q_target_deg": [90] * 7}},
            {"event_type": "arm_commanded", "stream": "arm_a", "mono_ns": 3, "payload": {"v": [80] * 7}},
            {"event_type": "arm_state", "stream": "arm_a", "mono_ns": 2, "payload": {"q_deg": [0] * 7}},
        ])
        _jsonl(legacy / "frames.jsonl", [{
            "schema_version": "savvy2.episode.frame.v1", "frame_mono_ns": 5,
            "images": {"head": "images/head/00000001.jpg"},
            "capture_ts": {"image.head": 2}, "recv_ts": {"image.head": 3},
        }])
        (legacy / "checksums.sha256").write_text(_manifest(legacy), encoding="utf-8")
        imported = import_savvy2(legacy, self.store.root)
        path = self.store.root / imported["episode_id"]
        self.assertTrue(verify_episode(path)["ok"])
        self.assertEqual(imported["metadata"]["source_quality_grade"], "B")
        self.assertEqual(imported["streams"]["images.head"], 1)
        rows = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
        applied = next(row for row in rows if row["stream"] == "applied.arm.arm_a")
        self.assertEqual(applied["data"]["q_rad"], [math.pi / 2] * 7)
        self.assertTrue(image.exists())


if __name__ == "__main__":
    unittest.main()
