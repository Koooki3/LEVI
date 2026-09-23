"""The DROID raw test sample a workspace draws: reproducible (the reference
subset script's selection), resumable, verified file by file, never
overlapping earlier draws. The public bucket is replaced by an in-memory one; nothing is downloaded."""

import base64
import hashlib
import json
import random
import shutil
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

    def objects(self, prefix, glob=None, delimiter=None):
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

    def fetch(self, name, dest: Path, size, md5, on_bytes=None):
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

    def start(size=droid.SIZE, workers=None, reason="manual"):
        calls.append(reason)
        return samples.plan(size, reason)

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
