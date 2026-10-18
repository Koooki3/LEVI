"""The rollout sets of an AERI run, for the read-only run window.

A ``subtask_loop`` run records one demo per policy segment. The generator
``levi.automatic.rollouts`` (``load_or_build(run_dir, rollout_root)``) groups
them into the manifest ``levi.aeri.rollouts.v1``: round -> forward set /
reverse set -> subtask -> attempts. This module only *projects* that manifest
for the page; it never writes and never serves a file itself.

- Only the fields the page shows leave this module (a whitelist). A check
  keeps only its ``value``: it is the model's judgement, never a label, and
  the request it came from stays behind the private request-evidence
  boundary (``request_evidence_view``).
- The blind rule (L2-AERI-SUBTASK-CHECK-BLIND): in a round without an
  operator label, a *forward* attempt (the caller's ``blind(episode_id)``:
  a forward episode the operator has not labelled) shows neither its check
  (``{"hidden_until_labelled": true}``) nor a stop reason of the blind set
  (``reasons``, the set ``GET runs/{id}/events`` hides), and the round's
  and forward set's outcomes derived from those checks are hidden too.
  Reverse (reset) attempts and round notes stay visible.
- Videos are never read here. An attempt's demo is matched to a dataset that
  LEVI already serves (its own raw capture or a linked live dataset) by the
  episode id in the demo's ``metadata.json``; the URLs point at LEVI's
  dataset file service (``/api/levi/files/<name>/videos/...``), which keeps
  its own checks. Without such a dataset the attempt keeps its demo path and
  ``media.available`` is false, with a ``reason``.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .request_evidence_view import PRIVATE, without_references

GENERATOR = "levi.automatic.rollouts"
SCHEMA = "levi.aeri.rollouts.v1"
HIDDEN = "hidden_until_labelled"
SETS = ("forward", "reverse")
MAX_ROLLOUTS = 500
MAX_SUBTASKS = 200
MAX_ATTEMPTS = 200
MAX_LISTED = 200
MAX_CANDIDATES = 8
MAX_TEXT = 2000
MAX_META_BYTES = 1 << 20
MAX_EPISODES_BYTES = 16 << 20
FILES = "/api/levi/files"

# read(base, parts, limit) -> bytes: the API's bounded, no-symlink reader.
Read = Callable[[Path, tuple[str, ...], int], bytes]
Blind = Callable[[str | None], bool]
# Outcomes that come from the forward checks: hidden with them.
FORWARD_OUTCOMES = frozenset(("forward_done", "forward_failed"))
LOOP_OUTCOMES = frozenset(("done", "success", "failure"))
Clean = Callable[[object], object]


class Unavailable(Exception):
    """This LEVI has no rollout manifest generator."""


def generator():
    """The generator module; ``Unavailable`` when it is not installed."""
    try:
        return importlib.import_module(GENERATOR)
    except ImportError:
        raise Unavailable(GENERATOR) from None


def rollout_root_of(run_dir: Path) -> Path | None:
    """The rollout root of ``<root>/.aeri/runs/<run_id>`` (``launch.run_dir_of``),
    or ``None`` for a run folder of another shape."""
    run_dir = Path(run_dir)
    if run_dir.parent.name != "runs" or run_dir.parent.parent.name != ".aeri":
        return None
    return run_dir.parent.parent.parent


# --- small value checks ----------------------------------------------------------


def _text(value, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _int(value) -> int | None:
    return value if type(value) is int else None


def _number(value) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _label(value):
    """An operator label as text, or its scalar fields."""
    if isinstance(value, str):
        return _text(value, 200)
    if isinstance(value, dict):
        out = {
            k: v
            for k, v in list(value.items())[:12]
            if isinstance(k, str)
            and (v is None or isinstance(v, (str, bool, int, float)))
        }
        return {k: (v[:200] if isinstance(v, str) else v) for k, v in out.items()}
    return None


def demo_parts(demo) -> tuple[str, str, str] | None:
    """``(group, task_folder, demo)`` of a demo path relative to the rollout
    root; ``None`` for anything else (absolute, ``..``, hidden parts)."""
    if not isinstance(demo, str) or not demo or len(demo) > 512 or "\x00" in demo:
        return None
    path = PurePosixPath(demo)
    if path.is_absolute() or len(path.parts) != 3:
        return None
    if any(not p or p.startswith(".") for p in path.parts):
        return None
    group, folder, name = path.parts
    return group, folder, name


# --- media: the datasets LEVI already serves ---------------------------------------


def default_sources():
    """``(items, capture_root, view_root)`` from this LEVI's catalog: every
    readable dataset (``catalog.known``), the confined capture folder of an
    entry and the folder the file service reads for a name."""
    from levi import catalog, links, paths

    try:
        items = catalog.known()
    except (OSError, ValueError):
        items = {}

    def capture_root(item: dict) -> Path:
        if item.get("linked"):
            return links.inside(item["path"])
        return paths.inside(item["path"])

    def view_root(name: str) -> Path:
        return catalog.local_root("local/" + name)

    return items, capture_root, view_root


def _candidates(items: dict, group: str, folder: str) -> list[tuple[str, dict]]:
    """Raw datasets that may hold this task folder's demos: one registered at
    the folder itself, or a live mirror named ``<group>__<folder>`` (and its
    ``__at__<root>`` variants). The demo's episode id decides, not the name."""
    from levi.live import mirror

    base = mirror.dataset_name(group, folder)
    short = base[: mirror.NAME_MAX - len(mirror.ROOT_MARK) - mirror.MARK_MAX - 8]
    out = []
    for name, item in sorted(items.items()):
        if not isinstance(item, dict) or item.get("kind") != "raw" or item.get("base"):
            continue
        leaf = PurePosixPath(str(item.get("path") or "")).name
        if leaf in (folder, base) or leaf.startswith(short + mirror.ROOT_MARK):
            out.append((name, item))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


class Media:
    """Finds the served videos of a demo. One instance per request: dataset
    metadata is read once per dataset."""

    def __init__(self, read: Read, sources=None):
        self.read = read
        self._sources = sources
        self._items = None
        self._views: dict[str, dict | str] = {}

    def _load(self):
        if self._items is None:
            items, self.capture_root, self.view_root = (
                self._sources() if self._sources else default_sources()
            )
            self._items = items if isinstance(items, dict) else {}
        return self._items

    def _json(self, base: Path, parts: tuple[str, ...], limit=MAX_META_BYTES):
        try:
            return json.loads(self.read(base, parts, limit))
        except (OSError, ValueError):
            return None

    def _episode_id(self, item: dict, demo: str) -> str | None:
        try:
            capture = self.capture_root(item)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        meta = self._json(Path(capture), (demo, "metadata.json"))
        for key in ("eval", "aeri"):
            meta = meta.get(key) if isinstance(meta, dict) else None
        found = meta.get("episode_id") if isinstance(meta, dict) else None
        return found if isinstance(found, str) else None

    def _view(self, name: str) -> dict | str:
        """``{info, episodes}`` of a dataset's served view, or a reason."""
        if name in self._views:
            return self._views[name]
        try:
            root = Path(self.view_root(name))
        except (OSError, ValueError, KeyError, TypeError):
            found: dict | str = "no_browsing_view"
        else:
            info = self._json(root, ("meta", "info.json"))
            try:
                lines = self.read(
                    root, ("meta", "episodes.jsonl"), MAX_EPISODES_BYTES
                ).splitlines()
            except OSError:
                lines = None
            if not isinstance(info, dict) or lines is None:
                found = "no_browsing_view"
            else:
                episodes = {}
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(row, dict)
                        and isinstance(row.get("source_demo"), str)
                        and type(row.get("episode_index")) is int
                    ):
                        episodes.setdefault(row["source_demo"], row["episode_index"])
                found = {"info": info, "episodes": episodes}
        self._views[name] = found
        return found

    @staticmethod
    def _videos(name: str, info: dict, index: int) -> list[dict]:
        template = info.get("video_path")
        chunk = info.get("chunks_size")
        features = info.get("features")
        if not isinstance(template, str) or not isinstance(features, dict):
            return []
        chunk = chunk if type(chunk) is int and chunk > 0 else 1000
        out = []
        for key, feature in features.items():
            if not isinstance(feature, dict) or feature.get("dtype") != "video":
                continue
            try:
                path = template.format(
                    episode_chunk=index // chunk, video_key=key, episode_index=index
                )
            except (KeyError, IndexError, ValueError):
                continue
            parts = PurePosixPath(path).parts
            if (
                not parts
                or parts[0] != "videos"
                or any(p in ("", ".", "..", ".aeri") for p in parts)
                or not path.endswith(".mp4")
            ):
                continue
            out.append(
                {
                    "camera": key,
                    "url": f"{FILES}/{quote(name, safe='')}/{quote(path)}",
                }
            )
        return out

    def of(self, demo, episode_id) -> dict:
        """``{available: true, dataset, episode_index, viewer, videos}`` or
        ``{available: false, reason}``."""
        parts = demo_parts(demo)
        if parts is None:
            return {"available": False, "reason": "no_demo"}
        if not isinstance(episode_id, str) or not episode_id:
            return {"available": False, "reason": "no_episode_id"}
        group, folder, leaf = parts
        reason = "no_dataset"
        for name, item in _candidates(self._load(), group, folder):
            if self._episode_id(item, leaf) != episode_id:
                continue
            view = self._view(name)
            if isinstance(view, str):
                reason = view
                continue
            index = view["episodes"].get(leaf)
            if index is None:
                reason = "not_in_view"
                continue
            videos = self._videos(name, view["info"], index)
            if not videos:
                reason = "no_video"
                continue
            return {
                "available": True,
                "dataset": "local/" + name,
                "episode_index": index,
                "viewer": f"/local/{quote(name, safe='')}/episode_{index}",
                "videos": videos,
            }
        return {"available": False, "reason": reason}


# --- the projection ------------------------------------------------------------------


def _check(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    found = value.get("value")
    return {"value": found if found in (True, False) else None}


class _Rule:
    """The blind rule for one round: ``blind`` is ``None`` once the round has
    an operator label."""

    def __init__(self, blind: Blind | None, reasons: frozenset):
        self.blind = blind
        self.reasons = reasons

    def hides(self, episode_id, label) -> bool:
        return self.blind is not None and label is None and self.blind(episode_id)


def _attempt(row: dict, rule: _Rule, media: Media | None, clean: Clean) -> dict:
    episode_id = _text(row.get("episode_id"), 200)
    stop = _text(row.get("stop_reason"), 80)
    label = _label(row.get("operator_label"))
    check = _check(row.get("check"))
    if rule.hides(episode_id, label):
        if stop in rule.reasons:
            stop = HIDDEN
        if check is not None:
            check = {HIDDEN: True}
    out = {
        "attempt": _int(row.get("attempt")),
        "episode_id": episode_id,
        "demo": _text(row.get("demo"), 512),
        "instruction": _text(row.get("instruction")),
        "frames": _int(row.get("frames")),
        "seconds": _number(row.get("seconds")),
        "started_at": _text(row.get("started_at"), 64),
        "ended_at": _text(row.get("ended_at"), 64),
        "stop_reason": stop,
        "check": check,
        "sealed": row.get("sealed") if isinstance(row.get("sealed"), bool) else None,
        "note": _text(row.get("note")) or _text(row.get("problem")),
    }
    incomplete = _pair(row.get("incomplete"), ("folder", "why"))
    if incomplete is not None:
        out["incomplete"] = incomplete
    if label is not None:
        out["operator_label"] = label
    out = clean(out)
    out["media"] = (
        media.of(out["demo"], episode_id)
        if media is not None
        else {"available": False, "reason": "no_dataset"}
    )
    return out


def _subtask(row: dict, rule: _Rule, media: Media | None, clean: Clean) -> dict:
    attempts = row.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    return {
        **clean(
            {
                "index": _int(row.get("index")),
                "instruction": _text(row.get("instruction")),
                "pick": _text(row.get("pick"), 200),
                "place": _text(row.get("place"), 200),
            }
        ),
        "attempts": [
            _attempt(a, rule, media, clean)
            for a in attempts[:MAX_ATTEMPTS]
            if isinstance(a, dict)
        ],
        "attempts_truncated": len(attempts) > MAX_ATTEMPTS,
    }


def _set(
    value, rule: _Rule, media: Media | None, clean: Clean, *, forward: bool
) -> dict | None:
    if not isinstance(value, dict):
        return None
    subtasks = value.get("subtasks")
    subtasks = subtasks if isinstance(subtasks, list) else []
    outcome = _text(value.get("outcome"), 40)
    if forward and rule.blind is not None and outcome in LOOP_OUTCOMES:
        outcome = HIDDEN
    return {
        **clean(
            {
                "plan_file": _text(value.get("plan_file"), 512),
                "task": _text(value.get("task")),
                "outcome": outcome,
            }
        ),
        "subtasks": [
            _subtask(s, rule, media, clean)
            for s in subtasks[:MAX_SUBTASKS]
            if isinstance(s, dict)
        ],
    }


def _pair(value, keys: tuple[str, ...]) -> dict | None:
    """A small record (``abandoned``, ``locked``, ``incomplete``): its text
    fields only, ``None`` when it is not a record."""
    if not isinstance(value, dict):
        return None
    return {k: _text(value.get(k), 300) for k in keys}


def _notes(values) -> list:
    """A round's notes (``motion_refused`` and the like): code, detail, time."""
    if not isinstance(values, list):
        return []
    return [
        {
            "code": _text(v.get("code"), 80),
            "detail": _text(v.get("detail"), 300),
            "at": _text(v.get("at"), 64),
        }
        for v in values[:MAX_LISTED]
        if isinstance(v, dict)
    ]


def redact(value):
    """``value`` with every private request-evidence file name replaced
    (``request_evidence_view.PRIVATE``): such a file is disclosed only through
    ``GET runs/{id}/subtasks``, which authorizes it."""
    if isinstance(value, str):
        return PRIVATE.sub("<private>", value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def _listed(values, clean: Clean) -> list:
    if not isinstance(values, list):
        return []
    out = []
    for value in values[:MAX_LISTED]:
        if isinstance(value, str):
            out.append(clean(value[:MAX_TEXT]))
        elif isinstance(value, dict):
            out.append(clean(_shallow(without_references(value))))
    return out


def _shallow(value: dict) -> dict:
    """A problem entry: its scalar fields and short lists of scalars."""
    out = {}
    for key, item in list(value.items())[:20]:
        if not isinstance(key, str):
            continue
        if item is None or isinstance(item, (bool, int, float)):
            out[key] = item
        elif isinstance(item, str):
            out[key] = item[:MAX_TEXT]
        elif isinstance(item, list) and all(
            isinstance(v, (str, int, float, bool)) for v in item
        ):
            out[key] = [v[:200] if isinstance(v, str) else v for v in item[:20]]
    return out


def project(
    manifest,
    *,
    run_id: str,
    blind: Blind | None = None,
    reasons: Iterable[str] = (),
    media: Media | None = None,
    clean: Clean = lambda value: value,
) -> dict:
    """What the page shows of a rollout manifest. ``clean`` (the API's
    ``scrub``) runs on every text, after ``redact``, before the media URLs
    are added."""
    outer = clean
    clean = lambda value: outer(redact(value))
    manifest = manifest if isinstance(manifest, dict) else {}
    rollouts = manifest.get("rollouts")
    rollouts = rollouts if isinstance(rollouts, list) else []
    source = manifest.get("generated_from")
    source = source if isinstance(source, dict) else {}
    mode = manifest.get("mode")
    out = {
        "schema": manifest.get("schema") if manifest.get("schema") == SCHEMA else None,
        "run_id": run_id,
        "task": clean(_text(manifest.get("task"))),
        "mode": mode if mode in ("external", "local") else "unknown",
        "reset_mode": _text(manifest.get("reset_mode"), 40),
        "generated_from": {
            "journal_sha256": _text(source.get("journal_sha256"), 64),
            "events": _int(source.get("events")),
        },
        "rollouts": [],
        "rollouts_truncated": len(rollouts) > MAX_ROLLOUTS,
        "unmatched_demos": _listed(manifest.get("unmatched_demos"), clean),
        "problems": _listed(manifest.get("problems"), clean),
    }
    for row in rollouts[:MAX_ROLLOUTS]:
        if not isinstance(row, dict):
            continue
        sets = row.get("sets") if isinstance(row.get("sets"), dict) else {}
        found = {
            "round": _int(row.get("round")),
            "round_id": _text(row.get("round_id"), 200),
            "released_at": _text(row.get("released_at"), 64),
            "outcome": _text(row.get("outcome"), 40),
            "notes": _notes(row.get("notes")),
        }
        for key, keys in (
            ("abandoned", ("phase", "why")),
            ("locked", ("reason", "at")),
        ):
            record = _pair(row.get(key), keys)
            if record is not None:
                found[key] = record
        label = _label(row.get("operator_label"))
        rule = _Rule(blind if label is None else None, frozenset(reasons))
        if label is not None:
            found["operator_label"] = label
        elif blind is not None and found["outcome"] in FORWARD_OUTCOMES:
            found["outcome"] = HIDDEN
        out["rollouts"].append(
            {
                **clean(found),
                "sets": {
                    k: _set(sets.get(k), rule, media, clean, forward=k == "forward")
                    for k in SETS
                },
            }
        )
    return out
