"""Append-only episode capture and offline dataset tools."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from typing import BinaryIO
from uuid import uuid4


SCHEMA = "rpa.episode.v1"
FRAME_SCHEMA = "rpa.frame.v1"
STREAMS = ("intended", "commanded", "applied", "measured", "images")
REQUIRED = STREAMS[:4]
_STREAM_NAME = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
_EPISODE_ID = re.compile(r"ep_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_LINE = 4 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_FRAMES = 1_000_000


def _string(value, name, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _stream(value):
    if not isinstance(value, str) or len(value) > 128 or not _STREAM_NAME.fullmatch(value):
        raise ValueError("invalid stream name")
    return value


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("monotonic timestamp must be a nonnegative int64 nanosecond value")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _encode(value):
    line = (json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(line) > MAX_LINE:
        raise ValueError("JSON record exceeds size limit")
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return line


def _invalid_constant(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        _invalid_constant(value)
    return number


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode(line):
    return json.loads(line, parse_constant=_invalid_constant, parse_float=_float, object_pairs_hook=_pairs)


def _file_object(path):
    with path.open("rb") as file:
        line = file.read(MAX_LINE + 1)
    if len(line) > MAX_LINE:
        raise ValueError(f"JSON exceeds size limit: {path.name}")
    return _object(_decode(line), path.name)


def _json_lines(path):
    with path.open("rb") as file:
        for number, line in enumerate(iter(lambda: file.readline(MAX_LINE + 1), b""), 1):
            if len(line) > MAX_LINE:
                raise ValueError(f"{path.name}:{number}: JSON record exceeds size limit")
            try:
                yield _object(_decode(line), "line")
            except (UnicodeError, ValueError) as error:
                raise ValueError(f"{path.name}:{number}: {error}") from error


def _flush(file):
    file.flush()
    os.fsync(file.fileno())


def _write(path, data):
    with path.open("xb") as file:
        file.write(data)
        _flush(file)


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _asset(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid relative asset path")
    path = PurePosixPath(relative)
    if path.is_absolute() or relative != path.as_posix() or relative == "." or ".." in path.parts:
        raise ValueError("asset path must stay inside the episode")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("episode assets must not be symbolic links")
    if not target.is_file():
        raise ValueError(f"missing episode file: {relative}")
    return target


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _checksums(root):
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symbolic link inside episode")
        if path.is_file() and path != root / "checksums.sha256":
            rows.append(f"{_sha(path)}  {path.relative_to(root).as_posix()}\n")
    _write(root / "checksums.sha256", "".join(rows).encode("utf-8"))


def _verify_checksums(root, required, allowed_unlisted=()):
    digests = {}
    with _asset(root, "checksums.sha256").open(encoding="utf-8") as file:
        for line in file:
            digest, delimiter, relative = line.rstrip("\r\n").partition("  ")
            if not delimiter or not _DIGEST.fullmatch(digest) or relative in digests or relative == "checksums.sha256":
                raise ValueError("invalid checksum manifest")
            if _sha(_asset(root, relative)) != digest:
                raise ValueError(f"checksum mismatch: {relative}")
            digests[relative] = digest
    if not set(required).issubset(digests):
        raise ValueError("checksum manifest omits mandatory files")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("symbolic link inside episode")
        relative = path.relative_to(root).as_posix()
        if path.is_file() and relative != "checksums.sha256" and relative not in digests and relative not in allowed_unlisted:
            raise ValueError(f"unverified episode file: {relative}")
    return digests


def _publish(work, destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    for directory in sorted((p for p in work.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        _sync_dir(directory)
    _sync_dir(work)
    work.rename(destination)
    _sync_dir(destination.parent)


@dataclass
class _Capture:
    directory: Path
    file: BinaryIO
    info: dict
    last: dict = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    failed: bool = False


class EpisodeStore:
    """Thread-safe filesystem recorder using a single local monotonic clock."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active = {}

    @staticmethod
    def _id(episode_id):
        if not isinstance(episode_id, str) or not _EPISODE_ID.fullmatch(episode_id):
            raise ValueError("invalid episode ID")
        return episode_id

    def _capture(self, episode_id):
        capture = self._active.get(self._id(episode_id))
        if capture is None:
            raise KeyError(f"episode is not active: {episode_id}")
        if capture.failed:
            raise RuntimeError("episode write failed; its temporary directory was retained")
        return capture

    def start(self, session_id, task, metadata=None):
        session_id = _string(session_id, "session ID")
        task = _string(task, "task")
        metadata = {} if metadata is None else _object(metadata, "metadata")
        episode_id = "ep_" + uuid4().hex
        info = {
            "schema": SCHEMA,
            "episode_id": episode_id,
            "session_id": session_id,
            "task": task,
            "started_wall_ns": time.time_ns(),
            "started_mono_ns": time.monotonic_ns(),
            "metadata": metadata,
        }
        payload = _encode(info)
        work = self.root / (episode_id + ".inprogress")
        with self._lock:
            work.mkdir()
            _write(work / "capture.json", payload)
            _sync_dir(work)
            _sync_dir(self.root)
            self._active[episode_id] = _Capture(work, (work / "events.jsonl").open("xb"), _decode(payload))
        return {"episode_id": episode_id, "status": "recording"}

    def append(self, episode_id, stream, data, t_mono_ns=None):
        stream = _stream(stream)
        data = _object(data, "event data")
        with self._lock:
            capture = self._capture(episode_id)
            stamp = time.monotonic_ns() if t_mono_ns is None else _stamp(t_mono_ns)
            self._append(capture, stream, data, stamp)

    def _append(self, capture, stream, data, stamp):
        if stamp < capture.last.get(stream, 0):
            raise ValueError(f"timestamp regressed on stream {stream}")
        if stream.split(".")[0] == "images" and "path" in data:
            relative = data["path"]
            if not isinstance(relative, str) or not relative.startswith("images/"):
                raise ValueError("image path must be relative to the episode images directory")
            _asset(capture.directory, relative)
        index = capture.counts[stream]
        event = {"stream": stream, "index": index, "t_mono_ns": stamp, "data": data}
        record = _encode(event)
        try:
            capture.file.write(record)
        except OSError:
            capture.failed = True
            raise
        capture.last[stream] = stamp
        capture.counts[stream] += 1
        return event

    def append_image(self, episode_id, role, image, t_mono_ns=None, *, encoding="jpeg", metadata=None):
        """Persist an encoded image and append its relative path to the role stream."""
        role = _stream(role)
        if encoding not in {"jpeg", "png"}:
            raise ValueError("image encoding must be jpeg or png")
        if not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE:
            raise ValueError("image must be nonempty bytes within the size limit")
        if encoding == "jpeg" and not (image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")):
            raise ValueError("invalid JPEG markers")
        if encoding == "png" and not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid PNG signature")
        details = {} if metadata is None else _object(metadata, "image metadata")
        stream = _stream(f"images.{role}")
        with self._lock:
            capture = self._capture(episode_id)
            stamp = time.monotonic_ns() if t_mono_ns is None else _stamp(t_mono_ns)
            if stamp < capture.last.get(stream, 0):
                raise ValueError(f"timestamp regressed on stream {stream}")
            suffix = "jpg" if encoding == "jpeg" else "png"
            relative = f"images/{role}/{capture.counts[stream]:08d}.{suffix}"
            data = {"role": role, "path": relative, "encoding": encoding, "metadata": details}
            _encode(data)
            target = capture.directory / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write(target, image)
                return self._append(capture, stream, data, stamp)
            except (OSError, ValueError, TypeError):
                capture.failed = True
                raise

    def mark(self, episode_id, label, details=None):
        label = _string(label, "mark label")
        details = {} if details is None else _object(details, "mark details")
        with self._lock:
            event = self._append(
                self._capture(episode_id), "marks", {"label": label, "details": details}, time.monotonic_ns()
            )
        return {"episode_id": episode_id, "label": label, "t_mono_ns": event["t_mono_ns"]}

    def status(self, episode_id):
        self._id(episode_id)
        with self._lock:
            capture = self._active.get(episode_id)
            if capture is not None:
                state = "write_failed" if capture.failed else "recording"
                return {"episode_id": episode_id, "status": state, "streams": dict(capture.counts)}
            if (self.root / episode_id).is_dir():
                return _file_object(self.root / episode_id / "manifest.json")
            if (self.root / (episode_id + ".inprogress")).is_dir():
                return {"episode_id": episode_id, "status": "incomplete"}
        raise KeyError(f"unknown episode: {episode_id}")

    def stop(self, episode_id, outcome="complete", *, capture_error=None):
        self._id(episode_id)
        outcome = _string(outcome, "outcome")
        if capture_error is not None:
            capture_error = _string(capture_error, "capture error")
        with self._lock:
            if episode_id not in self._active:
                status = self.status(episode_id)
                if status["status"] == "committed":
                    return status
                raise RuntimeError("incomplete episode cannot be committed")
            capture = self._active.pop(episode_id)
            try:
                if capture.failed:
                    raise RuntimeError("episode write failed; its temporary directory was retained")
                _flush(capture.file)
            finally:
                capture.file.close()
            manifest = {
                **capture.info,
                "status": "committed",
                "outcome": outcome,
                "stopped_wall_ns": time.time_ns(),
                "streams": dict(capture.counts),
                "event_count": sum(capture.counts.values()),
            }
            if capture_error is not None:
                manifest["capture_error"] = capture_error
            _write(capture.directory / "manifest.json", _encode(manifest))
            _checksums(capture.directory)
            _publish(capture.directory, self.root / episode_id)
        return manifest

    def abandon(self, episode_id):
        """Close a failed recorder without publishing or deleting its partial bytes."""
        with self._lock:
            capture = self._active.pop(self._id(episode_id), None)
            if capture is not None:
                capture.file.close()


def _committed_directory(path):
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir() or root.name.endswith(".inprogress"):
        raise ValueError("expected an already committed episode directory")
    return root


def _inspect_episode(path):
    root = _committed_directory(path)
    digests = _verify_checksums(root, {"capture.json", "manifest.json", "events.jsonl"})
    manifest = _file_object(root / "manifest.json")
    if manifest.get("schema") != SCHEMA or manifest.get("status") != "committed":
        raise ValueError("unsupported or incomplete episode manifest")
    EpisodeStore._id(manifest.get("episode_id"))
    _object(manifest.get("metadata"), "episode metadata")
    capture = _file_object(root / "capture.json")
    if any(manifest.get(key) != value for key, value in capture.items()):
        raise ValueError("capture metadata does not match the commit")
    streams = defaultdict(list)
    for event in _json_lines(root / "events.jsonl"):
        stream = _stream(event.get("stream"))
        stamp = _stamp(event.get("t_mono_ns"))
        data = _object(event.get("data"), "event data")
        preceding = streams[stream]
        if type(event.get("index")) is not int or event["index"] != len(preceding):
            raise ValueError(f"event index is not contiguous on stream {stream}")
        if preceding and stamp < preceding[-1]["t_mono_ns"]:
            raise ValueError(f"timestamp regressed on stream {stream}")
        if stream.split(".")[0] == "images" and "path" in data:
            if (not isinstance(data["path"], str) or not data["path"].startswith("images/")
                    or data["path"] not in digests):
                raise ValueError("image event does not reference a verified asset")
        preceding.append(event)
    counts = {stream: len(events) for stream, events in streams.items()}
    if manifest.get("streams") != counts or manifest.get("event_count") != sum(counts.values()):
        raise ValueError("event counts do not match the commit")
    return root, manifest, streams, digests


def verify_episode(path):
    """Validate byte integrity, metadata, ordering and media references."""
    _, manifest, _, digests = _inspect_episode(path)
    return {"ok": True, "episode_id": manifest["episode_id"], "manifest": manifest, "checksums": len(digests)}


def _required(manifest, required_streams):
    if required_streams is None:
        required_streams = manifest["metadata"].get("required_streams", REQUIRED)
    if not isinstance(required_streams, (list, tuple)) or not required_streams:
        raise ValueError("required streams must be a nonempty sequence")
    return tuple(dict.fromkeys(_stream(name) for name in required_streams))


def _alignment_options(hz, max_age_ms):
    if type(hz) is not int or not 1 <= hz <= 240:
        raise ValueError("hz must be an integer between 1 and 240")
    if type(max_age_ms) not in (int, float) or not 0 <= max_age_ms <= 60_000:
        raise ValueError("max_age_ms must be between 0 and 60000")
    return int(max_age_ms * 1_000_000)


def _timeline(streams, hz):
    series = [values for name, values in streams.items() if name.split(".")[0] in STREAMS and values]
    if not series:
        return 0, 0
    start = min(events[0]["t_mono_ns"] for events in series)
    end = max(events[-1]["t_mono_ns"] for events in series)
    frames = ((end - start + 1) * hz + 999_999_999) // 1_000_000_000
    if frames > MAX_FRAMES:
        raise ValueError("episode exceeds alignment frame limit; select a shorter capture")
    return start, frames


def _aligned_rows(streams, required, hz, max_age_ns, start, frames):
    names = sorted({name for name in streams if name.split(".")[0] in STREAMS} | {
        name for name in required
        if not any(actual.startswith(name + ".") for actual in streams)
    })
    cursors = {name: -1 for name in names}
    for index in range(frames):
        cut = start + (index * 1_000_000_000) // hz
        samples = {}
        for name in names:
            history = streams.get(name, ())
            previous = cursors[name]
            cursor = previous
            while cursor + 1 < len(history) and history[cursor + 1]["t_mono_ns"] <= cut:
                cursor += 1
            cursors[name] = cursor
            event = history[cursor] if cursor >= 0 else None
            age = None if event is None else cut - event["t_mono_ns"]
            valid = age is not None and age <= max_age_ns
            samples[name] = {
                "data": event["data"] if valid else None,
                "source_mono_ns": None if event is None else event["t_mono_ns"],
                "source_index": None if event is None else event["index"],
                "age_ms": None if age is None else age / 1_000_000,
                "valid": valid,
                "new": valid and cursor != previous,
            }
        yield {
            "schema": FRAME_SCHEMA,
            "frame_index": index,
            "t_mono_ns": cut,
            "timestamp_s": (cut - start) / 1_000_000_000,
            "samples": samples,
        }


def _quality(manifest, coverage, frame_count, streams):
    reasons = []
    grade = "A"
    if not frame_count:
        grade = "C"
        reasons.append("no_sample_frames")
    for name, ratio in sorted(coverage.items()):
        if ratio < 0.5:
            grade = "C"
            reasons.append(f"required_coverage_below_50_percent:{name}")
        elif ratio < 0.9:
            grade = max(grade, "B")
            reasons.append(f"required_coverage_below_90_percent:{name}")
    if manifest["metadata"].get("clock_verified") is not True:
        grade = max(grade, "B")
        reasons.append("hardware_capture_clock_unverified")
    if (
        manifest.get("capture_error") or manifest["metadata"].get("capture_error")
        or manifest["metadata"].get("source_quality_grade") == "C"
    ):
        grade = "C"
        reasons.append("capture_reported_unusable")
    elif manifest["metadata"].get("source_quality_grade") == "B":
        grade = max(grade, "B")
        reasons.append("source_quality_b")
    if any(
        event["data"].get("fault") not in (None, False, 0, "")
        for name, events in streams.items() if name.split(".")[0] == "measured"
        for event in events
    ):
        grade = "C"
        reasons.append("measured_fault")
    return {"grade": grade, "frame_count": frame_count, "coverage": coverage, "reasons": reasons}


def _new_destination(path):
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    parent = destination.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    return parent / destination.name


def _copy_verified_asset(source, destination, digest):
    calculated = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            calculated.update(block)
            writer.write(block)
        _flush(writer)
    if calculated.hexdigest() != digest:
        raise ValueError(f"episode asset changed during export: {source.name}")


def _derive(inspection, work, hz, max_age_ms, required_streams):
    root, source, streams, digests = inspection
    required = _required(source, required_streams)
    max_age_ns = _alignment_options(hz, max_age_ms)
    start, frames = _timeline(streams, hz)
    work.mkdir(exist_ok=False)
    counts = Counter()
    media = set()
    with (work / "frames.jsonl").open("xb") as file:
        for row in _aligned_rows(streams, required, hz, max_age_ns, start, frames):
            samples = row["samples"]
            for name in required:
                counts[name] += any(
                    entry["valid"] for stream, entry in samples.items()
                    if stream == name or stream.startswith(name + ".")
                )
            for name, sample in samples.items():
                if name.split(".")[0] == "images" and sample["valid"] and "path" in sample["data"]:
                    media.add(sample["data"]["path"])
            file.write(_encode(row))
        _flush(file)
    for relative in sorted(media):
        _copy_verified_asset(_asset(root, relative), work / relative, digests[relative])
    coverage = {name: counts[name] / frames if frames else 0.0 for name in required}
    quality = _quality(source, coverage, frames, streams)
    metadata = {
        "schema": "rpa.cleaned_episode.v1",
        "episode_id": source["episode_id"],
        "source_manifest_sha256": digests["manifest.json"],
        "task": source["task"],
        "outcome": source["outcome"],
        "alignment_hz": hz,
        "max_age_ms": max_age_ms,
        "required_streams": required,
        "marks": streams.get("marks", []),
        "quality": quality,
    }
    _write(work / "quality.json", _encode(quality))
    _write(work / "manifest.json", _encode(metadata))
    _checksums(work)
    return metadata


def clean_episode(source, destination, *, hz=30, max_age_ms=100, required_streams=None):
    """Verify raw data, then produce a separate causally aligned derived episode."""
    inspection = _inspect_episode(source)
    root = inspection[0]
    candidate = Path(destination).expanduser().resolve()
    if candidate == root or root in candidate.parents:
        raise ValueError("derived output must not be inside the source episode")
    destination = _new_destination(destination)
    work = destination.with_name(destination.name + ".inprogress")
    metadata = _derive(inspection, work, hz, max_age_ms, required_streams)
    _publish(work, destination)
    return metadata


def export_dataset(sources, destination, *, hz=30, max_age_ms=100, allow_b=False, seed="rpa-v1"):
    """Create an aligned dataset with deterministic whole-episode splits."""
    sources = list(sources)
    if not sources:
        raise ValueError("specify at least one source episode")
    seed = _string(seed, "split seed")
    inspections = [_inspect_episode(source) for source in sources]
    ids = [item[1]["episode_id"] for item in inspections]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate episode ID in export")
    candidate = Path(destination).expanduser().resolve()
    if any(item[0] == candidate or item[0] in candidate.parents for item in inspections):
        raise ValueError("dataset output must not be inside a source episode")
    destination = _new_destination(destination)
    work = destination.with_name(destination.name + ".inprogress")
    work.mkdir(exist_ok=False)
    rows = []
    for inspection in sorted(inspections, key=lambda item: item[1]["episode_id"]):
        episode_id = inspection[1]["episode_id"]
        selection = int(hashlib.sha256(f"{seed}:{episode_id}".encode()).hexdigest()[:8], 16) % 100
        split = "train" if selection < 80 else ("validation" if selection < 90 else "test")
        relative = f"{split}/{episode_id}"
        (work / split).mkdir(exist_ok=True)
        metadata = _derive(inspection, work / relative, hz, max_age_ms, None)
        grade = metadata["quality"]["grade"]
        if grade == "C" or (grade == "B" and not allow_b):
            raise ValueError(f"episode {episode_id} has grade {grade}; dataset was not published")
        rows.append({"episode_id": episode_id, "split": split, "grade": grade, "path": relative})
    manifest = {
        "schema": "rpa.dataset.v1",
        "seed": seed,
        "split_strategy": "sha256 episode ID buckets 80/10/10",
        "episodes": rows,
    }
    _write(work / "dataset.json", _encode(manifest))
    _checksums(work)
    _publish(work, destination)
    return manifest


_SAVVY2_EVENT_TYPES = {
    "arm_ik_target": ("intended", "arm"),
    "arm_commanded": ("commanded", "arm"),
    "arm_applied": ("applied", "arm"),
    "arm_state": ("measured", "arm"),
    "gripper_commanded": ("commanded", "gripper"),
    "gripper_applied": ("applied", "gripper"),
    "gripper_state": ("measured", "gripper"),
    "chassis_commanded": ("commanded", "chassis"),
    "chassis_applied": ("applied", "chassis"),
    "chassis_state": ("measured", "chassis"),
    "waist_commanded": ("commanded", "waist"),
    "waist_applied": ("applied", "waist"),
    "waist_state": ("measured", "waist"),
}


def _savvy2_event(event):
    kind = _string(event.get("event_type"), "Savvy2 event type", 96)
    stamp = _stamp(event.get("mono_ns"))
    if kind == "annotation":
        return stamp, "marks", {
            "label": _string(event.get("mark") or "unmarked", "mark label"),
            "details": {"savvy2": event},
        }
    payload = _object(event.get("payload", {}), "Savvy2 payload")
    role = event.get("stream")
    if kind == "pico_intent":
        return stamp, "intended.pico", {"payload": payload, "savvy2": event}
    if kind not in _SAVVY2_EVENT_TYPES:
        return stamp, "legacy.event", {"savvy2": event}
    category, component = _SAVVY2_EVENT_TYPES[kind]
    name = f"{category}.{component}"
    if role is not None:
        name += "." + _stream(role)
    normalized = {"payload": payload, "savvy2": event}
    if component == "arm":
        for key in ("q_target_deg", "q_deg", "joints_deg", "v"):
            values = payload.get(key)
            if (
                (key != "v" or kind == "arm_commanded")
                and isinstance(values, list) and len(values) >= 7
                and all(type(value) in (int, float) and -360_000 <= value <= 360_000 for value in values[:7])
            ):
                normalized["q_rad"] = [math.radians(value) for value in values[:7]]
                break
    if category == "measured":
        controller = payload.get("controller")
        error = payload.get("driver_error") or (controller.get("error") if isinstance(controller, dict) else None)
        if error not in (None, 0, "0", False, ""):
            normalized["fault"] = error
    return stamp, name, normalized


def _savvy2_required(meta):
    original = _object(meta.get("required") or {}, "Savvy2 required components")
    result = ["intended.pico"]
    for role in original.get("arms", ("arm_a", "arm_b")):
        role = _stream(role)
        result.extend(f"{category}.arm.{role}" for category in ("commanded", "applied", "measured"))
    for role in original.get("grippers", ()):
        role = _stream(role)
        result.extend(f"{category}.gripper.{role}" for category in ("commanded", "measured"))
        if original.get("gripper_applied", True):
            result.append(f"applied.gripper.{role}")
    for component in ("chassis", "waist"):
        if original.get(component):
            result.extend(f"{category}.{component}" for category in ("applied", "measured"))
    result.extend(f"images.{_stream(role)}" for role in original.get("cameras", ()))
    return result


def _savvy2_media(root, digests):
    seen = set()
    last_cut = -1
    for frame in _json_lines(root / "frames.jsonl"):
        if frame.get("schema_version") != "savvy2.episode.frame.v1":
            raise ValueError("unsupported Savvy2 frame schema")
        cut = _stamp(frame.get("frame_mono_ns"))
        if cut <= last_cut:
            raise ValueError("Savvy2 frame timestamps must increase")
        last_cut = cut
        for role, relative in _object(frame.get("images", {}), "Savvy2 images").items():
            role = _stream(role)
            if relative is None:
                continue
            if (not isinstance(relative, str) or not relative.startswith(f"images/{role}/")
                    or relative not in digests):
                raise ValueError("Savvy2 frame does not reference a verified role image")
            key = (role, relative)
            if key in seen:
                continue
            capture = _object(frame.get("capture_ts") or {}, "Savvy2 capture timestamps").get(f"image.{role}")
            receive = _object(frame.get("recv_ts") or {}, "Savvy2 receive timestamps").get(f"image.{role}")
            stamp = _stamp(capture if capture is not None else receive if receive is not None else cut)
            if stamp > cut:
                raise ValueError("Savvy2 frame references future media")
            seen.add(key)
            yield stamp, role, relative, {
                "legacy_path": relative,
                "capture_ns": capture,
                "receive_ns": receive,
                "first_frame_cut_ns": cut,
                "clock_reference": (
                    "savvy2_robot_encode" if capture is not None else
                    "savvy2_robot_receive" if receive is not None else "savvy2_frame_cut_proxy"
                ),
            }


def import_savvy2(source, destination_root):
    """Import a previously committed Savvy2 V1 episode, leaving it unchanged."""
    root = _committed_directory(source)
    destination = Path(destination_root).expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("import output must not be inside the original episode")
    required = {"episode_meta.json", "quality_manifest.json", "events.jsonl", "frames.jsonl"}
    digests = _verify_checksums(root, required, allowed_unlisted={"annotations.json"})
    original = _file_object(root / "episode_meta.json")
    quality = _file_object(root / "quality_manifest.json")
    if original.get("schema_version") != "savvy2.episode.meta.v1" or quality.get("grade") not in ("A", "B", "C"):
        raise ValueError("expected a Savvy2 V1 episode with a quality grade")
    events = sorted(
        (_savvy2_event(event) for event in _json_lines(root / "events.jsonl")),
        key=lambda row: row[0],
    )
    media = sorted(_savvy2_media(root, digests), key=lambda row: row[0])
    meta = {
        "source_format": "savvy2.episode.v1",
        "source_episode_id": original.get("episode_id") or original.get("ep_id"),
        "source_episode_meta_sha256": digests["episode_meta.json"],
        "source_quality_grade": quality["grade"],
        "clock_verified": False,
        "required_streams": _savvy2_required(original),
    }
    store = EpisodeStore(destination)
    episode_id = store.start(
        str(original.get("session_id") or "savvy2-import"),
        str(original.get("task_name") or "savvy2-recording"),
        meta,
    )["episode_id"]
    try:
        for stamp, stream, payload in events:
            store.append(episode_id, stream, payload, stamp)
        for stamp, role, relative, details in media:
            with _asset(root, relative).open("rb") as file:
                image = file.read(MAX_IMAGE + 1)
            if hashlib.sha256(image).hexdigest() != digests[relative]:
                raise ValueError("Savvy2 image changed during import")
            store.append_image(episode_id, role, image, stamp, metadata=details)
        return store.stop(episode_id, str(original.get("result") or "complete"))
    except Exception:
        store.abandon(episode_id)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify a committed SDK episode")
    verify.add_argument("episode")
    clean = commands.add_parser("clean", help="create a causal aligned copy and quality report")
    clean.add_argument("episode")
    clean.add_argument("output")
    clean.add_argument("--require-stream", action="append", dest="required_streams")
    export = commands.add_parser("export", help="export whole-episode train/validation/test splits")
    export.add_argument("output")
    export.add_argument("episodes", nargs="+")
    export.add_argument("--allow-b", action="store_true")
    export.add_argument("--seed", default="rpa-v1")
    for command in (clean, export):
        command.add_argument("--hz", type=int, default=30)
        command.add_argument("--max-age-ms", type=float, default=100)
    ingest = commands.add_parser("import-savvy2", help="import one committed Savvy2 V1 episode")
    ingest.add_argument("episode")
    ingest.add_argument("output_root")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_episode(args.episode)
        elif args.command == "clean":
            result = clean_episode(
                args.episode, args.output, hz=args.hz, max_age_ms=args.max_age_ms,
                required_streams=args.required_streams,
            )
        elif args.command == "export":
            result = export_dataset(
                args.episodes, args.output, hz=args.hz, max_age_ms=args.max_age_ms,
                allow_b=args.allow_b, seed=args.seed,
            )
        else:
            result = import_savvy2(args.episode, args.output_root)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        parser.exit(1, f"dataset: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
