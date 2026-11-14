"""Bounded, durable job store for the separate synthetic reset worker only."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from levi.automatic.offline_harness import FRAME_ID, OBJECT_ID, SCENE_ID
from levi.automatic.offline_scenarios import SCENARIOS, scenario_catalog
from levi.automatic.offline_worker import atomic_json, fail_document, read_json, utc_now

TERMINAL = {"succeeded", "blocked", "failed", "stopped"}
RUN_ID = re.compile(r"reset_[0-9a-f]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
MAX_RUNS = 1000


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _xyz(value) -> bool:
    try:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 3
            and all(type(x) in (int, float) and math.isfinite(x) for x in value)
        )
    except OverflowError:
        return False


def _aperture(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _evidence(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"status", "reason", "evidence"}
        and value["status"] in {"pass", "fail", "unknown"}
        and isinstance(value["reason"], str)
        and isinstance(value["evidence"], dict)
    )


def _valid_success_proofs(value: dict) -> bool:
    """Check saved proof integrity without re-aging historical observations."""
    events = value["events"]
    holding = value["holding"]
    if holding["status"] == "pass":
        proof = holding["evidence"]
        if (
            any(
                proof.get(key) != expected
                for key, expected in {
                    "run_id": value["run_id"],
                    "scene_id": SCENE_ID,
                    "object_id": OBJECT_ID,
                    "frame_id": FRAME_ID,
                    "source": "synthetic_measurement",
                }.items()
            )
            or any(
                proof.get(key) is not True
                for key in ("object_follow", "post_lift", "gripper_closed")
            )
            or any(
                not _count(proof.get(key)) or proof[key] < 1
                for key in ("policy_epoch", "before_seq", "after_seq")
            )
            or proof["before_seq"] >= proof["after_seq"]
            or not _aperture(proof.get("gripper_measurement"))
            or proof["gripper_measurement"] > 0.3
            or not _xyz(proof.get("object_delta"))
            or not _xyz(proof.get("gripper_delta"))
            # Match verify_holding's measured lift/follow contract.
            or proof["gripper_delta"][2] < 0.03
            or math.dist(proof["object_delta"], proof["gripper_delta"]) > 0.005
        ):
            return False
        # The original v1 worker omitted both capture times. Keep those
        # histories intact, but never accept a partial or malformed time pair.
        times = ("before_captured_ms", "after_captured_ms")
        if any(key in proof for key in times) and (
            any(not _count(proof.get(key)) for key in times)
            or proof[times[0]] >= proof[times[1]]
        ):
            return False
        lifts = [
            event
            for event in events
            if event["stage"] == "lift_verify" and "holding" in event["evidence"]
        ]
        if not lifts or lifts[-1]["evidence"]["holding"] != holding:
            return False
        handoffs = [
            event
            for event in events
            if event["stage"] == "handoff" and event["status"] == "succeeded"
        ]
        if handoffs:
            handoff = handoffs[-1]
            evidence = handoff["evidence"]
            if (
                handoff["seq"] <= lifts[-1]["seq"]
                or type(evidence.get("policy_epoch")) is not int
                or evidence["policy_epoch"] != proof["policy_epoch"] + 1
                or value["handoff"]["policy_epoch"] != evidence["policy_epoch"]
                or not _count(evidence.get("observation_seq"))
                or evidence["observation_seq"] <= proof["after_seq"]
            ):
                return False
    final = value["final_observation"]
    if final is not None and final["status"] == "pass":
        proof = final["evidence"]
        checks = [
            event
            for event in events
            if event["stage"] == "final_verify" and event["status"] == "succeeded"
        ]
        if (
            not checks
            or checks[-1]["evidence"].get("final_observation") != final
            or value["target"] is None
            or any(
                proof.get(key) != expected
                for key, expected in {
                    "run_id": value["run_id"],
                    "scene_id": SCENE_ID,
                    "source": "synthetic_measurement",
                    "policy_epoch": value["handoff"]["policy_epoch"],
                    "target_position_xyz": value["target"]["position_xyz"],
                }.items()
            )
            or proof.get("object_at_target") is not True
            or proof.get("released") is not True
            or not _count(proof.get("observation_seq"))
            or proof["observation_seq"] < 1
            or not _aperture(proof.get("gripper_measurement"))
            or any(
                not _xyz(proof.get(key))
                for key in (
                    "target_position_xyz",
                    "measured_object_xyz",
                    "measured_gripper_xyz",
                )
            )
            # Match verify_final_reset's measured placement/release contract.
            or math.dist(proof["measured_object_xyz"], proof["target_position_xyz"])
            > 0.005
            or proof["gripper_measurement"] < 0.9
            or math.dist(proof["measured_object_xyz"], proof["measured_gripper_xyz"])
            < 0.03
        ):
            return False
        for stage in ("handoff", "release"):
            observations = [
                event
                for event in events
                if event["stage"] == stage and "observation_seq" in event["evidence"]
            ]
            if observations:
                previous = observations[-1]
                seq = previous["evidence"]["observation_seq"]
                if (
                    not _count(seq)
                    or seq < 1
                    or proof["observation_seq"] <= seq
                    or checks[-1]["seq"] <= previous["seq"]
                ):
                    return False
    return True


def _valid_document(value: dict, spec: dict) -> bool:
    """Reject malformed persisted rows before they can poison a whole catalog."""
    json.dumps(value, allow_nan=False)
    required = set(initial_document(spec))
    if set(value) != required or any(
        value[key] != expected
        for key, expected in {
            "schema": "levi.reset_harness.run.v1",
            "execution_mode": "offline",
            "adapter": "fake",
            "run_id": spec["run_id"],
            "scenario_id": spec["scenario_id"],
            "seed": spec["seed"],
        }.items()
    ):
        return False
    if (
        value["physical_execution"] is not False
        or value["status"] not in {"queued", "running", *TERMINAL}
        or not isinstance(value["stage"], str)
        or not value["stage"]
        or not _count(value["stage_seq"])
        or not _count(value["elapsed_ms"])
        or not _count(value["seed"])
        or not isinstance(value["updated_at"], str)
        or datetime.fromisoformat(value["updated_at"]).utcoffset() is None
        or datetime.fromisoformat(value["updated_at"]).utcoffset().total_seconds() != 0
        or not _evidence(value["holding"])
        or (
            value["final_observation"] is not None
            and not _evidence(value["final_observation"])
        )
    ):
        return False
    handoff, events = value["handoff"], value["events"]
    if (
        not isinstance(handoff, dict)
        or set(handoff) != {"eligible", "physical_allowed", "reason", "policy_epoch"}
        or type(handoff["eligible"]) is not bool
        or handoff["physical_allowed"] is not False
        or not isinstance(handoff["reason"], str)
        or not (handoff["policy_epoch"] is None or _count(handoff["policy_epoch"]))
        or not isinstance(events, list)
        or len(events) != value["stage_seq"]
    ):
        return False
    for index, event in enumerate(events, 1):
        if (
            not isinstance(event, dict)
            or set(event)
            != {"seq", "stage", "status", "reason", "elapsed_ms", "evidence"}
            or event["seq"] != index
            or not isinstance(event["stage"], str)
            or event["status"] not in {"running", *TERMINAL}
            or not isinstance(event["reason"], str)
            or not _count(event["elapsed_ms"])
            or not isinstance(event["evidence"], dict)
        ):
            return False
    if events:
        if value["status"] == "queued":
            return False
        if events[-1]["stage"] != value["stage"]:
            return False
        if value["status"] in TERMINAL and events[-1]["status"] != value["status"]:
            return False
    elif value["stage"] != "created" or value["status"] not in {"queued", "running"}:
        return False
    target = value["target"]
    if target is not None and (
        not isinstance(target, dict)
        or set(target)
        != {"object_id", "region_id", "position_xyz", "frame_id", "source"}
        or any(
            not isinstance(target[k], str) or not target[k]
            for k in ("object_id", "region_id", "frame_id", "source")
        )
        or not isinstance(target["position_xyz"], list)
        or len(target["position_xyz"]) != 3
        or any(
            type(x) not in (int, float) or not math.isfinite(x)
            for x in target["position_xyz"]
        )
    ):
        return False
    if handoff["eligible"] and (
        value["holding"]["status"] != "pass"
        or not any(
            e["stage"] == "handoff" and e["status"] == "succeeded" for e in events
        )
    ):
        return False
    if value["holding"]["status"] == "pass" and not any(
        e["stage"] == "lift_verify" for e in events
    ):
        return False
    if (
        value["final_observation"] is not None
        and value["final_observation"]["status"] == "pass"
        and not any(
            e["stage"] == "final_verify" and e["status"] == "succeeded" for e in events
        )
    ):
        return False
    if value["status"] == "succeeded" and (
        not handoff["eligible"]
        or value["final_observation"] is None
        or value["final_observation"]["status"] != "pass"
        or not any(
            e["stage"] == "final_verify" and e["status"] == "succeeded" for e in events
        )
    ):
        return False
    return value["usage"] == initial_document(spec)["usage"] and _valid_success_proofs(
        value
    )


class HarnessError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def initial_document(spec: dict) -> dict:
    return {
        "schema": "levi.reset_harness.run.v1",
        "execution_mode": "offline",
        "adapter": "fake",
        "physical_execution": False,
        "status": "queued",
        "stage": "created",
        "stage_seq": 0,
        "events": [],
        "target": None,
        "final_observation": None,
        "holding": {"status": "unknown", "reason": "not_started", "evidence": {}},
        "handoff": {
            "eligible": False,
            "physical_allowed": False,
            "reason": "not_started",
            "policy_epoch": None,
        },
        "usage": {
            "llm_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "token_source": "unavailable",
            "provider": "scripted",
        },
        "elapsed_ms": 0,
        "run_id": spec["run_id"],
        "scenario_id": spec["scenario_id"],
        "seed": spec["seed"],
        "updated_at": utc_now(),
    }


class HarnessManager:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self._lock = threading.RLock()
        self._children: dict[str, subprocess.Popen] = {}

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self.root.is_symlink():
                raise HarnessError(409, "unsafe_store")
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _path(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise HarnessError(404, "run_missing")
        path = self.root / run_id
        if path.is_symlink() or not path.is_dir():
            raise HarnessError(404, "run_missing")
        return path

    def _directories(self):
        if self.root.is_symlink():
            raise HarnessError(409, "unsafe_store")
        if not self.root.exists():
            return []
        return sorted(
            (
                p
                for p in self.root.iterdir()
                if RUN_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
            ),
            key=lambda p: p.name,
        )

    def _read(self, directory: Path) -> dict:
        try:
            spec = self._spec(directory)
            document = read_json(directory / "status.json")
            if not _valid_document(document, spec):
                raise ValueError("invalid evidence")
            return document
        except (OSError, ValueError, TypeError, KeyError):
            raise HarnessError(409, "invalid_run_record") from None

    def _spec(self, directory: Path) -> dict:
        spec = read_json(directory / "request.json")
        if (
            set(spec) != {"request_id", "scenario_id", "seed", "run_id"}
            or spec["run_id"] != directory.name
            or not isinstance(spec["request_id"], str)
            or not REQUEST_ID.fullmatch(spec["request_id"])
            or spec["scenario_id"] not in SCENARIOS
            or type(spec["seed"]) is not int
            or not 0 <= spec["seed"] < 2**32
        ):
            raise ValueError("invalid request record")
        return spec

    def _active(self, directory: Path) -> bool:
        try:
            fd = os.open(directory / "worker.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def get(self, run_id: str) -> dict:
        with self._lock:
            directory = self._path(run_id)
            value = self._read(directory)
            if value["status"] not in TERMINAL and not self._active(directory):
                # Crash/restart: do not re-run the request, signal a stored PID,
                # or infer success. This view reports missing completion evidence.
                # Worker could have completed between the first read and lock
                # check. Its terminal evidence wins over process disappearance.
                value = self._read(directory)
                if value["status"] not in TERMINAL:
                    last_updated = value["updated_at"]
                    value = fail_document(value, "worker_lost")
                    value["updated_at"] = last_updated
            return value

    def catalog(self) -> dict:
        with self._lock:
            runs, issues = [], []
            for path in self._directories():
                try:
                    runs.append(self.get(path.name))
                except (HarnessError, OSError, ValueError):
                    issues.append({"run_id": path.name, "code": "invalid_run_record"})
        runs.sort(key=lambda r: r["updated_at"], reverse=True)
        return {
            "schema": "levi.reset_harness.catalog.v1",
            "execution_mode": "offline",
            "physical_execution": False,
            "scenarios": scenario_catalog(),
            "runs": runs[:50],
            "issues": issues,
        }

    def start(self, request_id: str, scenario_id: str, seed: int) -> dict:
        if not REQUEST_ID.fullmatch(request_id):
            raise HarnessError(422, "invalid_request_id")
        if (
            scenario_id not in SCENARIOS
            or type(seed) is not int
            or not 0 <= seed < 2**32
        ):
            raise HarnessError(422, "invalid_scenario")
        with self._transaction():
            directories = self._directories()
            for path in directories:
                try:
                    saved = self._spec(path)
                except (OSError, ValueError, TypeError):
                    continue  # Retained and reported separately in catalog.issues.
                if saved.get("request_id") == request_id:
                    if (saved["scenario_id"], saved["seed"]) != (scenario_id, seed):
                        raise HarnessError(409, "request_conflict")
                    return self.get(path.name)
            if any(self._active(p) for p in directories):
                raise HarnessError(409, "run_active")
            if len(directories) >= MAX_RUNS:
                raise HarnessError(429, "run_limit")
            spec = {
                "request_id": request_id,
                "scenario_id": scenario_id,
                "seed": seed,
                "run_id": f"reset_{uuid.uuid4().hex}",
            }
            directory = self.root / spec["run_id"]
            staging = self.root / f".pending_{spec['run_id']}"
            staging.mkdir(mode=0o700)
            atomic_json(staging / "request.json", spec)
            document = initial_document(spec)
            atomic_json(staging / "status.json", document)
            fd = os.open(
                staging / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.replace(staging, directory)
            source = Path(__file__).resolve().parents[2]
            bootstrap = (
                "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from levi.automatic.offline_worker import main; raise SystemExit(main())"
            )
            try:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        bootstrap,
                        str(source),
                        "--run-dir",
                        str(directory),
                        "--lease-fd",
                        str(fd),
                    ],
                    cwd=source,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(fd,),
                    env={
                        "LANG": "C.UTF-8",
                        "CUDA_VISIBLE_DEVICES": "",
                        "OMP_NUM_THREADS": "1",
                        "OPENBLAS_NUM_THREADS": "1",
                    },
                )
                self._children[spec["run_id"]] = child
            except OSError:
                atomic_json(
                    directory / "status.json",
                    fail_document(document, "worker_start_failed"),
                )
                raise HarnessError(503, "worker_start_failed") from None
            finally:
                os.close(fd)
            threading.Thread(
                target=self._reap, args=(spec["run_id"], child), daemon=True
            ).start()
            return document

    def _reap(self, run_id: str, child: subprocess.Popen) -> None:
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # Only this manager's live Popen handle; never a PID read from disk.
            child.kill()
            child.wait()
        finally:
            with self._lock:
                self._children.pop(run_id, None)

    def stop(self, run_id: str, request_id: str) -> dict:
        if not REQUEST_ID.fullmatch(request_id):
            raise HarnessError(422, "invalid_request_id")
        with self._transaction():
            directory = self._path(run_id)
            document = self.get(run_id)
            if document["status"] not in TERMINAL:
                stop = directory / "stop.json"
                if not stop.exists():
                    atomic_json(
                        stop, {"request_id": request_id, "updated_at": utc_now()}
                    )
            return document
