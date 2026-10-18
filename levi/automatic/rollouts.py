"""AERI rollout sets (``levi.aeri.rollouts.v1``): one subtask_loop run's demos
organised as rounds -> forward/reverse sets -> subtasks -> attempts.

The policy does not run for the whole round, only for each subtask episode,
and each episode is recorded as its own ``demo_NNNN`` folder (forward ones
in ``<forward_folder>__subtask``, reverse ones in ``<forward_folder>__reverse``).
A round therefore holds a forward set and a reverse set, each with one or
more subtasks, and a retried subtask has several attempts (several demos).

``build_rollouts`` derives that structure from what the run already wrote and
nothing else: the run journal (``state_journal.jsonl``, read without its lock
and never written), the subtask sidecars it names (``subtasks/``), the
optional labels (``labels/``), ``plan.json`` and each demo folder's
``metadata.json`` under the recording root. It is pure: it reads, it never
writes, copies or moves a demo. ``write_rollouts`` keeps the result as
``<run_dir>/rollouts.json`` (temporary file, then rename); the file can be
deleted and rebuilt at any time.

Rules:

- each ``subtask_started`` note is one attempt; its ``episode_id`` is
  matched exactly against ``metadata.json`` ``eval.aeri.episode_id``. A
  demo that matches no attempt, or an attempt without a demo, goes to
  ``problems``; nothing is guessed from folder names or numbers;
- a demo still named ``incomplete_*`` (its recording was never sealed) is
  ``demo: null`` with an ``incomplete`` entry that says why;
- ``check`` is the VLM's judgement of the subtask (``subtask_check``), a
  model's answer that drives the loop. It is never an operator label nor an
  outcome. Operator labels come from ``labels/`` only (``operator_label``
  for the round from ``round_label.jsonl``, for an attempt from
  ``operator_label.jsonl``), and only when one exists;
- the journal is parsed line by line as JSON; it is not re-validated here
  (``journal.scan`` does that). A whole line that is not JSON is reported in
  ``problems``; a torn last line (no newline) is ignored.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "levi.aeri.rollouts.v1"
FILE = "rollouts.json"
JOURNAL = "state_journal.jsonl"
LOOPS = ("forward", "reverse")
# Round outcomes. ``forward_done``: the forward loop ended with every subtask
# checked done (or the operator's success); ``forward_failed``: it ended with
# the operator's failure; ``discarded``: the operator discarded the round;
# ``abandoned``: a resume abandoned it (``subtask_round_abandoned``);
# ``locked``: the run went to FAULT_LOCKED during it; ``incomplete``: the
# journal ends before the forward loop did.
OUTCOMES = (
    "forward_done",
    "forward_failed",
    "discarded",
    "abandoned",
    "locked",
    "incomplete",
)
# Notes kept on the round they happened in (what a reader needs to see why a
# round stopped).
ROUND_NOTES = (
    "motion_refused",
    "subtask_plan_refused",
    "vlm_wake_failed",
    "vlm_wake_refused",
    "vlm_sleep_refused",
)


def _iso(wall_ns) -> str | None:
    if not isinstance(wall_ns, int) or isinstance(wall_ns, bool):
        return None
    return datetime.fromtimestamp(wall_ns / 1e9, tz=UTC).isoformat(
        timespec="milliseconds"
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


def _inside(base: Path, relative) -> Path | None:
    """``base / relative`` when it stays inside ``base`` (sidecar names come
    from the journal: never follow one out of the run folder)."""
    if not isinstance(relative, str) or not relative:
        return None
    path = (base / relative).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return None
    return path


def _journal(run_dir: Path, problems: list) -> tuple[list, dict]:
    path = run_dir / JOURNAL
    try:
        data = path.read_bytes()
    except OSError as exc:
        problems.append(
            {"kind": "journal_unreadable", "detail": f"{JOURNAL}: {exc.strerror}"}
        )
        return [], {"journal_sha256": None, "events": 0}
    events = []
    # What follows the last newline is a torn write: not an event.
    for number, line in enumerate(data.split(b"\n")[:-1], start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            problems.append(
                {"kind": "journal_line_unreadable", "detail": f"line {number}"}
            )
            continue
        events.append(event)
    found = {"journal_sha256": hashlib.sha256(data).hexdigest(), "events": len(events)}
    return events, found


def _note(event: dict) -> tuple[str | None, object]:
    note = event.get("note")
    if event.get("record") != "note" or not isinstance(note, dict):
        return None, None
    return note.get("code"), note.get("detail")


def _detail(run_dir: Path, raw) -> dict:
    """A subtask note's detail. A long one keeps only its short fields (or just
    ``file``) in the journal and the whole in its sidecar: fill what is
    missing from there."""
    try:
        detail = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        detail = None
    if not isinstance(detail, dict):
        return {}
    path = _inside(run_dir, detail.get("file"))
    side = _read_json(path) if path is not None else None
    if isinstance(side, dict):
        for key, value in side.items():
            detail.setdefault(key, value)
    return detail


def _plan_subtasks(run_dir: Path, plan_file) -> list:
    """The planned subtasks of one loop (index, instruction, pick, place) from
    its plan sidecar; empty when it cannot be read."""
    path = _inside(run_dir, plan_file)
    side = _read_json(path) if path is not None else None
    if not isinstance(side, dict):
        return []
    found = side.get("subtasks")
    if not isinstance(found, list):
        request = side.get("request")
        found = request.get("parsed") if isinstance(request, dict) else None
    out = []
    for item in found if isinstance(found, list) else []:
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            out.append(
                {k: item.get(k) for k in ("index", "instruction", "pick", "place")}
            )
    return out


def _check_request(run_dir: Path, detail: dict):
    """The VLM request id named in a check's sidecar, if any."""
    path = _inside(run_dir, detail.get("file"))
    side = _read_json(path) if path is not None else None
    if isinstance(side, dict) and isinstance(side.get("request_id"), str):
        return side["request_id"]
    return None


def _plan(run_dir: Path) -> dict:
    kept = _read_json(run_dir / "plan.json")
    plan = kept.get("plan") if isinstance(kept, dict) else None
    return plan if isinstance(plan, dict) else {}


def _mode(plan: dict) -> str:
    """The VLM provider mode declared in the frozen plan (the rule of
    ``api.subtask_provider_of``): ``external``, ``local`` or ``unknown``."""
    subtask = plan.get("subtask")
    vlm = subtask.get("vlm") if isinstance(subtask, dict) else None
    mode = vlm.get("provider_mode") if isinstance(vlm, dict) else None
    return mode if mode in ("local", "external") else "unknown"


# --- demo folders --------------------------------------------------------------------------------


def _literal(value):
    """A value that may have been stored as its Python repr (a dict, a quoted
    string): parsed with ``ast.literal_eval`` only, never ``eval``."""
    if isinstance(value, str) and value[:1] in ("{", "'", '"', "("):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return value
    return value


def episode_of(meta) -> str | None:
    """``eval.aeri.episode_id`` of a demo's metadata, read defensively."""
    if not isinstance(meta, dict):
        return None
    block = _literal(meta.get("eval"))
    block = _literal(block.get("aeri")) if isinstance(block, dict) else None
    if not isinstance(block, dict):
        return None
    episode = _literal(block.get("episode_id"))
    if isinstance(episode, dict):
        episode = episode.get("episode_id")
    return episode if isinstance(episode, str) and episode else None


def _run_folders(rollout_root: Path, run_id: str, plan: dict, names: set) -> list:
    """``(group, task folder)`` pairs of this run under the recording root:
    the folders the plan, the run manifest and the journal name, plus any
    folder whose name ends with ``__<run_id>`` or holds ``__<run_id>__``."""
    recording = plan.get("recording") if isinstance(plan.get("recording"), dict) else {}
    group = recording.get("group")
    try:
        groups = (
            [group]
            if isinstance(group, str) and (rollout_root / group).is_dir()
            else sorted(
                p.name
                for p in rollout_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        )
    except OSError:
        return []
    out = []
    for name in groups:
        try:
            children = sorted(p.name for p in (rollout_root / name).iterdir())
        except OSError:
            continue
        for child in children:
            if (
                child in names
                or child.endswith(f"__{run_id}")
                or f"__{run_id}__" in child
            ) and (rollout_root / name / child).is_dir():
                out.append((name, child))
    return out


def _demos(rollout_root: Path, folders: list, run_id: str, problems: list) -> dict:
    """``{episode_id: [demo, ...]}`` of the demo folders of this run."""
    out: dict = {}
    for group, folder in folders:
        base = rollout_root / group / folder
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for path in children:
            name = path.name
            if not name.startswith(("demo_", "incomplete_")):
                continue
            relative = f"{group}/{folder}/{name}"
            meta = _read_json(path / "metadata.json")
            episode = episode_of(meta)
            if episode is None:
                problems.append(
                    {
                        "kind": "demo_without_episode_id",
                        "demo": relative,
                        "detail": "metadata.json has no eval.aeri.episode_id",
                    }
                )
                continue
            if not episode.startswith(f"{run_id}."):
                problems.append(
                    {
                        "kind": "demo_of_other_run",
                        "demo": relative,
                        "episode_id": episode,
                    }
                )
                continue
            evaluation = meta.get("eval") if isinstance(meta.get("eval"), dict) else {}
            out.setdefault(episode, []).append(
                {
                    "path": relative,
                    "incomplete": name.startswith("incomplete_"),
                    "frames": meta.get("frame_count"),
                    "abort_reason": evaluation.get("abort_reason"),
                }
            )
    return out


# --- the manifest --------------------------------------------------------------------------------


def _new_round(number, round_id, released_at) -> dict:
    return {
        "round": number,
        "round_id": round_id,
        "released_at": released_at,
        "outcome": "incomplete",
        "sets": {loop: None for loop in LOOPS},
        "notes": [],
    }


def _set(found: dict, loop: str) -> dict:
    if found["sets"][loop] is None:
        found["sets"][loop] = {
            "plan_file": None,
            "task": None,
            "outcome": None,
            "subtasks": [],
        }
    return found["sets"][loop]


def _subtask(entry: dict, index) -> dict:
    for sub in entry["subtasks"]:
        if sub["index"] == index:
            return sub
    sub = {
        "index": index,
        "instruction": None,
        "pick": None,
        "place": None,
        "attempts": [],
    }
    entry["subtasks"].append(sub)
    entry["subtasks"].sort(key=_index_key)
    return sub


def _index_key(sub: dict) -> tuple:
    index = sub["index"]
    return (0, index) if isinstance(index, int) else (1, 0)


def _forward_outcome(value) -> str:
    if value in ("done", "success"):
        return "forward_done"
    if value in ("discard", "discarded"):
        return "discarded"
    return "forward_failed"


def build_rollouts(run_dir: Path, rollout_root: Path) -> dict:
    """The ``levi.aeri.rollouts.v1`` manifest of one run (see the module
    text). Reads only; writes nothing."""
    run_dir, rollout_root = Path(run_dir), Path(rollout_root)
    problems: list = []
    events, generated = _journal(run_dir, problems)
    plan = _plan(run_dir)
    run = plan.get("run") if isinstance(plan.get("run"), dict) else {}
    run_id = next(
        (e["run_id"] for e in events if isinstance(e.get("run_id"), str)),
        run.get("run_id") or run_dir.name,
    )
    header = next(
        (e.get("header") for e in events if e.get("record") == "run_header"), None
    )
    task = run.get("subtask_task")

    rounds: list = []
    by_number: dict = {}
    current = None
    attempts: dict = {}  # episode_id -> attempt
    started_wall: dict = {}  # episode_id -> wall ns of the first dispatch commit
    for event in events:
        code, raw = _note(event)
        wall = event.get("emitted_wall_ns")
        if code == "subtask_round_released":
            detail = _detail(run_dir, raw)
            current = _new_round(
                detail.get("round"), detail.get("round_id"), _iso(wall)
            )
            rounds.append(current)
            by_number[detail.get("round")] = current
            continue
        if code in (
            "subtask_plan",
            "subtask_started",
            "subtask_check",
            "subtask_loop_done",
            "subtask_round_abandoned",
        ):
            detail = _detail(run_dir, raw)
            number = detail.get("round")
            found = by_number.get(number)
            if found is None:
                problems.append(
                    {
                        "kind": "round_not_released",
                        "detail": f"{code} names round {number!r}, which no "
                        "subtask_round_released note opened",
                    }
                )
                found = _new_round(number, None, None)
                rounds.append(found)
                by_number[number] = found
            current = found
            loop = detail.get("loop") or detail.get("phase")
            if code == "subtask_round_abandoned":
                found["abandoned"] = {
                    "phase": detail.get("phase"),
                    "why": detail.get("why"),
                }
                continue
            if loop not in LOOPS:
                problems.append(
                    {"kind": "unknown_loop", "detail": f"{code}: loop {loop!r}"}
                )
                continue
            entry = _set(found, loop)
            if code == "subtask_plan":
                entry["plan_file"] = detail.get("plan_file")
                entry["task"] = detail.get("task")
                task = task or (detail.get("task") if loop == "forward" else None)
                for planned in _plan_subtasks(run_dir, detail.get("plan_file")):
                    sub = _subtask(entry, planned["index"])
                    for key in ("instruction", "pick", "place"):
                        if sub[key] is None:
                            sub[key] = planned[key]
            elif code == "subtask_started":
                episode = detail.get("episode_id")
                sub = _subtask(entry, detail.get("index"))
                if sub["instruction"] is None:
                    sub["instruction"] = detail.get("instruction")
                attempt = {
                    "attempt": detail.get("attempt"),
                    "episode_id": episode,
                    "instruction": detail.get("instruction"),
                    "demo": None,
                    "frames": None,
                    "started_at": None,
                    "ended_at": None,
                    "seconds": None,
                    "stop_reason": None,
                    "check": None,
                }
                sub["attempts"].append(attempt)
                if isinstance(episode, str):
                    if episode in attempts:
                        problems.append(
                            {"kind": "episode_started_twice", "episode_id": episode}
                        )
                    attempts[episode] = attempt
            elif code == "subtask_check":
                attempt = attempts.get(detail.get("episode_id"))
                if attempt is None:
                    problems.append(
                        {
                            "kind": "check_without_attempt",
                            "episode_id": detail.get("episode_id"),
                        }
                    )
                    continue
                # The VLM's judgement; never an operator label or outcome.
                attempt["check"] = {
                    "by": "vlm",
                    "value": detail.get("value"),
                    "file": detail.get("file"),
                    "request": _check_request(run_dir, detail),
                }
            else:  # subtask_loop_done
                entry["outcome"] = detail.get("outcome")
                if loop == "forward" and found["outcome"] == "incomplete":
                    found["outcome"] = _forward_outcome(detail.get("outcome"))
            continue
        if code in ROUND_NOTES and current is not None:
            current["notes"].append(
                {"code": code, "detail": str(raw)[:300], "at": _iso(wall)}
            )
            continue
        if event.get("record") != "committed":
            continue
        episode = event.get("episode_id")
        if isinstance(episode, str) and episode not in started_wall:
            started_wall[episode] = wall
        result = event.get("episode_result")
        attempt = attempts.get(episode) if isinstance(episode, str) else None
        if attempt is not None and isinstance(result, dict):
            attempt["stop_reason"] = result.get("stop_reason")
            attempt["ended_at"] = _iso(wall)
            begun = started_wall.get(episode)
            if isinstance(begun, int) and isinstance(wall, int):
                attempt["started_at"] = _iso(begun)
                attempt["seconds"] = round((wall - begun) / 1e9, 1)
            rollout = result.get("rollout")
            if isinstance(rollout, dict):
                attempt["sealed"] = rollout.get("sealed")
        if event.get("to_state") == "FAULT_LOCKED" and current is not None:
            current["outcome"] = "locked"
            current["locked"] = {"reason": event.get("reason"), "at": _iso(wall)}

    for found in rounds:
        if found["outcome"] != "locked" and "abandoned" in found:
            found["outcome"] = "abandoned"
        for loop in LOOPS:
            entry = found["sets"][loop]
            if entry is not None and entry["outcome"] is None:
                if found["outcome"] in ("abandoned", "locked"):
                    entry["outcome"] = found["outcome"]
                else:
                    entry["outcome"] = "incomplete"

    # Demos: exact episode_id match, nothing guessed.
    names = {
        run.get(key)
        for key in (
            "subtask_forward_folder",
            "subtask_reverse_folder",
            "forward_folder",
        )
        if isinstance(run.get(key), str)
    }
    manifest = _read_json(run_dir / "manifest.json")
    for item in manifest.get("episodes", []) if isinstance(manifest, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("task_folder"), str):
            names.add(item["task_folder"])
    demos = _demos(
        rollout_root, _run_folders(rollout_root, run_id, plan, names), run_id, problems
    )
    used: set = set()
    for episode, attempt in attempts.items():
        found = demos.get(episode, [])
        if len(found) > 1:
            problems.append(
                {
                    "kind": "several_demos",
                    "episode_id": episode,
                    "demos": [d["path"] for d in found],
                }
            )
        if not found:
            problems.append({"kind": "attempt_without_demo", "episode_id": episode})
            continue
        used.add(episode)
        demo = found[0]
        attempt["frames"] = demo["frames"]
        if demo["incomplete"]:
            attempt["incomplete"] = {
                "folder": demo["path"],
                "why": "recording not sealed (incomplete_*)"
                + (f": {demo['abort_reason']}" if demo["abort_reason"] else ""),
            }
        else:
            attempt["demo"] = demo["path"]
    unmatched = []
    for episode in sorted(set(demos) - used):
        for demo in demos[episode]:
            unmatched.append({"episode_id": episode, "demo": demo["path"]})
            problems.append(
                {
                    "kind": "demo_without_attempt",
                    "episode_id": episode,
                    "demo": demo["path"],
                }
            )

    _operator_labels(run_dir, rounds, attempts, problems)
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "task": task,
        "mode": _mode(plan),
        "reset_mode": header.get("reset_mode") if isinstance(header, dict) else None,
        "generated_from": generated,
        "rollouts": rounds,
        "unmatched_demos": unmatched,
        "problems": problems,
    }


def _operator_labels(
    run_dir: Path, rounds: list, attempts: dict, problems: list
) -> None:
    """A person's labels, where there are any: the round's
    (``round_label.jsonl``) and an episode's (``operator_label.jsonl``)."""
    from . import metrics

    try:
        by_round = metrics.round_labels(run_dir)
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        problems.append({"kind": "round_labels_unreadable", "detail": str(exc)[:200]})
        by_round = {}
    for found in rounds:
        label = by_round.get(found["round_id"])
        if label and label.get("value") is not None:
            found["operator_label"] = {
                k: label.get(k) for k in ("value", "by", "source")
            }
    try:
        lines = metrics.LabelStore(run_dir).lines("operator_label")
    except (metrics.LabelRefused, OSError) as exc:
        problems.append(
            {"kind": "operator_labels_unreadable", "detail": str(exc)[:200]}
        )
        return
    for episode, view in metrics.operator_view(lines).items():
        attempt = attempts.get(episode)
        if attempt is not None and view.get("current") is not None:
            attempt["operator_label"] = view["current"]


def _encode(manifest: dict) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    ).encode()


def write_rollouts(run_dir: Path, rollout_root: Path) -> Path:
    """Build and keep ``<run_dir>/rollouts.json`` (temporary file, fsync,
    rename). The only file this module writes."""
    from .journal import write_durable

    run_dir = Path(run_dir)
    path = run_dir / FILE
    write_durable(path, _encode(build_rollouts(run_dir, rollout_root)))
    return path


def load_or_build(run_dir: Path, rollout_root: Path) -> dict:
    """``rollouts.json`` when it is newer than the journal and readable, else
    the manifest built in memory (nothing is written)."""
    run_dir = Path(run_dir)
    try:
        kept = (run_dir / FILE).stat().st_mtime_ns
        # Labels are written to labels/*.jsonl, not the journal: a label added
        # after the run closed must rebuild, or the round stays hidden
        # (review of int/aeri-1018, P2-1).
        newest = max(
            [(run_dir / JOURNAL).stat().st_mtime_ns]
            + [p.stat().st_mtime_ns for p in (run_dir / "labels").glob("*.jsonl")]
        )
    except OSError:
        kept = newest = None
    if kept is not None and kept > newest:
        found = _read_json(run_dir / FILE)
        if isinstance(found, dict) and found.get("schema") == SCHEMA:
            return found
    return build_rollouts(run_dir, rollout_root)


def default_root(run_dir: Path) -> Path:
    """The recording root of a run: ``launch.json``'s or the plan's
    ``rollout_root``, else ``<root>`` of ``<root>/.aeri/runs/<run id>``."""
    run_dir = Path(run_dir)
    for found in (
        _read_json(run_dir / "launch.json"),
        (_plan(run_dir).get("recording") or {}),
    ):
        if isinstance(found, dict) and isinstance(found.get("rollout_root"), str):
            return Path(found["rollout_root"])
    return run_dir.resolve().parents[2]


def summary(manifest: dict) -> str:
    """A compact text summary: rounds, per set subtasks x attempts, problems."""
    lines = [
        (
            f"{manifest.get('run_id')}  task: {manifest.get('task')}  mode: "
            f"{manifest.get('mode')}  rounds: {len(manifest.get('rollouts', []))}  "
            "(check = VLM judgement, not a label)"
        )
    ]
    for found in manifest.get("rollouts", []):
        label = (found.get("operator_label") or {}).get("value")
        lines.append(
            f"round {found.get('round')} [{found.get('round_id')}] "
            f"{found.get('outcome')}" + (f"  operator: {label}" if label else "")
        )
        for loop in LOOPS:
            entry = found["sets"].get(loop)
            if entry is None:
                lines.append(f"  {loop}: -")
                continue
            lines.append(
                f"  {loop} ({entry.get('outcome')}): {len(entry['subtasks'])} subtask(s)"
            )
            for sub in entry["subtasks"]:
                lines.append(
                    f"    #{sub['index']} {sub['instruction']}: "
                    f"{len(sub['attempts'])} attempt(s)"
                )
                for attempt in sub["attempts"]:
                    check = attempt.get("check") or {}
                    where = attempt.get("demo") or (
                        "incomplete " + attempt["incomplete"]["folder"]
                        if attempt.get("incomplete")
                        else "no demo"
                    )
                    lines.append(
                        f"      try {attempt['attempt']} {attempt['episode_id']}: "
                        f"{where}, frames {attempt.get('frames')}, "
                        f"{attempt.get('seconds')} s, stop {attempt.get('stop_reason')}, "
                        f"check {check.get('value', '-')}"
                    )
        for note in found.get("notes", []):
            lines.append(f"  note {note['code']}: {note['detail'][:120]}")
    problems = manifest.get("problems", [])
    lines.append(f"problems: {len(problems)}")
    for problem in problems:
        lines.append("  " + json.dumps(problem, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)
