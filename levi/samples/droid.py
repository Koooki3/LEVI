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
  The first draw is that script's ``selected_500``; draw *k* takes the next
  episodes of the same order, so draws never overlap.
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
import fcntl
import hashlib
import json
import os
import random
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
def _flock(path: Path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_lock():
    """Held by the process that is drawing, for as long as it draws."""
    return _flock(home() / f"{KEY}.run.lock", blocking=False)


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
    """Free and total bytes where the sample goes -- for the status only; a
    draw of 500 episodes (18-30 GB) is not gated on it."""
    usage = shutil.disk_usage(ROOT)
    return {"free": usage.free, "total": usage.total}


def gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


# ------------------------------------------------------------------- bucket


class Bucket:
    """Anonymous, read-only access to the public bucket (JSON API)."""

    def __init__(self, attempts=6):
        self.attempts = attempts

    def _open(self, url, timeout):
        for attempt in range(self.attempts):
            try:
                return urllib.request.urlopen(url, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 404) or attempt == self.attempts - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.attempts - 1:
                    raise
            time.sleep(min(30, 2**attempt))
        raise RuntimeError("unreachable")

    def objects(self, prefix, glob=None, delimiter=None):
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
            with self._open(API + "?" + urllib.parse.urlencode(params), 90) as r:
                page = json.loads(r.read())
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

    def fetch(self, name, dest: Path, size: int, md5: str | None, on_bytes=None):
        """Download one object; the file appears only once size and MD5 match."""
        url = MEDIA + urllib.parse.quote(name)
        temporary = dest.with_name(dest.name + ".part")
        last = None
        for attempt in range(self.attempts):
            digest, count = hashlib.md5(), 0
            try:
                with self._open(url, 120) as response, temporary.open("wb") as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
            except (OSError, urllib.error.URLError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                got = base64.b64encode(digest.digest()).decode()
                if count == size and (md5 is None or got == md5):
                    os.replace(temporary, dest)
                    if on_bytes:
                        on_bytes(count)
                    return
                last = f"size {count}/{size}, md5 {got}/{md5}"
            time.sleep(min(30, 2**attempt))
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{name.rsplit('/', 1)[-1]}: download failed ({last})")


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


def list_release(bucket: Bucket, progress=None) -> list[str]:
    """Every episode's metadata object, relative to the release root:
    ``lab/outcome/date/episode/metadata_*.json`` -- kept with the ledger."""
    path = listing_path()
    if path.is_file() and path.stat().st_size:
        return path.read_text().splitlines()
    labs = [
        item["prefix"]
        for item in bucket.objects(PREFIX + "/", delimiter="/")
        if "prefix" in item
    ]
    found: list[str] = []
    lock = threading.Lock()

    def one(lab):
        rows = [
            item["name"][len(PREFIX) + 1 :]
            for item in bucket.objects(lab, glob="**/metadata_*.json")
            if "name" in item
        ]
        with lock:
            found.extend(rows)
            if progress:
                progress(len(found), lab.rstrip("/").rsplit("/", 1)[-1])

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(labs)))) as pool:
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


def files_of(bucket: Bucket, episode: str) -> list[dict]:
    base = f"{PREFIX}/{episode}/"
    out = []
    for item in bucket.objects(base):
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


def readable(demo: Path) -> str | None:
    """What LEVI's own DROID reader says about one episode (None: fine)."""
    from ..conversion.inputs import droid_raw

    try:
        meta = droid_raw.metadata(demo)
        droid_raw.camera_paths(demo, meta)
        frames, _task, _outcome = droid_raw._validate_metadata(meta)
        try:
            import h5py  # noqa: F401
        except ImportError:
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
            "updated_at": time.time(),
            "eta_seconds": None,
        }
        self.written = 0.0
        self.bytes_at_start = 0

    def set(self, force=False, **fields):
        with self.lock:
            self.value.update(fields)
            now = time.time()
            v = self.value
            v["updated_at"] = now
            fetched = v["bytes_done"] - self.bytes_at_start
            elapsed = now - v["started_at"]
            if v["stage"] == "downloading" and fetched > 0 and elapsed > 5:
                rate = fetched / elapsed
                v["eta_seconds"] = round(
                    max(0, v["bytes_total"] - v["bytes_done"]) / rate
                )
            if force or now - self.written >= 1:
                write_json(self.path, v)
                self.written = now

    def add_bytes(self, count):
        with self.lock:
            self.value["bytes_done"] += count
        self.set()


# -------------------------------------------------------------------- draw


def manifest_path(root: Path) -> Path:
    return root / "_meta" / "selected.json"


def plan_draw(bucket: Bucket, draw: dict, progress: Progress) -> dict:
    """Assign an episode of the release to every index of this draw, with
    each episode's files (sizes and MD5s) -- or read the plan of a draw that
    is being resumed."""
    root = partial_root(draw["name"])
    saved = read_json(manifest_path(root), None)
    if saved:
        return saved
    progress.set(force=True, stage="listing")
    listing = list_release(
        bucket, lambda n, lab: progress.set(current=f"{lab}: {n} episodes listed")
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
    # Candidate k takes index k unless LEVI could not read it, so the draw is
    # the same on every machine; file lists are fetched a window at a time.
    with ThreadPoolExecutor(max_workers=8) as pool:
        while len(chosen) < size:
            need = size - len(chosen)
            batch = sequence[position : position + need + max(8, need // 20)]
            if not batch:
                raise RuntimeError("The release has no episodes left to draw")
            listings = list(pool.map(lambda c: files_of(bucket, c[2]), batch))
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


def download_episode(bucket: Bucket, root: Path, item: dict, progress: Progress) -> str:
    destination = root / f"demo_{item['index']:04d}"
    done = destination / ".download_complete"
    if done.is_file():
        check_files(destination)
        return "already complete"
    destination.mkdir(parents=True, exist_ok=True)
    for f in item["files"]:
        target = destination / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if verified(target, f["size"], f["md5"]):
            continue
        bucket.fetch(f["name"], target, f["size"], f["md5"], progress.add_bytes)
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


def replace_unreadable(bucket: Bucket, plan: dict, index: int, reason: str) -> dict:
    """The next unused episode of the order takes an unreadable one's index."""
    sequence = order(episodes_of(listing_path().read_text().splitlines()))
    old = plan["episodes"][index]
    plan["replaced"].append({"index": index, "source": old["source"], "reason": reason})
    while True:
        if plan["next"] >= len(sequence):
            raise RuntimeError("The release has no episodes left to draw")
        lab, outcome, episode = sequence[plan["next"]]
        plan["next"] += 1
        files = files_of(bucket, episode)
        problem = listing_problem(files)
        if problem is None:
            plan["episodes"][index] = {
                "index": index,
                "lab": lab,
                "outcome": outcome,
                "source": f"{SOURCE}/{episode}",
                "episode": episode,
                "files": files,
            }
            return plan
        plan["replaced"].append(
            {"index": index, "source": f"{SOURCE}/{episode}", "reason": problem}
        )


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
                "filter; draw k continues the same order after draw k-1"
            ),
            "seed": SEED,
            "draw": plan["draw"],
            "order_positions": [plan["first"], plan["next"]],
            "available_episodes": plan["available_episodes"],
            "selected_episodes": plan["size"],
            "available_groups": plan["groups"],
            "selected_groups": selected,
            "replaced": plan["replaced"],
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


def run(draw_number: int, workers: int = 4, bucket: Bucket | None = None) -> dict:
    """Plan, download, verify and publish one draw. Resumes
    whatever an earlier attempt left in the partial folder."""
    bucket = bucket or Bucket()
    with run_lock() as held:
        if not held:
            raise RuntimeError("Another LEVI process is drawing a DROID sample")
        ledger = read_json(ledger_path(), {"draws": []})
        if not any(d["draw"] == draw_number for d in ledger["draws"]):
            raise ValueError(
                f"No draw {draw_number} is planned; use `levi sample draw`"
            )
        draw = draw_entry(ledger, draw_number)
        name = draw["name"]
        root = partial_root(name)
        progress = Progress(progress_path(name), draw["size"])
        set_draw(
            draw_number,
            status="listing",
            pid=os.getpid(),
            error=None,
            started_at=time.time(),
        )
        try:
            (root / "_meta").mkdir(parents=True, exist_ok=True)
            plan = plan_draw(bucket, draw, progress)
            need = remaining_bytes(root, plan)
            planned = sum(f["size"] for i in plan["episodes"] for f in i["files"])
            set_draw(draw_number, bytes=planned, disk=disk())
            set_draw(draw_number, status="downloading")
            progress.set(
                force=True,
                stage="downloading",
                done=sum(
                    (root / f"demo_{i:04d}" / ".download_complete").is_file()
                    for i in range(plan["size"])
                ),
                bytes_total=planned,
                bytes_done=planned - need,
            )
            progress.bytes_at_start = planned - need
            _download_all(bucket, root, plan, progress, workers)
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
            set_draw(draw_number, status=status, error=str(exc)[-2000:], pid=None)
            raise


def _download_all(bucket, root, plan, progress, workers):
    lock = threading.Lock()
    failures: list[tuple[int, str]] = []

    def one(index):
        while True:
            item = plan["episodes"][index]
            try:
                message = download_episode(bucket, root, item, progress)
            except Unreadable as exc:
                shutil.rmtree(root / f"demo_{index:04d}", ignore_errors=True)
                with lock:
                    replace_unreadable(bucket, plan, index, str(exc))
                    write_json(manifest_path(root), plan)
                    progress.set(replaced=len(plan["replaced"]))
                continue
            return message

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, i): i for i in range(plan["size"])}
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
                progress.set(current=f"demo_{index:04d}")
            except Exception as exc:  # noqa: BLE001 - reported per episode
                failures.append((index, str(exc)))
            done = sum(
                (root / f"demo_{i:04d}" / ".download_complete").is_file()
                for i in range(plan["size"])
            )
            progress.set(done=done)
    with (root / "_meta" / "download_failed.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(("index", "source", "error"))
        for index, error in sorted(failures):
            writer.writerow((index, plan["episodes"][index]["source"], error))
    if failures:
        raise RuntimeError(
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
