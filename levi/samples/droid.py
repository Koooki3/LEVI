"""A reproducible DROID raw test sample: 500 episodes of the public release.

Each *draw* is a new LEVI raw capture ``<workspace>/droid_raw_<size>_draw<NN>``
with ``demo_0000`` … and ``_meta/`` (the selection, what was replaced and
the verification). Draws come from one seeded order of the whole release:

- Every episode of the release is listed once (``metadata_*.json`` objects;
  the listing is kept with the workspace's sample ledger, so later draws use
  the same list).
- The order is the round-robin of the reference subset script: episodes are
  grouped by (lab, outcome), each group shuffled with seed 42, and groups are
  visited in a reshuffled order each round -- no language or success filter.
  A new workspace's first draw starts at position 0 (that script's
  ``selected_500``); each later draw takes the next unused positions of the
  same order, so the draws of one workspace never overlap. What identifies
  a draw's episodes is these positions, recorded in ``_meta/selection.json``
  (``order_positions``, ``replaced``) -- not its name, which only numbers
  the workspace's own draws.
- An episode LEVI cannot read (a missing camera, a malformed trajectory) is
  replaced by the next one of the order; ``_meta/selection.json`` names it.

Only ``metadata_*.json``, ``trajectory.h5`` and the non-stereo
``recordings/MP4/*.mp4`` are kept (no SVO, no stereo MP4). Every file is
checked against the bucket's size and MD5 -- what ``rclone check`` does --
and downloads resume file by file. The capture is assembled in a hidden
``.droid_raw_…partial`` folder that the workspace sync ignores and is renamed
into place only when all episodes are verified, so a half-downloaded sample
is never registered.

Nothing is written outside the workspace, and the public bucket is only
read, anonymously.

  python -m levi.samples run --draw N [--workers 4]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import errno
import fcntl
import hashlib
import http.client
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..paths import ROOT, STATE, inside

BUCKET = "gresearch"
PREFIX = "robotics/droid_raw/1.0.1"
SOURCE = f"gs://{BUCKET}/{PREFIX}"
DOCS = "https://droid-dataset.github.io/droid/the-droid-dataset"
API = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o"
MEDIA = f"https://storage.googleapis.com/{BUCKET}/"
SEED = 42
SIZE = 500
KEY = "droid_raw"
# What LEVI's DROID reader needs; a candidate without it is replaced before
# anything is downloaded.
CAMERAS = 3
ACTIVE = ("planned", "listing", "selecting", "downloading", "verifying")
# Retrying cannot help these: the local disk failed, not the network.
LOCAL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG, errno.EROFS})
# The request itself is refused; asking again gives the same answer.
PERMANENT_HTTP = frozenset({400, 401, 403, 404})
# How long a draw keeps trying for the run lock (a status probe holds it for
# microseconds) before concluding that another process draws.
LOCK_WAIT = 10.0
NAME = re.compile(rf"^{KEY}_(\d+)_draw(\d+)$")


class Stopped(Exception):
    """The draw is stopping (Ctrl-C, a failure elsewhere): a thread gives up."""


class NetworkError(RuntimeError):
    """The bucket could not be reached after every attempt. Transient: the
    service retries such a draw on its next start."""


def _check(stop) -> None:
    if stop is not None and stop.is_set():
        raise Stopped("the draw is stopping")


def _pause(stop, seconds: float) -> None:
    """The wait between two attempts; a stop ends it at once."""
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise Stopped("the draw is stopping")


@contextlib.contextmanager
def _threads(workers: int, stop: threading.Event):
    """A thread pool that stops with its caller. When the caller leaves by an
    exception -- Ctrl-C included -- ``stop`` is set, queued work is cancelled,
    and the caller goes on only once every running thread has returned (each
    checks ``stop`` before a file and between chunks). So the run lock is
    never released while a thread still writes."""
    pool = ThreadPoolExecutor(max_workers=workers)
    interrupted = False
    try:
        yield pool
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=stop.is_set())
        while True:
            try:
                pool.shutdown(wait=True)
                break
            except KeyboardInterrupt:
                # Another Ctrl-C: the threads are stopping already, and
                # leaving now would release the lock under them.
                stop.set()
                interrupted = True
    if interrupted:
        raise KeyboardInterrupt


# ------------------------------------------------------------------ places


# ``base=ROOT`` names this module's own ROOT (not inside()'s default, bound
# at import), so a test can point the whole sample at a temporary workspace.


def home() -> Path:
    return inside(STATE / "samples", base=ROOT)


def ledger_path() -> Path:
    return home() / f"{KEY}.json"


def listing_path() -> Path:
    return home() / KEY / "episodes.txt"


def dataset_name(size: int, draw: int) -> str:
    return f"{KEY}_{size}_draw{draw:02d}"


def final_root(name: str) -> Path:
    return inside(ROOT / name, base=ROOT)


def partial_root(name: str) -> Path:
    # Hidden and ``.partial``: the workspace sync skips it (levi/sync.py).
    return inside(ROOT / f".{name}.partial", base=ROOT)


def progress_path(name: str) -> Path:
    return home() / f"{name}.progress.json"


def log_path(name: str) -> Path:
    return home() / f"{name}.log"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def _flock(path: Path, blocking=True, wait: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_lock(wait: float = 0.0):
    """Held by the process that is drawing, for as long as it draws (until
    every thread it started has returned). ``wait``: seconds to keep trying
    before answering False."""
    return _flock(home() / f"{KEY}.run.lock", blocking=False, wait=wait)


def update_ledger(change) -> dict:
    """Read-modify-write of the ledger under its own lock (several LEVI
    processes may share a workspace)."""
    with _flock(home() / f"{KEY}.ledger.lock"):
        value = read_json(ledger_path(), {})
        value.setdefault("source", SOURCE)
        value.setdefault("seed", SEED)
        value.setdefault("cursor", 0)
        value.setdefault("draws", [])
        change(value)
        write_json(ledger_path(), value)
        return value


def draw_entry(ledger: dict, draw: int) -> dict:
    return next(d for d in ledger["draws"] if d["draw"] == draw)


def set_draw(draw: int, **fields) -> dict:
    return update_ledger(lambda v: draw_entry(v, draw).update(fields))


# --------------------------------------------------------------------- disk


def disk() -> dict:
    """Free and total bytes where the sample goes -- for the status only;
    nothing checks it before a draw (the first draw of 500 episodes was
    11.6 GiB)."""
    usage = shutil.disk_usage(ROOT)
    return {"free": usage.free, "total": usage.total}


def gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


# ------------------------------------------------------------------- bucket


def transient(exc: BaseException) -> bool:
    """A failure that asking again can cure: the network, a body cut short, a
    server error -- not a refusal (400/401/403/404), not the local disk, not
    a stop."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code not in PERMANENT_HTTP
    if isinstance(exc, OSError):
        return exc.errno not in LOCAL_ERRNOS
    return isinstance(exc, http.client.HTTPException | ValueError)


class Bucket:
    """Anonymous, read-only access to the public bucket (JSON API)."""

    def __init__(self, attempts=6):
        self.attempts = attempts

    def _request(self, url, timeout, consume, stop=None, what="request"):
        """One request -- open, read the whole body, parse or write it --
        retried as a whole; the only retry layer. A refusal, a local disk
        error or a stop is raised at once; running out of attempts raises
        :class:`NetworkError`."""
        last: BaseException | None = None
        for attempt in range(self.attempts):
            _check(stop)
            try:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    return consume(response)
            except Exception as exc:  # sorted by transient()
                if not transient(exc):
                    raise
                last = exc
            if attempt + 1 < self.attempts:
                _pause(stop, min(30, 2**attempt))
        raise NetworkError(
            f"{what} failed after {self.attempts} attempts "
            f"({type(last).__name__}: {last})"
        ) from last

    def objects(self, prefix, glob=None, delimiter=None, stop=None):
        """Objects (and, with a delimiter, sub-prefixes) under ``prefix``."""
        token = None
        while True:
            params = {
                "prefix": prefix,
                "maxResults": "1000",
                "fields": "items(name,size,md5Hash),prefixes,nextPageToken",
            }
            if glob:
                params["matchGlob"] = glob
            if delimiter:
                params["delimiter"] = delimiter
            if token:
                params["pageToken"] = token
            page = self._request(
                API + "?" + urllib.parse.urlencode(params),
                90,
                lambda r: json.loads(r.read()),
                stop,
                what=f"listing {prefix}",
            )
            yield from ({"prefix": p} for p in page.get("prefixes", []))
            for item in page.get("items", []):
                yield {
                    "name": item["name"],
                    "size": int(item["size"]),
                    "md5": item.get("md5Hash"),
                }
            token = page.get("nextPageToken")
            if not token:
                return

    def fetch(
        self, name, dest: Path, size: int, md5: str | None, on_bytes=None, stop=None
    ):
        """Download one object; the file appears only once size and MD5 match.
        ``stop`` is checked between chunks."""
        url = MEDIA + urllib.parse.quote(name)
        temporary = dest.with_name(dest.name + ".part")

        def body(response):
            digest, count = hashlib.md5(), 0
            with temporary.open("wb") as out:
                while True:
                    _check(stop)
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
            got = base64.b64encode(digest.digest()).decode()
            if count != size or (md5 is not None and got != md5):
                # Cut short or damaged in transit: asked for again.
                raise ValueError(f"size {count}/{size}, md5 {got}/{md5}")
            return count

        try:
            count = self._request(
                url, 120, body, stop, what=f"{name.rsplit('/', 1)[-1]}: download"
            )
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, dest)
        if on_bytes:
            on_bytes(count)


def verified(path: Path, size: int, md5: str | None) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    if md5 is None:
        return True
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode() == md5


# ---------------------------------------------------------- listing, order


def list_release(bucket: Bucket, progress=None, stop=None) -> list[str]:
    """Every episode's metadata object, relative to the release root:
    ``lab/outcome/date/episode/metadata_*.json`` -- kept with the ledger."""
    stop = stop or threading.Event()
    path = listing_path()
    if path.is_file() and path.stat().st_size:
        return path.read_text().splitlines()
    labs = [
        item["prefix"]
        for item in bucket.objects(PREFIX + "/", delimiter="/", stop=stop)
        if "prefix" in item
    ]
    found: list[str] = []
    lock = threading.Lock()

    def one(lab):
        rows = [
            item["name"][len(PREFIX) + 1 :]
            for item in bucket.objects(lab, glob="**/metadata_*.json", stop=stop)
            if "name" in item
        ]
        with lock:
            found.extend(rows)
            if progress:
                progress(len(found), lab.rstrip("/").rsplit("/", 1)[-1])

    with _threads(min(8, max(1, len(labs))), stop) as pool:
        for future in as_completed([pool.submit(one, lab) for lab in labs]):
            future.result()
    rows = sorted(set(found))
    write_json(path.with_suffix(".json"), {"source": SOURCE, "listed_at": time.time()})
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(rows) + "\n")
    os.replace(temporary, path)
    return rows


def episodes_of(listing: list[str]) -> dict[str, tuple[str, str]]:
    """Episode folder (``lab/outcome/date/episode``) -> (lab, outcome)."""
    out = {}
    for relative in listing:
        parts = relative.split("/")
        if len(parts) != 5 or not parts[-1].startswith("metadata_"):
            continue
        lab, outcome, date, episode, _ = parts
        if all((lab, outcome, date, episode)):
            out["/".join(parts[:4])] = (lab, outcome)
    return out


def order(
    episodes: dict[str, tuple[str, str]], seed=SEED
) -> list[tuple[str, str, str]]:
    """The whole release in the reference script's seeded round-robin across
    (lab, outcome); its first 500 are that script's ``selected_500``."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for episode, key in sorted(episodes.items()):
        groups[key].append(episode)
    rng = random.Random(seed)
    queues = {}
    for key, values in sorted(groups.items()):
        rng.shuffle(values)
        queues[key] = deque(values)
    keys = sorted(queues)
    out: list[tuple[str, str, str]] = []
    while len(out) < len(episodes):
        rng.shuffle(keys)
        for key in keys:
            if queues[key]:
                out.append((*key, queues[key].popleft()))
    return out


# ----------------------------------------------------------- one episode


def retained(relative: str) -> bool:
    """A file kept from an episode folder (path relative to it)."""
    parts = relative.split("/")
    if len(parts) == 1:
        return (
            parts[0].startswith("metadata_") and parts[0].endswith(".json")
        ) or parts[0] == "trajectory.h5"
    return (
        len(parts) == 3
        and parts[:2] == ["recordings", "MP4"]
        and parts[2].endswith(".mp4")
        and not parts[2].endswith("-stereo.mp4")
    )


def files_of(bucket: Bucket, episode: str, stop=None) -> list[dict]:
    base = f"{PREFIX}/{episode}/"
    out = []
    for item in bucket.objects(base, stop=stop):
        if "name" not in item:
            continue
        relative = item["name"][len(base) :]
        if retained(relative):
            out.append({**item, "path": relative})
    return sorted(out, key=lambda f: f["path"])


def listing_problem(files: list[dict]) -> str | None:
    """Why LEVI could not read this episode, from its file list alone."""
    names = [f["path"] for f in files]
    if sum(n.startswith("metadata_") for n in names) != 1:
        return "not exactly one metadata JSON"
    if "trajectory.h5" not in names:
        return "no trajectory.h5"
    if sum(n.startswith("recordings/") for n in names) < CAMERAS:
        return f"fewer than {CAMERAS} non-stereo MP4 cameras"
    if any(f["size"] == 0 for f in files):
        return "an empty file"
    return None


def check_files(destination: Path) -> None:
    """The reference script's structural check of a downloaded episode."""
    metadata = list(destination.glob("metadata_*.json"))
    videos = list((destination / "recordings" / "MP4").glob("*.mp4"))
    trajectory = destination / "trajectory.h5"
    if len(metadata) != 1 or not trajectory.is_file() or not videos:
        raise RuntimeError("missing metadata, trajectory.h5 or non-stereo MP4")
    if trajectory.stat().st_size == 0:
        raise RuntimeError("empty trajectory.h5")
    if any("stereo" in v.name.lower() or v.stat().st_size == 0 for v in videos):
        raise RuntimeError("unexpected stereo or empty MP4")
    if list(destination.rglob("*.svo")):
        raise RuntimeError("unexpected SVO file")
    json.loads(metadata[0].read_text())
    with trajectory.open("rb") as handle:
        if handle.read(8) != b"\x89HDF\r\n\x1a\n":
            raise RuntimeError("invalid HDF5 signature")


def trajectory_checked() -> bool:
    """Whether :func:`readable` checks the trajectory (needs h5py, the
    ``droid`` extra): a download-time replacement depends on it."""
    return importlib.util.find_spec("h5py") is not None


def readable(demo: Path) -> str | None:
    """What LEVI's own DROID reader says about one episode (None: fine)."""
    from ..conversion.inputs import droid_raw

    try:
        meta = droid_raw.metadata(demo)
        droid_raw.camera_paths(demo, meta)
        frames, _task, _outcome = droid_raw._validate_metadata(meta)
        if not trajectory_checked():
            return None
        droid_raw._validate_trajectory(demo, frames)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- progress


class Progress:
    """Throttled progress record the service and the CLI read."""

    def __init__(self, path: Path, total: int):
        self.path = path
        self.lock = threading.Lock()
        self.value = {
            "stage": "listing",
            "done": 0,
            "total": total,
            "bytes_done": 0,
            "bytes_total": 0,
            "replaced": 0,
            "current": "",
            "started_at": time.time(),
            "download_started_at": None,
            "updated_at": time.time(),
            "eta_seconds": None,
        }
        self.written = 0.0
        # Bytes this run transferred: the rate, whatever was on disk before.
        self.fetched = 0

    def set(self, force=False, **fields):
        with self.lock:
            self.value.update(fields)
            now = time.time()
            v = self.value
            v["updated_at"] = now
            if v["stage"] == "downloading":
                if v["download_started_at"] is None:
                    v["download_started_at"] = now
                v["eta_seconds"] = self._eta(now)
            else:
                v["eta_seconds"] = None
            if force or now - self.written >= 1:
                write_json(self.path, v)
                self.written = now

    def _eta(self, now) -> int | None:
        """Seconds left, from the rate since the downloads began: at least 1
        while bytes remain, 0 only when none do, None until measured."""
        v = self.value
        remaining = max(0, v["bytes_total"] - v["bytes_done"])
        if not remaining:
            return 0
        elapsed = now - v["download_started_at"]
        if self.fetched <= 0 or elapsed <= 5:
            return None
        return max(1, math.ceil(remaining / (self.fetched / elapsed)))

    def add_bytes(self, count):
        with self.lock:
            self.value["bytes_done"] += count
            self.fetched += count
        self.set()

    def adjust(self, done=0, total=0):
        """An episode swapped for another: its bytes leave, the new one's
        join the total."""
        with self.lock:
            self.value["bytes_done"] += done
            self.value["bytes_total"] += total
        self.set()


# -------------------------------------------------------------------- draw


def manifest_path(root: Path) -> Path:
    return root / "_meta" / "selected.json"


def plan_draw(bucket: Bucket, draw: dict, progress: Progress, stop=None) -> dict:
    """Assign an episode of the release to every index of this draw, with
    each episode's files (sizes and MD5s) -- or read the plan of a draw that
    is being resumed."""
    stop = stop or threading.Event()
    root = partial_root(draw["name"])
    saved = read_json(manifest_path(root), None)
    if saved:
        if (saved.get("draw"), saved.get("first"), saved.get("size")) != (
            draw["draw"],
            draw["first"],
            draw["size"],
        ):
            raise RuntimeError(
                f"{root} holds another draw's files (draw {saved.get('draw')}, "
                f"first {saved.get('first')}); move it away first"
            )
        return saved
    progress.set(force=True, stage="listing")
    listing = list_release(
        bucket,
        lambda n, lab: progress.set(current=f"{lab}: {n} episodes listed"),
        stop,
    )
    episodes = episodes_of(listing)
    sequence = order(episodes)
    first, size = draw["first"], draw["size"]
    if len(sequence) - first < size:
        raise RuntimeError(
            f"Only {len(sequence) - first} unused episodes left in the release"
        )
    progress.set(force=True, stage="selecting", current="")
    chosen: list[dict] = []
    replaced: list[dict] = []
    position = first
    # Candidates are taken in order; one LEVI could not read is skipped and
    # named in ``replaced``. File lists are fetched a window at a time.
    with _threads(8, stop) as pool:
        while len(chosen) < size:
            need = size - len(chosen)
            batch = sequence[position : position + need + max(8, need // 20)]
            if not batch:
                raise RuntimeError("The release has no episodes left to draw")
            listings = list(pool.map(lambda c: files_of(bucket, c[2], stop), batch))
            for (lab, outcome, episode), files in zip(batch, listings, strict=True):
                position += 1
                problem = listing_problem(files)
                if problem:
                    replaced.append(
                        {
                            "index": len(chosen),
                            "source": f"{SOURCE}/{episode}",
                            "reason": problem,
                        }
                    )
                    continue
                chosen.append(
                    {
                        "index": len(chosen),
                        "lab": lab,
                        "outcome": outcome,
                        "source": f"{SOURCE}/{episode}",
                        "episode": episode,
                        "files": files,
                    }
                )
                if len(chosen) == size:
                    break
            progress.set(done=len(chosen))
    plan = {
        "draw": draw["draw"],
        "name": draw["name"],
        "size": size,
        "seed": SEED,
        "first": first,
        "next": position,
        "available_episodes": len(sequence),
        "groups": _counts(episodes.values()),
        "episodes": chosen,
        "replaced": replaced,
    }
    write_json(manifest_path(root), plan)
    return plan


def _counts(keys) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for lab, outcome in keys:
        counts[f"{lab}/{outcome}"] += 1
    return dict(sorted(counts.items()))


def download_episode(
    bucket: Bucket, root: Path, item: dict, progress: Progress, stop=None
) -> str:
    destination = root / f"demo_{item['index']:04d}"
    done = destination / ".download_complete"
    if done.is_file():
        check_files(destination)
        return "already complete"
    destination.mkdir(parents=True, exist_ok=True)
    for f in item["files"]:
        _check(stop)
        target = destination / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if verified(target, f["size"], f["md5"]):
            continue
        bucket.fetch(
            f["name"], target, f["size"], f["md5"], progress.add_bytes, stop=stop
        )
    check_files(destination)
    problem = readable(destination)
    if problem:
        raise Unreadable(problem)
    for suffix, value in (
        ("source", item["source"]),
        ("lab", item["lab"]),
        ("outcome", item["outcome"]),
    ):
        (destination / f".droid_{suffix}").write_text(value + "\n")
    done.write_text("size and MD5 of every file match the bucket\n")
    return "downloaded and checked"


class Unreadable(RuntimeError):
    """Downloaded intact, but LEVI's DROID reader rejects it."""


def replace_unreadable(
    bucket: Bucket, plan: dict, index: int, reason: str, stop=None
) -> dict:
    """The next unused episode of the order takes an unreadable one's index.
    The candidates are checked first; the plan changes only once one is
    found, all at once, so a failed listing leaves it as it was."""
    sequence = order(episodes_of(listing_path().read_text().splitlines()))
    old = plan["episodes"][index]
    position = plan["next"]
    replaced = [{"index": index, "source": old["source"], "reason": reason}]
    while True:
        if position >= len(sequence):
            raise RuntimeError("The release has no episodes left to draw")
        lab, outcome, episode = sequence[position]
        position += 1
        files = files_of(bucket, episode, stop)
        problem = listing_problem(files)
        if problem is None:
            break
        replaced.append(
            {"index": index, "source": f"{SOURCE}/{episode}", "reason": problem}
        )
    plan["replaced"].extend(replaced)
    plan["next"] = position
    plan["episodes"][index] = {
        "index": index,
        "lab": lab,
        "outcome": outcome,
        "source": f"{SOURCE}/{episode}",
        "episode": episode,
        "files": files,
    }
    return plan


def episode_bytes(item: dict) -> int:
    return sum(f["size"] for f in item["files"])


def planned_bytes(plan: dict) -> int:
    return sum(episode_bytes(i) for i in plan["episodes"])


def remaining_bytes(root: Path, plan: dict) -> int:
    total = 0
    for item in plan["episodes"]:
        destination = root / f"demo_{item['index']:04d}"
        if (destination / ".download_complete").is_file():
            continue
        for f in item["files"]:
            target = destination / f["path"]
            if not (target.is_file() and target.stat().st_size == f["size"]):
                total += f["size"]
    return total


def write_selection(root: Path, plan: dict, report: dict | None = None) -> None:
    meta = root / "_meta"
    with (meta / "selected.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(("index", "lab", "outcome", "source"))
        for item in plan["episodes"]:
            writer.writerow(
                (item["index"], item["lab"], item["outcome"], item["source"])
            )
    selected = _counts((i["lab"], i["outcome"]) for i in plan["episodes"])
    write_json(
        meta / "selection.json",
        {
            "dataset": "DROID raw 1.0.1",
            "source": SOURCE,
            "source_documentation": DOCS,
            "sampling": (
                "seeded round-robin across (lab, outcome); no language or success "
                "filter; each draw takes the next unused positions of the same "
                "order (order_positions)"
            ),
            "seed": SEED,
            "draw": plan["draw"],
            "order_positions": [plan["first"], plan["next"]],
            "available_episodes": plan["available_episodes"],
            "selected_episodes": plan["size"],
            "available_groups": plan["groups"],
            "selected_groups": selected,
            "replaced": plan["replaced"],
            "trajectory_checked": trajectory_checked(),
            "retained_files": "metadata_*.json, trajectory.h5, non-stereo recordings/MP4/*.mp4",
            "excluded_files": "recordings/SVO/**, *-stereo.mp4",
            **({"verification": report} if report else {}),
        },
    )


def verify(root: Path, plan: dict) -> dict:
    problems, total, videos = [], 0, 0
    for item in plan["episodes"]:
        destination = root / f"demo_{item['index']:04d}"
        try:
            if not (destination / ".download_complete").is_file():
                raise RuntimeError("completion marker missing")
            check_files(destination)
            if (destination / ".droid_source").read_text().strip() != item["source"]:
                raise RuntimeError("source marker mismatch")
            total += sum(
                p.stat().st_size for p in destination.rglob("*") if p.is_file()
            )
            videos += len(list((destination / "recordings" / "MP4").glob("*.mp4")))
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append((item["index"], str(exc)))
    report = {
        "expected": plan["size"],
        "complete": plan["size"] - len(problems),
        "videos": videos,
        "bytes": total,
        "problems": problems,
    }
    write_json(root / "_meta" / "verification.json", report)
    return report


def on_disk() -> list[dict]:
    """The sample folders in place (``droid_raw_<size>_draw<NN>``), each with
    the ``_meta/selection.json`` it carries (None when unreadable)."""
    try:
        entries = sorted(ROOT.iterdir())
    except OSError:
        return []
    out = []
    for path in entries:
        match = NAME.match(path.name)
        if not match or not path.is_dir():
            continue
        out.append(
            {
                "name": path.name,
                "size": int(match[1]),
                "draw": int(match[2]),
                "path": path,
                "selection": read_json(path / "_meta" / "selection.json", None),
            }
        )
    return out


def _positions(selection) -> tuple[int, int] | None:
    if not isinstance(selection, dict):
        return None
    if selection.get("source") != SOURCE or selection.get("seed") != SEED:
        return None
    try:
        first, after = (int(x) for x in selection["order_positions"])
    except (KeyError, TypeError, ValueError):
        return None
    return (first, after) if 0 <= first <= after else None


def _complete(selection: dict) -> bool:
    report = selection.get("verification") or {}
    size = selection.get("selected_episodes")
    return (
        size is not None
        and report.get("expected") == size == report.get("complete")
        and not report.get("problems")
    )


def reconcile(v: dict) -> list[str]:
    """Bring the ledger (inside :func:`update_ledger`) in line with the sample
    folders in place, so no draw downloads again what the workspace holds:

    - an unfinished draw whose folder is in place, naming the same draw,
      first position and size, with a complete verification -- its process
      stopped between publishing (the rename) and recording ``ready`` -- is
      recorded ready;
    - a complete sample folder the ledger does not know (the ledger was lost,
      or the folder copied in) is recorded as a ready draw;
    - the cursor moves past the order positions of every such folder, so a
      later draw never takes them again.
    """
    notes = []
    for found in on_disk():
        selection = found["selection"]
        positions = _positions(selection)
        if positions is None:
            continue
        v["cursor"] = max(v.get("cursor", 0), positions[1])
        if not _complete(selection):
            continue
        ready = {
            "status": "ready",
            "path": str(found["path"]),
            "bytes": selection["verification"].get("bytes"),
            "replaced": len(selection.get("replaced") or []),
            "finished_at": time.time(),
            "pid": None,
            "identity": None,
        }
        entry = next((d for d in v["draws"] if d["name"] == found["name"]), None)
        if entry is not None:
            if (
                entry["status"] not in ("ready", "discarded")
                and selection.get("draw") == entry["draw"]
                and positions[0] == entry["first"]
                and selection.get("selected_episodes") == entry["size"]
            ):
                entry.update(ready, recovered="published before the ledger said so")
                notes.append(f"{found['name']}: published, recorded ready")
        elif selection.get("draw") == found["draw"] and not any(
            d["draw"] == found["draw"] for d in v["draws"]
        ):
            v["draws"].append(
                {
                    "draw": found["draw"],
                    "name": found["name"],
                    "size": selection["selected_episodes"],
                    "first": positions[0],
                    "reason": "found in the workspace",
                    "attempts": 0,
                    **ready,
                }
            )
            notes.append(f"{found['name']}: found in the workspace, recorded ready")
    return notes


def _identity():
    from ..children import identity

    return identity(os.getpid())


def run(draw_number: int, workers: int = 4, bucket: Bucket | None = None) -> dict:
    """Plan, download, verify and publish one draw. Resumes
    whatever an earlier attempt left in the partial folder."""
    bucket = bucket or Bucket()
    stop = threading.Event()
    with run_lock(wait=LOCK_WAIT) as held:
        if not held:
            raise RuntimeError("Another LEVI process is drawing a DROID sample")
        # A draw published just before its process stopped, or a sample the
        # ledger lost, is recorded before anything is planned or fetched.
        ledger = update_ledger(reconcile)
        if not any(d["draw"] == draw_number for d in ledger["draws"]):
            raise ValueError(
                f"No draw {draw_number} is planned; use `levi sample draw`"
            )
        draw = draw_entry(ledger, draw_number)
        name = draw["name"]
        if draw["status"] == "ready":
            return {
                "status": "ready",
                "path": draw.get("path") or str(final_root(name)),
                "bytes": draw.get("bytes"),
            }
        if draw["status"] == "discarded":
            raise ValueError(
                f"Draw {draw_number} ({name}) was discarded; "
                "`levi sample draw` plans a new one"
            )
        root = partial_root(name)
        progress = Progress(progress_path(name), draw["size"])
        set_draw(
            draw_number,
            status="listing",
            pid=os.getpid(),
            identity=_identity(),
            error=None,
            transient=None,
            started_at=time.time(),
        )
        try:
            (root / "_meta").mkdir(parents=True, exist_ok=True)
            plan = plan_draw(bucket, draw, progress, stop)
            need = remaining_bytes(root, plan)
            planned = planned_bytes(plan)
            set_draw(draw_number, bytes=planned, disk=disk(), status="downloading")
            progress.set(
                force=True,
                stage="downloading",
                done=sum(
                    (root / f"demo_{i:04d}" / ".download_complete").is_file()
                    for i in range(plan["size"])
                ),
                bytes_total=planned,
                bytes_done=planned - need,
                replaced=len(plan["replaced"]),
            )
            _download_all(bucket, root, plan, progress, workers, stop)
            progress.set(force=True, stage="verifying", current="")
            set_draw(draw_number, status="verifying")
            report = verify(root, plan)
            write_selection(root, plan, report)
            if report["problems"]:
                raise RuntimeError(
                    f"{len(report['problems'])} episodes failed verification"
                )
            final = final_root(name)
            if final.exists():
                raise RuntimeError(
                    f"{final} already exists; the sample stays in {root}"
                )
            # A stop between here and the ledger update is recovered by
            # reconcile() from the published _meta/selection.json.
            os.replace(root, final)
            progress.set(force=True, stage="ready", current=str(final))

            def done(v):
                entry = draw_entry(v, draw_number)
                entry.update(
                    status="ready",
                    path=str(final),
                    bytes=report["bytes"],
                    replaced=len(plan["replaced"]),
                    finished_at=time.time(),
                    pid=None,
                    identity=None,
                )
                v["cursor"] = max(v.get("cursor", 0), plan["next"])

            update_ledger(done)
            return {"status": "ready", "path": str(final), "bytes": report["bytes"]}
        except BaseException as exc:
            status = (
                "interrupted"
                if isinstance(exc, KeyboardInterrupt | SystemExit)
                else "failed"
            )
            progress.set(force=True, stage=status, current=str(exc)[-500:])
            set_draw(
                draw_number,
                status=status,
                error=str(exc)[-2000:],
                transient=isinstance(exc, NetworkError),
                pid=None,
                identity=None,
            )
            raise


def _download_all(bucket, root, plan, progress, workers, stop=None):
    stop = stop or threading.Event()
    lock = threading.Lock()
    failures: list[tuple[int, BaseException]] = []

    def one(index):
        while True:
            _check(stop)
            item = plan["episodes"][index]
            try:
                message = download_episode(bucket, root, item, progress, stop)
            except Unreadable as exc:
                shutil.rmtree(root / f"demo_{index:04d}", ignore_errors=True)
                old = episode_bytes(item)
                # Its files are gone: no longer done.
                progress.adjust(done=-old)
                with lock:
                    replace_unreadable(bucket, plan, index, str(exc), stop)
                    write_json(manifest_path(root), plan)
                    progress.adjust(total=episode_bytes(plan["episodes"][index]) - old)
                    progress.set(replaced=len(plan["replaced"]))
                    set_draw(plan["draw"], bytes=planned_bytes(plan))
                continue
            return message

    with _threads(workers, stop) as pool:
        futures = {pool.submit(one, i): i for i in range(plan["size"])}
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
                progress.set(current=f"demo_{index:04d}")
            except Exception as exc:  # noqa: BLE001 - reported per episode
                failures.append((index, exc))
            done = sum(
                (root / f"demo_{i:04d}" / ".download_complete").is_file()
                for i in range(plan["size"])
            )
            progress.set(done=done)
    failures.sort(key=lambda f: f[0])
    with (root / "_meta" / "download_failed.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(("index", "source", "error"))
        for index, error in failures:
            writer.writerow((index, plan["episodes"][index]["source"], str(error)))
    if failures:
        # Transient (resumed by the service) only when the network alone failed.
        kind = (
            NetworkError
            if all(isinstance(e, NetworkError) for _, e in failures)
            else RuntimeError
        )
        raise kind(
            f"{len(failures)} episodes failed ({failures[0][1]}); drawing again resumes"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m levi.samples", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run", help="draw (or resume) one planned draw")
    one.add_argument("--draw", type=int, required=True)
    one.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be 1-8")
    try:
        result = run(args.draw, args.workers)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - the ledger keeps the error
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
