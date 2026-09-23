"""The DROID raw test sample a workspace draws: reproducible (the reference
subset script's selection), resumable, verified file by file, never
overlapping earlier draws. The public bucket is replaced by an in-memory one; nothing is downloaded."""

import base64
import errno
import hashlib
import http.client
import io
import json
import logging
import pathlib
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from collections import defaultdict, deque
from pathlib import Path

import pytest
from test_formats import droid_fixture

from levi.samples import droid

LABS = ("AUTOLab", "IRIS", "RAIL")
OUTCOMES = ("failure", "success")


class FakeBucket:
    """The public bucket's layout: lab/outcome/date/episode/..."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects_ = objects
        self.fetched: list[str] = []
        self.corrupt: set[str] = set()
        self.delay = 0.0  # seconds per file, in five "chunks"
        self.threads: set[threading.Thread] = set()

    def objects(self, prefix, glob=None, delimiter=None, stop=None):
        if delimiter:
            seen = set()
            for name in sorted(self.objects_):
                if name.startswith(prefix):
                    head = name[len(prefix) :].split(delimiter, 1)
                    if len(head) == 2 and head[0] not in seen:
                        seen.add(head[0])
                        yield {"prefix": prefix + head[0] + delimiter}
            return
        for name in sorted(self.objects_):
            if not name.startswith(prefix):
                continue
            if glob and not name.rsplit("/", 1)[-1].startswith("metadata_"):
                continue
            data = self.objects_[name]
            yield {
                "name": name,
                "size": len(data),
                "md5": base64.b64encode(hashlib.md5(data).digest()).decode(),
            }

    def fetch(self, name, dest: Path, size, md5, on_bytes=None, stop=None):
        self.threads.add(threading.current_thread())
        for _ in range(5):
            if stop is not None and stop.is_set():
                raise droid.Stopped("stopping")
            time.sleep(self.delay / 5)
        self.fetched.append(name)
        data = self.objects_[name]
        if name in self.corrupt:
            raise RuntimeError(f"{name}: download failed (md5 mismatch)")
        dest.write_bytes(data)
        if on_bytes:
            on_bytes(len(data))


@pytest.fixture(scope="module")
def episode_files(tmp_path_factory):
    """One readable DROID episode's files (LEVI's own fixture)."""
    demo = droid_fixture(tmp_path_factory.mktemp("droid")) / "demo_0000"
    return {
        "trajectory.h5": (demo / "trajectory.h5").read_bytes(),
        **{
            f"recordings/MP4/{n}.mp4": (
                demo / "recordings/MP4" / f"{n}.mp4"
            ).read_bytes()
            for n in ("101", "102", "103")
        },
    }


def release(episode_files, per_group=4, broken=()):
    """3 labs x 2 outcomes x ``per_group`` episodes, each with the files
    LEVI keeps and the ones it must not download (stereo MP4, SVO)."""
    objects = {}
    for lab in LABS:
        for outcome in OUTCOMES:
            for k in range(per_group):
                episode = f"{lab}/{outcome}/2023-07-0{k + 1}/Fri_Jul__{k}_09:50:13_2023"
                base = f"{droid.PREFIX}/{episode}/"
                meta = {
                    "trajectory_length": 20,
                    "current_task": "Move the cup",
                    "success": outcome == "success",
                    "building": lab,
                    "wrist_mp4_path": "x/101.mp4",
                    "ext1_mp4_path": "x/102.mp4",
                    "ext2_mp4_path": "x/103.mp4",
                }
                if episode in broken:
                    meta["success"] = "yes"  # LEVI's reader rejects it
                objects[base + f"metadata_{lab}+{k}.json"] = json.dumps(meta).encode()
                for path, data in episode_files.items():
                    objects[base + path] = data
                objects[base + "recordings/MP4/101-stereo.mp4"] = b"stereo"
                objects[base + "recordings/SVO/101.svo"] = b"svo"
    return objects


def reference_selection(urls: list[str], n: int, seed=42):
    """The pasted reference script's select(), verbatim in logic."""
    episodes = {}
    for url in urls:
        parts = url.split("/")
        lab, outcome, _date, _episode, _ = parts
        episodes["/".join(parts[:4])] = (lab, outcome)
    groups = defaultdict(list)
    for url, key in sorted(episodes.items()):
        groups[key].append(url)
    rng = random.Random(seed)
    queues = {}
    for key, values in sorted(groups.items()):
        rng.shuffle(values)
        queues[key] = deque(values)
    keys = sorted(queues)
    selected = []
    while len(selected) < n:
        rng.shuffle(keys)
        for key in keys:
            if queues[key]:
                selected.append((*key, queues[key].popleft()))
                if len(selected) == n:
                    break
    return selected


def new_draw(size):
    from levi import samples

    return samples.plan(size)


def test_order_starts_with_the_reference_selection_and_covers_the_release():
    rng = random.Random(7)
    urls = [
        f"{lab}/{outcome}/2023-0{rng.randint(1, 9)}-01/ep{i}/metadata_{i}.json"
        for i, (lab, outcome) in enumerate(
            (rng.choice(LABS + ("TRI", "WEIRD")), rng.choice(OUTCOMES))
            for _ in range(300)
        )
    ]
    sequence = droid.order(droid.episodes_of(urls))
    assert sequence[:120] == reference_selection(urls, 120)
    assert len(sequence) == len(set(sequence)) == 300


def test_draws_are_disjoint_complete_and_keep_only_what_levi_reads(episode_files):
    bucket = FakeBucket(release(episode_files))
    first = new_draw(5)
    result = droid.run(first["draw"], workers=2, bucket=bucket)
    assert result["status"] == "ready"
    root = Path(result["path"])
    assert root.name == "droid_raw_5_draw01"
    assert not droid.partial_root(root.name).exists()
    demos = sorted(p.name for p in root.glob("demo_*"))
    assert demos == [f"demo_{i:04d}" for i in range(5)]
    kept = {
        str(p.relative_to(root / "demo_0000"))
        for p in (root / "demo_0000").rglob("*")
        if p.is_file()
    }
    assert "recordings/MP4/101-stereo.mp4" not in kept and not any(
        k.endswith(".svo") for k in kept
    )
    assert {"trajectory.h5", ".download_complete", ".droid_source"} <= kept
    assert not any("stereo" in n or "SVO" in n for n in bucket.fetched)
    selection = json.loads((root / "_meta/selection.json").read_text())
    assert selection["verification"]["complete"] == 5 and not selection["replaced"]
    # The same order as the reference script, over the same listing.
    listing = [n[len(droid.PREFIX) + 1 :] for n in bucket.objects_ if "/metadata_" in n]
    expected = [f"{droid.SOURCE}/{e}" for *_, e in reference_selection(listing, 5)]
    tsv = (root / "_meta/selected.tsv").read_text().splitlines()[1:]
    assert [row.split("\t")[3] for row in tsv] == expected
    # LEVI can read the capture as DROID raw.
    from levi.conversion import registry

    assert registry.detect(root).id == "droid_raw"
    second = new_draw(5)
    again = droid.run(second["draw"], workers=2, bucket=bucket)
    assert Path(again["path"]).name == "droid_raw_5_draw02"
    sources = lambda r: {
        line.split("\t")[3]
        for line in (Path(r) / "_meta/selected.tsv").read_text().splitlines()[1:]
    }
    assert not sources(result["path"]) & sources(again["path"])
    ledger = json.loads(droid.ledger_path().read_text())
    assert [d["status"] for d in ledger["draws"]] == ["ready", "ready"]
    assert ledger["cursor"] == 10


def test_an_episode_levi_cannot_read_is_replaced_by_the_next(episode_files):
    listing = [
        n[len(droid.PREFIX) + 1 :] for n in release(episode_files) if "/metadata_" in n
    ]
    first = reference_selection(listing, 1)[0][2]
    bucket = FakeBucket(release(episode_files, broken={first}))
    result = droid.run(new_draw(3)["draw"], workers=1, bucket=bucket)
    root = Path(result["path"])
    selection = json.loads((root / "_meta/selection.json").read_text())
    assert [r["source"] for r in selection["replaced"]] == [f"{droid.SOURCE}/{first}"]
    assert "success must be an explicit boolean" in selection["replaced"][0]["reason"]
    assert len(list(root.glob("demo_*/.download_complete"))) == 3
    # The replacement took the next position, so the next draw starts after it.
    assert json.loads(droid.ledger_path().read_text())["cursor"] == 4


def test_an_interrupted_draw_resumes_without_fetching_again(episode_files):
    objects = release(episode_files)
    bucket = FakeBucket(objects)
    draw = new_draw(4)
    listing = [n[len(droid.PREFIX) + 1 :] for n in objects if "/metadata_" in n]
    last = reference_selection(listing, 4)[3][2]
    bucket.corrupt = {n for n in objects if n.startswith(f"{droid.PREFIX}/{last}/traj")}
    with pytest.raises(RuntimeError, match="1 episodes failed"):
        droid.run(draw["draw"], workers=1, bucket=bucket)
    partial = droid.partial_root(draw["name"])
    assert len(list(partial.glob("demo_*/.download_complete"))) == 3
    from levi import samples

    assert samples.status()["droid_raw"]["draws"][0]["status"] == "failed"
    # The same draw resumes: only the missing episode's files are fetched.
    assert samples.plan(4)["draw"] == draw["draw"]
    bucket.corrupt, bucket.fetched = set(), []
    result = droid.run(draw["draw"], workers=1, bucket=bucket)
    assert result["status"] == "ready"
    assert bucket.fetched and all(f"/{last}/" in n for n in bucket.fetched)


def fake_start(monkeypatch):
    """samples.start without a worker process: plans the draw and records why."""
    from levi import samples

    calls = []

    def start(size=droid.SIZE, workers=None, reason="manual", automatic=False):
        calls.append(reason)
        return samples.plan(size, reason, automatic)

    monkeypatch.setattr(samples, "start", start)
    return calls


def test_a_new_workspace_is_offered_a_sample_once_and_only_when_allowed(monkeypatch):
    from levi import samples

    calls = fake_start(monkeypatch)
    samples.note_new_workspace()
    samples.on_service_start()  # LEVI_DROID_SAMPLE=off in tests
    assert not calls
    monkeypatch.setenv("LEVI_DROID_SAMPLE", "auto")
    samples.on_service_start()
    assert calls == ["new workspace"]
    assert json.loads(droid.ledger_path().read_text())["auto"] == "started"
    # Offered once; the draw it planned is resumed on the next start instead.
    samples.on_service_start()
    assert calls == ["new workspace", "resume after restart"]
    assert len(json.loads(droid.ledger_path().read_text())["draws"]) == 1
    # At most three automatic attempts per draw.
    samples.on_service_start()
    samples.on_service_start()
    assert calls == ["new workspace"] + ["resume after restart"] * 2


def test_a_draw_by_hand_answers_the_offer(monkeypatch):
    from levi import samples

    calls = fake_start(monkeypatch)
    samples.note_new_workspace()
    draw = samples.plan(3, "levi sample draw")
    droid.set_draw(draw["draw"], status="ready")
    monkeypatch.setenv("LEVI_DROID_SAMPLE", "auto")
    samples.on_service_start()
    assert not calls


def test_off_means_no_download_starts_or_resumes_by_itself(monkeypatch):
    from levi import samples

    calls = fake_start(monkeypatch)
    samples.plan(3)  # planned, as if the last core stopped mid-draw
    samples.on_service_start()
    assert not calls
    assert samples.status()["droid_raw"]["draws"][0]["status"] == "interrupted"


def test_a_discarded_draw_frees_its_positions_and_its_folder():
    from levi import samples

    first = samples.plan(3)
    partial = droid.partial_root(first["name"])
    (partial / "demo_0000").mkdir(parents=True)
    (partial / "demo_0000" / "trajectory.h5").write_bytes(b"x")
    assert samples.plan(3)["draw"] == first["draw"]  # unfinished: resumed
    result = samples.cancel(discard=True)
    assert result["discarded"] == str(partial) and not partial.exists()
    again = samples.plan(3)
    assert again["draw"] == first["draw"] + 1 and again["first"] == first["first"]
    statuses = [d["status"] for d in samples.status()["droid_raw"]["draws"]]
    assert statuses == ["discarded", "interrupted"]


def test_sync_registers_the_finished_sample_and_never_the_partial(
    episode_files, client, monkeypatch, tmp_path
):
    from test_views import wait

    from levi import catalog
    from levi.sync import Synchronizer

    monkeypatch.setenv("LEVI_SYNC_SETTLE", "0")
    bucket = FakeBucket(release(episode_files))
    result = droid.run(new_draw(2)["draw"], workers=1, bucket=bucket)
    # A later draw, still downloading.
    partial = droid.partial_root("droid_raw_2_draw02")
    shutil.copytree(Path(result["path"]) / "demo_0000", partial / "demo_0000")
    changes = Synchronizer(root=tmp_path).scan()
    assert [(c["kind"], c.get("name")) for c in changes] == [
        ("added", "droid_raw_2_draw01")
    ]
    entry = catalog.datasets()["droid_raw_2_draw01"]
    assert entry["input_format"] == "droid_raw"
    assert wait(client, entry["view_job"])["status"] == "succeeded"


def test_the_retained_files_are_the_reference_filters():
    assert droid.retained("metadata_AUTOLab+x.json")
    assert droid.retained("trajectory.h5")
    assert droid.retained("recordings/MP4/22008760.mp4")
    assert not droid.retained("recordings/MP4/22008760-stereo.mp4")
    assert not droid.retained("recordings/SVO/22008760.svo")
    assert not droid.retained("trajectory_im128.h5")


def test_only_creating_a_workspace_marks_it(monkeypatch, tmp_path):
    from levi import paths, samples

    marked = []
    monkeypatch.setattr(samples, "note_new_workspace", lambda: marked.append(1))
    monkeypatch.setattr(paths, "STATE", tmp_path / "new" / "outputs/LEVI/workbench")
    paths.configure()
    assert marked == [1]
    paths.configure()  # it exists now
    assert marked == [1]


# ------------------------------------------------ stopping (Ctrl-C, cancel)


def test_ctrl_c_stops_the_draw_and_its_threads_before_the_lock_is_released(
    episode_files, monkeypatch
):
    bucket = FakeBucket(release(episode_files))
    bucket.delay = 0.05
    draw = new_draw(20)
    real = droid.Progress.set

    def interrupt_at_first_episode(self, force=False, **fields):
        # What a Ctrl-C does: KeyboardInterrupt in the main thread.
        if threading.current_thread() is threading.main_thread() and str(
            fields.get("current", "")
        ).startswith("demo_"):
            raise KeyboardInterrupt
        return real(self, force, **fields)

    monkeypatch.setattr(droid.Progress, "set", interrupt_at_first_episode)
    with pytest.raises(KeyboardInterrupt):
        droid.run(draw["draw"], workers=2, bucket=bucket)
    fetched = len(bucket.fetched)
    assert fetched < 20, "the queued episodes were not downloaded"
    assert not any(t.is_alive() for t in bucket.threads)
    time.sleep(0.3)
    assert len(bucket.fetched) == fetched
    with droid.run_lock() as free:
        assert free
    ledger = json.loads(droid.ledger_path().read_text())
    assert ledger["draws"][0]["status"] == "interrupted"


def test_a_second_ctrl_c_still_waits_for_the_threads(monkeypatch):
    stop = threading.Event()
    finished = []

    def work():
        stop.wait(5)
        time.sleep(0.2)
        finished.append(1)

    joins = []
    real = droid.ThreadPoolExecutor.shutdown

    def shutdown(self, wait=True, *, cancel_futures=False):
        if wait and not joins:
            joins.append(1)
            raise KeyboardInterrupt  # the second Ctrl-C, during the join
        return real(self, wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(droid.ThreadPoolExecutor, "shutdown", shutdown)
    with pytest.raises(KeyboardInterrupt), droid._threads(1, stop) as pool:
        pool.submit(work)
        time.sleep(0.05)
        raise KeyboardInterrupt
    assert finished == [1] and stop.is_set()


HOLDER = (
    "import fcntl, sys, time\n"
    "h = open(sys.argv[1], 'a')\n"
    "fcntl.flock(h, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(60)\n"
)


def foreground_draw(identity_ok=True):
    """A process LEVI did not start holding the run lock, recorded in the
    ledger the way run() records itself."""
    from levi import children, samples

    draw = samples.plan(3)
    partial = droid.partial_root(draw["name"])
    (partial / "demo_0000").mkdir(parents=True)
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(droid.home() / f"{droid.KEY}.run.lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "held"
    who = children.identity(process.pid)
    if not identity_ok:
        who = {**who, "start_ticks": "1"}
    droid.set_draw(draw["draw"], status="downloading", pid=process.pid, identity=who)
    return draw, partial, process


def test_cancel_stops_a_foreground_draw_it_can_identify_then_discards():
    from levi import samples

    draw, partial, process = foreground_draw()
    try:
        result = samples.cancel(discard=True, grace=5)
        assert process.wait(timeout=5) is not None
        assert result["discarded"] == str(partial) and not partial.exists()
        assert result["stopped"] == [f"{draw['name']} (pid {process.pid})"]
        ledger = json.loads(droid.ledger_path().read_text())
        assert ledger["draws"][0]["status"] == "discarded"
    finally:
        process.kill()
        process.wait()


def test_cancel_never_touches_a_draw_it_cannot_stop(client):
    _draw, partial, process = foreground_draw(identity_ok=False)
    try:
        from levi import samples

        with pytest.raises(RuntimeError, match="cannot identify"):
            samples.cancel(discard=True, grace=1)
        response = client.post("/api/levi/samples/droid_raw/cancel?discard=true")
        assert response.status_code == 409
        assert process.poll() is None, "not signalled"
        assert (partial / "demo_0000").is_dir()
        ledger = json.loads(droid.ledger_path().read_text())
        assert ledger["draws"][0]["status"] == "downloading"
    finally:
        process.kill()
        process.wait()


def test_the_run_lock_waits_out_a_short_holder():
    droid.home().mkdir(parents=True, exist_ok=True)
    held = threading.Event()

    def probe():
        with droid.run_lock() as free:
            assert free
            held.set()
            time.sleep(0.3)

    thread = threading.Thread(target=probe)
    thread.start()
    held.wait(2)
    with droid.run_lock() as free:
        assert not free
    with droid.run_lock(wait=3) as free:
        assert free
    thread.join()


# --------------------------------------- publishing, lost ledgers, identity


def test_a_draw_published_before_its_ledger_update_is_recorded_not_redrawn(
    episode_files,
):
    from levi import samples

    bucket = FakeBucket(release(episode_files))
    draw = new_draw(2)
    result = droid.run(draw["draw"], workers=1, bucket=bucket)
    selection = json.loads((Path(result["path"]) / "_meta/selection.json").read_text())

    def stopped_after_the_rename(v):
        v["cursor"] = 0
        droid.draw_entry(v, draw["draw"]).update(status="verifying", path=None)

    droid.update_ledger(stopped_after_the_rename)
    bucket.fetched = []
    again = droid.run(draw["draw"], workers=1, bucket=bucket)
    assert again["status"] == "ready" and again["path"] == result["path"]
    assert not bucket.fetched and not droid.partial_root(draw["name"]).exists()
    ledger = json.loads(droid.ledger_path().read_text())
    assert ledger["draws"][0]["status"] == "ready"
    assert ledger["cursor"] == selection["order_positions"][1]
    # plan() does the same, so a new draw never reuses the positions.
    droid.update_ledger(stopped_after_the_rename)
    nxt = samples.plan(2)
    assert nxt["draw"] == 2 and nxt["first"] == selection["order_positions"][1]


def test_a_sample_whose_ledger_was_lost_is_never_drawn_again(
    episode_files, monkeypatch
):
    from levi import samples

    bucket = FakeBucket(release(episode_files))
    result = droid.run(new_draw(3)["draw"], workers=1, bucket=bucket)
    positions = json.loads((Path(result["path"]) / "_meta/selection.json").read_text())[
        "order_positions"
    ]
    shutil.rmtree(droid.home())  # the ledger is lost; the folder stays
    samples.note_new_workspace()
    calls = fake_start(monkeypatch)
    monkeypatch.setenv("LEVI_DROID_SAMPLE", "auto")
    samples.on_service_start()
    assert not calls, "a workspace that holds a sample is not offered another"
    ledger = json.loads(droid.ledger_path().read_text())
    assert [(d["name"], d["status"]) for d in ledger["draws"]] == [
        ("droid_raw_3_draw01", "ready")
    ]
    nxt = samples.plan(500)
    assert nxt["name"] == "droid_raw_500_draw02" and nxt["first"] == positions[1]


def test_the_selection_records_whether_the_trajectory_was_checked(episode_files):
    bucket = FakeBucket(release(episode_files))
    result = droid.run(new_draw(1)["draw"], workers=1, bucket=bucket)
    selection = json.loads((Path(result["path"]) / "_meta/selection.json").read_text())
    assert selection["trajectory_checked"] is droid.trajectory_checked()
    assert "next unused positions" in selection["sampling"]


# ------------------------------------------------ replacement and progress


class UnreachableBucket(FakeBucket):
    def objects(self, prefix, glob=None, delimiter=None, stop=None):
        raise droid.NetworkError("listing failed after 6 attempts")


def test_a_failed_replacement_leaves_the_plan_untouched(episode_files):
    import copy

    objects = release(episode_files)
    listing = [n[len(droid.PREFIX) + 1 :] for n in objects if "/metadata_" in n]
    droid.listing_path().parent.mkdir(parents=True, exist_ok=True)
    droid.listing_path().write_text("\n".join(listing) + "\n")
    first = reference_selection(listing, 1)[0][2]
    plan = {
        "episodes": [{"index": 0, "source": f"{droid.SOURCE}/{first}"}],
        "replaced": [],
        "next": 1,
    }
    before = copy.deepcopy(plan)
    with pytest.raises(droid.NetworkError):
        droid.replace_unreadable(UnreachableBucket(objects), plan, 0, "unreadable")
    assert plan == before
    droid.replace_unreadable(FakeBucket(objects), plan, 0, "unreadable")
    assert plan["next"] == 2 and len(plan["replaced"]) == 1
    assert plan["episodes"][0]["source"] != before["episodes"][0]["source"]


def test_progress_counts_every_replacement_and_keeps_bytes_consistent(episode_files):
    listing = [
        n[len(droid.PREFIX) + 1 :] for n in release(episode_files) if "/metadata_" in n
    ]
    picked = reference_selection(listing, 2)
    broken, missing = picked[0][2], picked[1][2]
    objects = release(episode_files, broken={broken})
    # One candidate lacks a camera: replaced while selecting.
    del objects[f"{droid.PREFIX}/{missing}/recordings/MP4/103.mp4"]
    result = droid.run(new_draw(3)["draw"], workers=1, bucket=FakeBucket(objects))
    progress = json.loads(droid.progress_path("droid_raw_3_draw01").read_text())
    assert progress["replaced"] == 2
    plan = json.loads((Path(result["path"]) / "_meta/selected.json").read_text())
    assert (
        progress["bytes_done"] == progress["bytes_total"] == droid.planned_bytes(plan)
    )


def test_the_time_left_counts_from_the_downloads_not_the_listing(tmp_path):
    progress = droid.Progress(tmp_path / "p.json", 10)
    progress.value["started_at"] = time.time() - 3600  # a long listing
    progress.set(stage="downloading", bytes_total=1000, bytes_done=0)
    assert progress.value["eta_seconds"] is None  # nothing measured yet
    progress.value["download_started_at"] = time.time() - 10
    progress.add_bytes(100)
    assert 85 <= progress.value["eta_seconds"] <= 95
    progress.add_bytes(899)
    assert progress.value["eta_seconds"] >= 1  # bytes remain
    progress.add_bytes(1)
    assert progress.value["eta_seconds"] == 0


# --------------------------------------------------------------- network


class Response(io.BytesIO):
    def __init__(self, data=b"", fail_first_read=False):
        super().__init__(data)
        self.fail_first_read = fail_first_read

    def read(self, *args):
        if self.fail_first_read:
            self.fail_first_read = False
            raise http.client.IncompleteRead(b"")
        return super().read(*args)


@pytest.fixture
def network(monkeypatch):
    """urlopen and the backoff sleep, recorded; nothing leaves the machine."""
    state = {"answers": [], "calls": 0, "sleeps": []}

    def urlopen(url, timeout=None):
        state["calls"] += 1
        answer = state["answers"].pop(0) if state["answers"] else state["last"]
        state["last"] = answer
        if isinstance(answer, BaseException):
            raise answer
        return answer()

    monkeypatch.setattr(droid.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(droid.time, "sleep", state["sleeps"].append)
    return state


def test_a_listing_page_whose_body_is_cut_is_asked_for_again(network):
    page = json.dumps({"items": [{"name": "a", "size": "1"}]}).encode()
    network["answers"] = [
        lambda: Response(page, fail_first_read=True),
        lambda: Response(page),
    ]
    assert list(droid.Bucket().objects("x/")) == [{"name": "a", "size": 1, "md5": None}]
    assert network["calls"] == 2


def test_one_retry_layer_and_no_sleep_after_the_last_attempt(network, tmp_path):
    network["answers"] = [TimeoutError("timed out")]
    with pytest.raises(droid.NetworkError, match="after 3 attempts"):
        droid.Bucket(attempts=3).fetch("p/f.bin", tmp_path / "f.bin", 1, None)
    assert network["calls"] == 3 and network["sleeps"] == [1, 2]
    assert not (tmp_path / "f.bin.part").exists()


def test_a_refusal_is_not_retried(network, tmp_path):
    network["answers"] = [urllib.error.HTTPError("u", 404, "Not Found", {}, None)]
    with pytest.raises(urllib.error.HTTPError):
        droid.Bucket().fetch("p/f.bin", tmp_path / "f.bin", 1, None)
    assert network["calls"] == 1 and not network["sleeps"]


def test_a_full_disk_is_not_retried(network, tmp_path, monkeypatch):
    network["answers"] = [lambda: Response(b"x" * 10)]
    real_open = pathlib.Path.open

    class Full:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

    def open_(self, mode="r", *args, **kwargs):
        if self.name.endswith(".part"):
            return Full()
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", open_)
    with pytest.raises(OSError) as raised:
        droid.Bucket().fetch("p/f.bin", tmp_path / "f.bin", 10, None)
    assert raised.value.errno == errno.ENOSPC
    assert network["calls"] == 1 and not network["sleeps"]


# ----------------------------------------------- service start and settings


def test_the_service_resumes_interrupted_and_network_failed_draws_only(monkeypatch):
    from levi import samples

    calls = fake_start(monkeypatch)
    draw = samples.plan(3)
    monkeypatch.setenv("LEVI_DROID_SAMPLE", "auto")
    for fields, resumed in (
        ({"status": "interrupted"}, True),
        ({"status": "failed", "transient": True}, True),
        ({"status": "failed", "transient": False}, False),
        ({"status": "cancelled"}, False),
    ):
        calls.clear()
        droid.set_draw(
            draw["draw"], **{"auto_attempts": 0, "transient": None, **fields}
        )
        samples.on_service_start()
        assert bool(calls) is resumed, fields


def test_the_setting_is_off_unless_it_clearly_says_on(monkeypatch, caplog):
    from levi import samples

    for value in ("off", "0", "false", "no", " OFF "):
        monkeypatch.setenv("LEVI_DROID_SAMPLE", value)
        assert samples.setting() == "off", value
    for value in ("auto", "on", "1", "true", "yes", ""):
        monkeypatch.setenv("LEVI_DROID_SAMPLE", value)
        assert samples.setting() == "auto", value
    monkeypatch.delenv("LEVI_DROID_SAMPLE")
    assert samples.setting() == "auto"
    monkeypatch.setenv("LEVI_DROID_SAMPLE", "disabled")
    with caplog.at_level(logging.WARNING, logger="levi"):
        assert samples.setting() == "off"
    assert "LEVI_DROID_SAMPLE" in caplog.text


@pytest.mark.parametrize("workers", ["0", "9", "-1"])
def test_draw_workers_are_checked_before_anything_is_planned(workers):
    from levi import samples

    with pytest.raises(SystemExit) as raised:
        samples.cli(["draw", "--workers", workers])
    assert raised.value.code == 2
    assert not droid.ledger_path().exists()
