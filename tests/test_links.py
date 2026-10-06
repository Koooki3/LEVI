"""Read-only links to the live service's workspace (levi/links.py): the live
datasets are listed and opened in the product LEVI's "Local datasets" with
their rollouts, time segments and outcome labels, without the live service
running, without a copy and without ever writing into the live workspace."""

import hashlib
import json
from pathlib import Path

import pytest
from test_pool import make_lerobot

from levi import catalog, links, paths
from levi.live import auto

NAME = "pi05__pick_the_eggplant"
LINKED = "live." + NAME


def make_live(root: Path, name: str = NAME, *, episodes: int = 2) -> Path:
    """A live workspace: marker, catalog entry, mirror folder, browsing view,
    one episode's time segments and outcome label (the legacy layout)."""
    (root / "live").mkdir(parents=True)
    (root / "live" / auto.MARKER).write_text("{}")
    state = root / "outputs/LEVI/workbench"
    view = state / "views" / name
    make_lerobot(view, 7, episodes=episodes, task="Pick the eggplant")
    capture = root / "captures" / name
    (capture / "demo_0000").mkdir(parents=True)
    (state / "datasets.json").write_text(
        json.dumps(
            {
                name: {
                    "id": "local/" + name,
                    "name": name,
                    "kind": "raw",
                    "path": str(capture),
                    "view": str(view),
                    "view_status": "ready",
                    "info": json.loads((view / "meta/info.json").read_text()),
                }
            }
        )
    )
    notes = state / "annotations" / name
    (notes / "outcomes").mkdir(parents=True)
    (notes / "episode_000000.json").write_text(
        json.dumps(
            {
                "episode_index": 0,
                "atoms": [
                    {
                        "role": "assistant",
                        "content": "grasp the eggplant",
                        "style": "subtask",
                        "timestamp": 0.0,
                        "to": 0.8,
                    }
                ],
            }
        )
    )
    (notes / "outcomes" / "episode_000000.json").write_text(
        json.dumps({"episode_index": 0, "outcome": "success", "source": "human"})
    )
    return root


def tree(root: Path) -> dict:
    """Every file under ``root`` with a content hash, and every folder."""
    out = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        out[key] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        )
    return out


@pytest.fixture
def live(tmp_path, monkeypatch, client):
    """The product LEVI of the test, linked to a live workspace by the
    environment (as the live page's workspace is in a real install)."""
    # The product's workspace is its own folder: the live workspace lies
    # outside it, as in a real install (not under it, as the test folders are
    # under the checkout's .state).
    monkeypatch.setattr(paths, "ROOT", tmp_path / "product")
    (tmp_path / "product").mkdir()
    workspace = make_live(tmp_path / "livews")
    monkeypatch.setenv(links.ENV, str(workspace))
    return workspace


def test_the_live_datasets_are_listed_read_only_and_never_stored(client, live):
    found = client.get("/api/levi/catalog").json()
    (entry,) = [e for e in found["local"] if e["name"] == LINKED]
    assert entry["id"] == "local/" + LINKED
    assert entry["linked"] == {
        "kind": "live",
        "source": NAME,
        "workspace": "livews",
        "readonly": True,
    }
    assert found["linked"] == [{"kind": "live", "workspace": "livews", "datasets": 1}]
    assert client.get(f"/api/levi/catalog/{LINKED}").json()["revision"]
    # It is computed when asked: this LEVI's own catalog never holds it.
    assert LINKED not in catalog.datasets()
    client.post("/api/levi/sync")
    assert LINKED not in catalog.datasets()
    assert not (catalog.STATE / "datasets.json").exists() or LINKED not in json.loads(
        (catalog.STATE / "datasets.json").read_text()
    )


def test_a_rollout_view_opens_and_streams_without_the_live_service(client, live):
    base = f"/api/levi/files/{LINKED}"
    info = client.get(f"{base}/meta/info.json")
    assert info.status_code == 200 and info.json()["total_episodes"] == 2
    video = f"{base}/videos/chunk-000/observation.images.hand/episode_000000.mp4"
    assert client.head(video).status_code == 200
    assert client.get(video, headers={"Range": "bytes=0-3"}).status_code == 206
    # Only the dataset's assets, and only inside its folder.
    assert client.get(f"{base}/meta/../../../datasets.json").status_code != 200
    assert client.get(f"{base}/outputs/LEVI/workbench/datasets.json").status_code == 403
    assert client.get(f"{base}/../../live/workspace.json").status_code != 200


def test_time_segments_and_outcome_labels_are_read_from_the_live_workspace(
    client, live
):
    repo = "local/" + LINKED
    atoms = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    ).json()["atoms"]
    assert [a["content"] for a in atoms] == ["grasp the eggplant"]
    labels = client.get(
        "/annotations/api/episodes/outcomes", params={"repo_id": repo}
    ).json()["labels"]
    assert labels["0"]["outcome"] == "success"
    summary = client.get(
        "/annotations/api/episodes/annotation-summary", params={"repo_id": repo}
    ).json()
    assert summary["language"] == {"0": True, "1": False}


def test_nothing_is_ever_written_into_the_live_workspace(client, live):
    before = tree(live)
    repo = "local/" + LINKED
    client.get("/api/levi/catalog")
    client.get(f"/api/levi/catalog/{LINKED}")
    client.get(f"/api/levi/files/{LINKED}/meta/info.json")
    client.get("/annotations/api/episodes/0/atoms", params={"repo_id": repo})
    client.get("/annotations/api/episodes/outcomes", params={"repo_id": repo})
    client.get("/annotations/api/episodes/annotation-summary", params={"repo_id": repo})
    client.get("/annotations/api/dataset/vocabulary", params={"repo_id": repo})
    client.get("/annotations/api/anchored/summary", params={"repo_id": repo})
    client.get("/annotations/api/sam3/revisions", params={"repo_id": repo})
    client.get("/api/levi/review", params={"repo_id": repo})
    client.post("/annotations/api/dataset/load", json={"repo_id": repo})
    # Not a file more, not a folder more (no agent store, no sidecar root).
    assert tree(live) == before


@pytest.mark.parametrize(
    "method, url, body",
    [
        (
            "post",
            "/annotations/api/episodes/0/atoms",
            {"episode_index": 0, "atoms": []},
        ),
        ("delete", "/annotations/api/episodes/0/atoms", None),
        ("post", "/annotations/api/episodes/0/outcome", {"outcome": "failure"}),
        ("post", "/annotations/api/episodes/0/status", {"status": "done"}),
        ("post", "/annotations/api/dataset/vocabulary", {"vocabulary": []}),
        ("post", "/annotations/api/export", {}),
        ("post", "/annotations/api/sam3/run", {"episode_indices": [0]}),
        ("post", "/annotations/api/sam3/edits", {}),
        ("post", "/annotations/api/recap/run", {}),
        ("post", "/api/levi/review", {"flagged": [0]}),
        ("post", "/api/levi/manifest", {"operation": "all"}),
    ],
)
def test_every_write_to_a_live_dataset_is_refused(client, live, method, url, body):
    before = tree(live)
    repo = "local/" + LINKED
    kwargs = {"json": {**(body or {}), "repo_id": repo}}
    if method == "delete":
        kwargs = {"params": {"repo_id": repo}}
    response = getattr(client, method)(url, **kwargs)
    assert response.status_code == 403, (url, response.status_code, response.text)
    assert "read-only" in response.text
    assert tree(live) == before


def test_a_live_dataset_cannot_be_unregistered_here(client, live):
    assert client.delete(f"/api/levi/catalog/{LINKED}").status_code == 403
    assert any(
        e["name"] == LINKED for e in client.get("/api/levi/catalog").json()["local"]
    )


def test_only_a_real_live_workspace_is_linked(client, tmp_path, monkeypatch, live):
    plain = tmp_path / "plain"
    (plain / "outputs/LEVI/workbench").mkdir(parents=True)
    (plain / "outputs/LEVI/workbench/datasets.json").write_text(
        json.dumps(
            {"x": {"id": "local/x", "name": "x", "kind": "lerobot", "path": "/"}}
        )
    )
    monkeypatch.setenv(links.ENV, f"{plain}, relative/path, {live}, {live}")
    assert [w.name for w in links.workspaces()] == ["livews"]  # once, and only it
    # This LEVI's own workspace is never a link, nor one inside or around it.
    monkeypatch.setattr(paths, "ROOT", live)
    assert links.workspaces() == []
    monkeypatch.setattr(paths, "ROOT", live / "captures")
    assert links.workspaces() == []


def test_a_dataset_the_live_workspace_drops_is_gone_here_too(client, live):
    assert client.get(f"/api/levi/catalog/{LINKED}").status_code == 200
    (live / "outputs/LEVI/workbench/datasets.json").write_text("{}")
    assert client.get(f"/api/levi/catalog/{LINKED}").status_code == 404
    assert client.get(f"/api/levi/files/{LINKED}/meta/info.json").status_code == 400


def test_two_live_workspaces_with_one_dataset_name_stay_two_datasets(
    client, live, tmp_path, monkeypatch
):
    other = make_live(tmp_path / "other")
    monkeypatch.setenv(links.ENV, f"{live},{other}")
    names = sorted(links.links())
    assert len(names) == 2 and LINKED in names
    (second,) = [n for n in names if n != LINKED]
    assert second.startswith("live.") and second.endswith("." + NAME)
    assert links.get(second).workspace == other.resolve()


def test_a_view_that_is_not_ready_says_so_unless_an_earlier_one_is_whole(client, live):
    path = live / "outputs/LEVI/workbench/datasets.json"
    value = json.loads(path.read_text())
    value[NAME]["view_status"] = "failed"
    value[NAME]["view_error"] = "rollouts removed"
    path.write_text(json.dumps(value))
    # The rollouts are gone and the rebuild failed, but the view built before
    # is still there: it opens, marked stale.
    (entry,) = [
        e
        for e in client.get("/api/levi/catalog").json()["local"]
        if e["name"] == LINKED
    ]
    assert entry["view_status"] == "ready" and entry["linked"]["stale"] is True
    assert client.get(f"/api/levi/files/{LINKED}/meta/info.json").status_code == 200
    # No view at all: it says so.
    view = live / "outputs/LEVI/workbench/views" / NAME
    (view / "meta/info.json").unlink()
    response = client.get(f"/api/levi/files/{LINKED}/meta/info.json")
    assert response.status_code == 400 and "not ready" in response.text


def test_a_store_with_a_head_is_read_where_it_is_kept(client, live):
    """Annotations of a live dataset that has moved to the agent store's
    bundles are found through its head, read-only."""
    from levi.agent.store import ReadStore, Store, resolve_readonly

    state = live / "outputs/LEVI/workbench"
    store = Store(state)  # the live service's own writer, to make the fixture
    folder = store.bundle(NAME, "r1")
    (folder / "annotations/outcomes").mkdir(parents=True)
    (folder / "annotations/episode_000001.json").write_text(
        json.dumps(
            {
                "episode_index": 1,
                "atoms": [
                    {
                        "role": "assistant",
                        "content": "from the bundle",
                        "style": "subtask",
                        "timestamp": 0.0,
                        "to": 0.5,
                    }
                ],
            }
        )
    )
    with store.connect() as db:
        db.execute("INSERT INTO heads(dataset, revision) VALUES (?, ?)", (NAME, "r1"))
    assert ReadStore(state).head(NAME) == "r1"
    assert resolve_readonly(state, NAME, "annotations") == folder / "annotations"
    before = tree(live)
    atoms = client.get(
        "/annotations/api/episodes/1/atoms", params={"repo_id": "local/" + LINKED}
    )
    assert atoms.status_code == 200
    assert [a["content"] for a in atoms.json()["atoms"]] == ["from the bundle"]
    assert atoms.headers["X-LEVI-Annotation-Revision"] == "r1"
    assert tree(live) == before


def test_a_read_only_store_never_creates_anything(tmp_path):
    from levi.agent.store import ReadStore

    state = tmp_path / "nowhere"
    store = ReadStore(state)
    assert store.head("x") == "legacy" and store.list("runs") == []
    with pytest.raises(KeyError):
        store.get("runs", "a")
    assert not state.exists()


def test_reading_a_closed_store_makes_no_file_beside_it(tmp_path):
    """A WAL database opened read-only gets a -shm and a -wal file made beside
    it; a store nobody has open is read without that."""
    import sqlite3

    from levi.agent.store import ReadStore

    state = tmp_path / "state"
    (state / "agent").mkdir(parents=True)
    db = sqlite3.connect(state / "agent/workbench.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        "CREATE TABLE heads(dataset TEXT PRIMARY KEY, revision TEXT NOT NULL);"
        "CREATE TABLE records(kind TEXT, id TEXT, body TEXT NOT NULL);"
        "INSERT INTO heads VALUES('d', 'r7');"
    )
    db.commit()
    db.close()
    before = sorted(p.name for p in (state / "agent").iterdir())
    assert before == ["workbench.sqlite3"]
    assert ReadStore(state).head("d") == "r7"
    assert ReadStore(state).list("runs") == []
    assert sorted(p.name for p in (state / "agent").iterdir()) == before


def test_a_quality_inspection_of_a_live_dataset_reads_and_writes_only_here(
    client, live
):
    before = tree(live)
    response = client.post(
        "/api/levi/diagnostics", json={"repo_id": "local/" + LINKED, "max_episodes": 1}
    )
    assert response.status_code == 200, response.text
    assert tree(live) == before
