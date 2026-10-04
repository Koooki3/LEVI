"""The live page in the product LEVI: how a LEVI that is not the live
workspace finds it (``levi/live/locate.py``), that every read route then
answers from the live workspace's files, that removing an episode stays a
person's action and is audited in the live workspace, that nothing of the
live service lands in the product workspace, and that ``levi live start``
starts no page of its own by default. Temporary directories, a fake home
and a fake model server only: no running LEVI, no live service, no GPU."""

import os
import time
from pathlib import Path

import pytest
from test_live_exclude import audit_lines, mirror_only
from test_live_pipeline import NAME, Env

from levi.live import api, cli, exclusion, jsonio, locate, stats

UI = {"x-levi-ui-token": "test-ui-token"}
AGENT = {"Authorization": "Bearer test-scoped-token"}


@pytest.fixture
def env(tmp_path, demo_template):
    made = []

    def make(**fake_options):
        made.append(Env(tmp_path, demo_template, **fake_options))
        return made[-1]

    yield make
    for item in made:
        item.close()


@pytest.fixture
def product(tmp_path, monkeypatch):
    """The product LEVI's own workspace: no live marker, a catalog that has a
    dataset of the live dataset's name (another one), and the live service's
    home where the status file would be."""
    ws = tmp_path / "product"
    (ws / "outputs/LEVI/workbench").mkdir(parents=True)
    jsonio.write(
        ws / "outputs/LEVI/workbench/datasets.json",
        {NAME: {"id": "local/not-the-live-one", "name": NAME}},
    )
    monkeypatch.setattr(api, "_own", lambda: ws)
    home = tmp_path / "home"
    monkeypatch.setenv("LEVI_LIVE_HOME", str(home))
    return ws, home


def write_status(home: Path, workspace, **extra):
    jsonio.write(
        Path(home) / "status.json",
        {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "state": "idle",
            "workspace": str(workspace),
            "datasets": {},
            **extra,
        },
    )


def snapshot(folder: Path) -> list:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


# --- finding the live workspace --------------------------------------------------------


def test_the_live_workspace_s_own_core_shows_its_own(env):
    e = env()
    found = locate.find(e.ws)
    assert found.workspace == e.ws and found.how == "own" and not found.embedded


def test_nothing_set_up_is_not_configured_and_the_page_says_so(client, product):
    ws, _home = product
    found = locate.find(ws)
    assert found.workspace is None and found.problem == "not_configured"
    for path in ("status", "sessions", "datasets", "audit", "stats"):
        body = client.get(f"/api/levi/live/{path}").json()
        assert body == {"enabled": False, "reason": "not_configured"}, path


def test_the_status_file_names_the_live_workspace(client, env, product):
    e = env()
    ws, home = product
    write_status(home, e.ws)
    found = locate.find(ws)
    assert found.workspace == e.ws.resolve() and found.how == "status"
    assert found.embedded
    body = client.get("/api/levi/live/status").json()
    assert body["enabled"] is True and body["embedded"] is True
    # The service runs no page of its own: no link to one.
    assert body["live_ui"] is None


def test_the_environment_overrides_the_status_file(env, product, tmp_path, monkeypatch):
    e = env()
    ws, home = product
    write_status(home, tmp_path / "elsewhere")
    assert locate.find(ws).problem == "not_live"
    monkeypatch.setenv("LEVI_LIVE_WORKSPACE", str(e.ws))
    found = locate.find(ws)
    assert found.workspace == e.ws.resolve() and found.how == "env"


@pytest.mark.parametrize("named", ["missing", "plain"])
def test_a_folder_that_is_not_a_live_workspace_is_not_used(
    client, product, tmp_path, named
):
    ws, home = product
    target = tmp_path / named
    if named == "plain":
        target.mkdir()
    write_status(home, target)
    assert locate.find(ws).problem == "not_live"
    assert client.get("/api/levi/live/status").json() == {
        "enabled": False,
        "reason": "not_live",
    }


def test_the_product_workspace_is_never_shown_as_the_live_one(
    product, monkeypatch, tmp_path
):
    ws, home = product
    # A checkout's own `.state` (where the product LEVI runs), even carrying
    # the live marker (someone copied it there).
    checkout = tmp_path / "checkout/.state"
    (checkout / "live").mkdir(parents=True)
    (checkout / "live/workspace.json").write_text("{}")
    monkeypatch.setattr(cli, "protected_workspaces", lambda: [checkout.resolve()])
    write_status(home, checkout)
    found = locate.find(ws)
    assert found.workspace is None and found.problem == "product_workspace"
    monkeypatch.setenv("LEVI_LIVE_WORKSPACE", str(checkout))
    assert locate.find(ws).problem == "product_workspace"
    # This LEVI's own workspace, named as the live one.
    monkeypatch.setenv("LEVI_LIVE_WORKSPACE", str(ws))
    assert locate.find(ws).problem == "product_workspace"


def test_no_path_or_secret_in_a_refusal(client, product, tmp_path):
    _ws, home = product
    write_status(home, tmp_path / "secret-folder-name")
    text = client.get("/api/levi/live/status").text
    assert "secret-folder-name" not in text and str(tmp_path) not in text


# --- the read routes in the product LEVI ----------------------------------------------


def test_every_read_route_answers_from_the_live_workspace(client, env, product):
    e = env()
    ws, home = product
    mirror_only(e, 0, 1)
    jsonio.write(
        e.ws / "outputs/LEVI/workbench/datasets.json",
        {NAME: {"id": "local/the-live-one", "name": NAME}},
    )
    stats.record(
        e.config.live_dir,
        {"dataset": NAME, "demo": "demo_0000", "session": "s1", "at": 1.0},
    )
    write_status(home, e.ws, datasets={NAME: {"pending": 2, "done": 0}})
    before = snapshot(ws)

    status = client.get("/api/levi/live/status").json()
    assert status["enabled"] and status["service"]["datasets"][NAME]["pending"] == 2
    assert client.get("/api/levi/live/datasets").json()["datasets"] == {
        NAME: {"pending": 2, "done": 0}
    }
    detail = client.get(f"/api/levi/live/datasets/{NAME}").json()
    assert detail["total_demos"] == 2 and detail["embedded"] is True
    # The live workspace's catalog, not the product's same-named dataset.
    assert detail["repo_id"] == "local/the-live-one"
    sessions = client.get("/api/levi/live/sessions").json()
    assert sessions["enabled"] is True and "fr3" in sessions
    assert client.get("/api/levi/live/audit").json() == {"enabled": True, "audit": []}
    body = client.get("/api/levi/live/stats").json()
    assert body["enabled"] is True
    export = client.get("/api/levi/live/stats/export?format=csv")
    assert export.status_code == 200
    assert snapshot(ws) == before


def test_the_live_page_link_is_given_only_when_the_service_runs_one(
    client, env, product
):
    e = env()
    _ws, home = product
    write_status(home, e.ws, frontend={"ui": True, "state": "ok"})
    page = client.get("/api/levi/live/status").json()["live_ui"]
    assert page == f"http://127.0.0.1:{e.config.service.ui_port}"
    write_status(home, e.ws, frontend={"ui": False, "state": "ok"})
    assert client.get("/api/levi/live/status").json()["live_ui"] is None


# --- removing an episode from the product LEVI ------------------------------------------


ROUTES = [
    ("/exclude", {"demos": ["demo_0000"], "reason": "x"}),
    ("/restore", {"demos": ["demo_0000"]}),
    ("/demos/demo_0000/exclude", {"reason": "x"}),
    ("/demos/demo_0000/restore", None),
]


def test_removing_in_the_product_needs_its_ui_token_and_refuses_an_agent(
    client, env, product, monkeypatch
):
    e = env()
    _ws, home = product
    mirror_only(e, 0)
    write_status(home, e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    for suffix, body in ROUTES:
        url = f"/api/levi/live/datasets/{NAME}{suffix}"
        assert client.post(url, json=body).status_code == 401, url
        assert client.post(url, json=body, headers=AGENT).status_code == 403, url
        both = {**AGENT, **UI}
        assert client.post(url, json=body, headers=both).status_code == 403, url
        # The live core's key is not the product's token.
        wrong = {"x-levi-ui-token": "the-live-core-s-key"}
        assert client.post(url, json=body, headers=wrong).status_code == 401, url
    assert not exclusion.is_excluded(e.state()["demos"]["demo_0000"])
    assert audit_lines(e) == []


def test_removing_in_the_product_changes_the_live_workspace_and_audits_there(
    client, env, product, monkeypatch
):
    e = env()
    ws, home = product
    mirror_only(e, 0, 1)
    write_status(home, e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    before = snapshot(ws)
    url = f"/api/levi/live/datasets/{NAME}"
    done = client.post(
        f"{url}/exclude", json={"demos": ["demo_0001"], "reason": "bad"}, headers=UI
    )
    assert done.status_code == 200 and done.json()["changed"] == ["demo_0001"]
    assert exclusion.is_excluded(e.state()["demos"]["demo_0001"])
    detail = client.get(url, headers=UI).json()
    assert detail["excluded_count"] == 1 and detail["total_demos"] == 1
    back = client.post(f"{url}/demos/demo_0001/restore", headers=UI)
    assert back.status_code == 200
    assert not exclusion.is_excluded(e.state()["demos"]["demo_0001"])
    lines = audit_lines(e)
    assert [(x["tool"], x["via"], x["actor"]) for x in lines] == [
        ("episode.exclude", "product", "person"),
        ("episode.restore", "product", "person"),
    ]
    text = (e.ws / "live/audit.jsonl").read_text().lower()
    assert "token" not in text and "key" not in text
    # Nothing of the live service in the product workspace.
    assert snapshot(ws) == before
    assert not (ws / "live").exists()


def test_the_live_workspace_s_own_core_does_not_mark_its_audit(
    client, env, monkeypatch
):
    e = env()
    monkeypatch.setattr(api, "_own", lambda: e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    mirror_only(e, 0)
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    assert (
        client.post(url, json={"demos": ["demo_0000"]}, headers=UI).status_code == 200
    )
    (line,) = audit_lines(e)
    assert "via" not in line
    assert client.get("/api/levi/live/status", headers=UI).json()["embedded"] is False


def test_no_live_workspace_refuses_a_removal(client, product, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    answer = client.post(url, json={"demos": ["demo_0000"]}, headers=UI)
    assert answer.status_code == 404


# --- the live service starts no page of its own by default ----------------------------


@pytest.mark.parametrize(
    "flags, ui",
    [([], False), (["--no-ui"], False), (["--ui"], True), (["--ui", "--no-ui"], False)],
)
def test_the_live_service_starts_its_own_page_only_when_asked(flags, ui):
    args = cli.build_parser().parse_args(["start", *flags])
    assert cli.wants_ui(args) is ui
    forwarded = cli._forward(args)
    assert ("--ui" in forwarded) is ui and "--no-ui" not in forwarded


def test_the_status_line_points_to_the_product_page_without_a_page():
    line = cli.format_status(
        {"updated_at": 0, "ui_url": "http://127.0.0.1:7880", "frontend": {"ui": False}},
        False,
    )
    assert "product LEVI (/live)" in line and "7880" not in line
    line = cli.format_status(
        {"updated_at": 0, "ui_url": "http://127.0.0.1:7880", "frontend": {"ui": True}},
        False,
    )
    assert "http://127.0.0.1:7880" in line
