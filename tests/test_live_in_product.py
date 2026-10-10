"""The live page in the product LEVI: how a LEVI that is not the live
workspace finds it (``levi/live/locate.py``), that every read route then
answers from the live workspace's files, that removing an episode stays a
person's action and is audited in the live workspace, that nothing of the
live service lands in the product workspace, and that ``levi live start``
starts no page of its own by default. Temporary directories, a fake home
and a fake model server only: no running LEVI, no live service, no GPU."""

import json
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


def write_status(home: Path, workspace, *, started=True, **extra):
    """The live home after ``levi live start`` on ``workspace``: its status
    file and, unless ``started=False`` (a service from before the record, or
    only a ``levi live once``), its ``started.json``."""
    if started:
        jsonio.write(Path(home) / locate.STARTED, {"workspace": str(workspace)})
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


# The one thing the product LEVI keeps about the live workspace in its own
# workspace: its training pool's list of the live workspaces it was shown.
REMEMBERED = {"pool", "pool/live_workspaces.json", "pool/live_workspaces.json.lock"}


def snapshot(folder: Path) -> list:
    return sorted(
        name
        for name in (str(p.relative_to(folder)) for p in folder.rglob("*"))
        if name not in REMEMBERED
    )


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
    monkeypatch.setattr(locate, "protected_workspaces", lambda: [checkout.resolve()])
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
    summaries = client.get("/api/levi/live/datasets").json()["datasets"]
    assert set(summaries) == {NAME}
    assert (summaries[NAME]["pending"], summaries[NAME]["done"]) == (2, 0)
    assert summaries[NAME]["task_folder"] == "stack_the_plates"
    assert summaries[NAME]["view_status"] == "pending"
    assert summaries[NAME]["viewer_url"] is None
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


# --- review: overlap, every worktree, links, relative paths, paths not shown -----------


def fake_checkouts(tmp_path):
    """A main checkout with one worktree, as git lays them out."""
    main = tmp_path / "LEVI"
    other = tmp_path / "LEVI-other"
    (main / ".git/worktrees/other").mkdir(parents=True)
    (main / ".git/worktrees/other/gitdir").write_text(f"{other}/.git\n")
    (main / ".git/worktrees/other/commondir").write_text("../..\n")
    other.mkdir()
    (other / ".git").write_text(f"gitdir: {main}/.git/worktrees/other\n")
    for top in (main, other):
        (top / ".state").mkdir()
    return main, other


def live_folder(path: Path) -> Path:
    (path / "live").mkdir(parents=True)
    (path / "live/workspace.json").write_text("{}")
    return path


def test_every_checkout_s_state_is_protected_from_any_of_them(tmp_path, monkeypatch):
    main, other = fake_checkouts(tmp_path)
    for start in (main, other):
        monkeypatch.setattr(locate, "checkout_root", lambda start=start: start)
        assert set(locate.protected_workspaces()) == {
            (main / ".state").resolve(),
            (other / ".state").resolve(),
        }


@pytest.mark.parametrize(
    "where",
    ["main/.state/live-ws", "other/.state/tmp/ws", "main/.state", "holder"],
)
def test_a_live_workspace_inside_or_around_a_checkout_s_state_is_refused(
    product, tmp_path, monkeypatch, where
):
    ws, home = product
    main, other = fake_checkouts(tmp_path / "repo")
    monkeypatch.setattr(locate, "checkout_root", lambda: main)
    named = {
        "main/.state/live-ws": main / ".state/live-ws",
        "other/.state/tmp/ws": other / ".state/tmp/ws",
        "main/.state": main / ".state",
        "holder": tmp_path / "repo",  # holds both checkouts' .state
    }[where]
    live_folder(named) if not (named / "live").exists() else None
    write_status(home, named)
    assert locate.find(ws).problem == "product_workspace"
    # And the service refuses it as its workspace, adopted or not.
    from levi.live import config as live_config

    c = live_config.Config()
    c.service.workspace = str(named)
    c.service.home = str(home)
    with pytest.raises(ValueError, match="product LEVI"):
        cli.check_workspace(c, adopt=True)


def test_a_live_workspace_around_the_product_s_own_workspace_is_refused(
    product, tmp_path
):
    ws, home = product
    write_status(home, live_folder(tmp_path))  # tmp_path holds the product's
    assert locate.find(ws).problem == "product_workspace"


def test_a_live_folder_linked_to_elsewhere_is_not_a_live_workspace(
    client, env, product, tmp_path, monkeypatch
):
    e = env()
    ws, home = product
    mirror_only(e, 0)
    outside = tmp_path / "elsewhere"
    (e.ws / "live").rename(outside)
    (e.ws / "live").symlink_to(outside, target_is_directory=True)
    write_status(home, e.ws)
    assert locate.find(ws).problem == "not_live"
    # The live workspace's own core: the read works, a write is refused.
    monkeypatch.setattr(api, "_own", lambda: e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    answer = client.post(url, json={"demos": ["demo_0000"]}, headers=UI)
    assert answer.status_code == 409
    assert not exclusion.is_excluded(
        jsonio.read(outside / f"datasets/{NAME}.json")["demos"]["demo_0000"]
    )


@pytest.mark.parametrize("named", ["levi-live-ws", "./ws", "ws/../ws"])
def test_a_relative_path_never_names_the_live_workspace(
    product, tmp_path, monkeypatch, named
):
    ws, home = product
    monkeypatch.chdir(tmp_path)
    live_folder(tmp_path / named.split("/")[-1].lstrip("."))
    monkeypatch.setenv("LEVI_LIVE_WORKSPACE", named)
    assert locate.find(ws).problem == "not_live"
    monkeypatch.delenv("LEVI_LIVE_WORKSPACE")
    write_status(home, named)
    assert locate.find(ws).problem == "not_live"


def test_the_configured_workspace_is_stored_absolute(tmp_path, monkeypatch):
    from levi.live import config as live_config

    monkeypatch.chdir(tmp_path)
    config = live_config.load(None, "rel/ws")
    assert config.service.workspace == str((tmp_path / "rel/ws").resolve())


def test_the_product_page_gives_no_path_of_the_live_workspace(client, env, product):
    e = env()
    _ws, home = product
    write_status(home, e.ws, config=str(e.ws / "live.toml"))
    body = client.get("/api/levi/live/status").json()
    assert "workspace" not in body["service"] and "config" not in body["service"]
    assert body["workspace_name"] == e.ws.name
    assert str(e.ws) not in json.dumps(body)


def test_the_live_workspace_s_own_page_keeps_the_whole_status(client, env, monkeypatch):
    e = env()
    monkeypatch.setattr(api, "_own", lambda: e.ws)
    write_status(locate.home(), e.ws, config="x.toml")
    body = client.get("/api/levi/live/status").json()
    assert body["service"]["workspace"] == str(e.ws)
    assert body["service"]["config"] == "x.toml"


def test_the_product_remembers_each_live_workspace_it_showed(
    client, env, product, tmp_path
):
    e = env()
    ws, home = product
    write_status(home, e.ws)
    client.get("/api/levi/live/status")
    other = live_folder(tmp_path / "second-live")
    write_status(home, other)
    client.get("/api/levi/live/status")
    client.get("/api/levi/live/status")
    from levi.pool import exclusions as pool_exclusions

    assert pool_exclusions.remembered(ws / "pool") == [e.ws.resolve(), other.resolve()]


def test_the_first_log_line_says_what_the_service_serves():
    c = __import__("levi.live.config", fromlist=["Config"]).Config()
    parse = cli.build_parser().parse_args
    assert cli._served(c, parse(["start"])) == (
        "core :7881 (the live page is in the product LEVI)"
    )
    assert cli._served(c, parse(["start", "--ui"])) == "UI :7880 core :7881"
    assert cli._served(c, parse(["start", "--no-core"])) == "no page or core"


def test_no_setting_unprotects_the_checkout_s_state(tmp_path, monkeypatch):
    """A variable that once named the protected checkout (it could come from
    a `.env`) is ignored: the checkout this code runs from stays protected."""
    from levi.live import config as live_config

    monkeypatch.setattr(locate, "checkout_root", lambda: locate.THIS_CHECKOUT)
    monkeypatch.setenv("LEVI_LIVE_PROTECT_CHECKOUT", str(tmp_path / "elsewhere"))
    assert (locate.THIS_CHECKOUT / ".state").resolve() in locate.protected_workspaces()
    for named in (".state", ".state/live-ws"):
        c = live_config.Config()
        c.service.workspace = str(locate.THIS_CHECKOUT / named)
        with pytest.raises(ValueError, match="product LEVI"):
            cli.check_workspace(c, adopt=True)


# --- review 2: the remembered list, its cap and the CLI; every written path ---------------


def test_the_remembered_list_has_a_cap_and_says_so(
    client, env, product, tmp_path, monkeypatch, caplog
):
    from levi.pool import exclusions as pool_exclusions

    ws, home = product
    monkeypatch.setattr(pool_exclusions, "MAX_REMEMBERED", 2)
    first, second = (live_folder(tmp_path / f"live-{n}") for n in (1, 2))
    for named in (first, second):
        write_status(home, named)
        assert client.get("/api/levi/live/status").json()["pool_memory_full"] is False
    e = env()
    write_status(home, e.ws)
    with caplog.at_level("WARNING", logger="levi.pool.exclusions"):
        body = client.get("/api/levi/live/status").json()
    assert body["enabled"] and body["pool_memory_full"] is True
    assert "levi pool live-workspaces forget" in caplog.text
    assert len(pool_exclusions.remembered(ws / "pool")) == 2
    # Forgetting deletes nothing and makes room.
    assert pool_exclusions.forget(first, ws / "pool")
    assert (first / "live/workspace.json").is_file()
    assert client.get("/api/levi/live/status").json()["pool_memory_full"] is False
    assert pool_exclusions.remembered(ws / "pool") == [second.resolve(), e.ws.resolve()]
    assert not pool_exclusions.forget(tmp_path / "never", ws / "pool")


def test_levi_pool_live_workspaces_lists_and_forgets(tmp_path, monkeypatch, capsys):
    from levi import paths
    from levi.pool import cli as pool_cli
    from levi.pool import exclusions as pool_exclusions

    monkeypatch.setattr(paths, "configure", lambda: None)
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "product"))
    live = live_folder(tmp_path / "live-a")
    pool_exclusions.remember(live)
    assert pool_cli.main(["live-workspaces", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["remembered"] == [{"path": str(live), "exists": True}]
    assert listed["limit"] == pool_exclusions.MAX_REMEMBERED
    assert pool_cli.main(["live-workspaces", "forget", str(live)]) == 0
    assert "nothing deleted" in capsys.readouterr().out
    assert live.is_dir() and pool_exclusions.remembered() == []
    assert pool_cli.main(["live-workspaces", "forget", str(live)]) == 1


@pytest.mark.parametrize("linked", ["audit", "lock"])
def test_a_removal_refuses_an_audit_log_or_lock_linked_out_of_the_workspace(
    client, env, monkeypatch, tmp_path, linked
):
    e = env()
    mirror_only(e, 0)
    monkeypatch.setattr(api, "_own", lambda: e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    target = tmp_path / "outside-file"
    target.write_text("")
    link = (
        e.ws / "live/audit.jsonl"
        if linked == "audit"
        else e.ws / f"live/datasets/{NAME}.json.lock"
    )
    link.unlink(missing_ok=True)
    link.symlink_to(target)
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    answer = client.post(url, json={"demos": ["demo_0000"]}, headers=UI)
    assert answer.status_code == 409
    assert not exclusion.is_excluded(e.state()["demos"]["demo_0000"])
    assert target.read_text() == ""


def test_the_detail_names_the_product_viewer_id_of_a_linked_dataset(
    client, env, product, monkeypatch
):
    """The product LEVI links the live workspace its page shows (levi/links.py):
    the card then opens the dataset in the product's own viewer."""
    from levi import links, paths

    e = env()
    ws, home = product
    mirror_only(e, 0, 1)
    jsonio.write(
        e.ws / "outputs/LEVI/workbench/datasets.json",
        {NAME: {"id": "local/the-live-one", "name": NAME}},
    )
    write_status(home, e.ws)
    monkeypatch.setattr(paths, "ROOT", ws)
    detail = client.get(f"/api/levi/live/datasets/{NAME}").json()
    assert detail["embedded"] is True
    assert detail["linked_repo_id"] == "local/live." + NAME
    # Not linked (no such live workspace here): nothing to open.
    monkeypatch.setattr(links, "workspaces", list)
    assert (
        client.get(f"/api/levi/live/datasets/{NAME}").json()["linked_repo_id"] is None
    )
