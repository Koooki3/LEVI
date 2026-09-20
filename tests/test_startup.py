"""Startup regressions: distinguish the workbench from its internal API."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from levi.cli import check_ports, frontend_url, wait_ready


def test_backend_root_guides_to_ui_without_host_header_redirect(client, monkeypatch):
    monkeypatch.delenv("LEVI_FRONTEND_URL", raising=False)
    response = client.get("/", headers={"Host": "untrusted.example"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'href="http://127.0.0.1:7860"' in response.text
    assert "This is the LEVI API service" in response.text
    assert "这是 LEVI 的内部 API 服务" in response.text
    assert "untrusted.example" not in response.text
    assert client.head("/").status_code == 200
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/api/levi/health").json() == {
        "service": "levi-api",
        "status": "ok",
    }
    assert client.get("/not-an-api-route").status_code == 404


def test_backend_link_follows_custom_ui_port(client, monkeypatch):
    monkeypatch.setenv("LEVI_FRONTEND_URL", frontend_url("0.0.0.0", 7870))
    assert 'href="http://127.0.0.1:7870"' in client.get("/").text
    monkeypatch.setenv("LEVI_FRONTEND_URL", "javascript:alert(1)")
    response = client.get("/")
    assert 'href="http://127.0.0.1:7860"' in response.text
    assert "javascript:" not in response.text
    assert frontend_url("::", 7870) == "http://127.0.0.1:7870"
    assert frontend_url("::1", 7870) == "http://[::1]:7870"


def test_conflicting_or_occupied_ports_are_rejected():
    with pytest.raises(ValueError, match="ports must differ"):
        check_ports("127.0.0.1", 7860, 7860)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match="Web UI.*cannot bind"):
            check_ports("127.0.0.1", port, 1)
        with pytest.raises(ValueError, match="API.*cannot bind"):
            check_ports("127.0.0.1", 1, port, backend_only=True)


@pytest.fixture
def startup_server():
    state = {"html": True, "service": "levi-api", "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["paths"].append(self.path)
            self.send_response(200)
            if self.path == "/api/levi/health":
                body = json.dumps({"service": state["service"]}).encode()
                content_type = "application/json"
            else:
                body = b"<!doctype html><title>LEVI</title>"
                content_type = "text/html" if state["html"] else "application/json"
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ready_requires_ui_and_api(startup_server, monkeypatch):
    state, url = startup_server
    # Local checks must not use a configured outbound HTTP proxy.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    wait_ready([SimpleNamespace(poll=lambda: None)], url, url, timeout=1)
    assert set(state["paths"]) == {"/", "/api/levi/health"}


@pytest.mark.parametrize("field,value", [("html", False), ("service", "other-api")])
def test_wrong_service_cannot_be_announced_ready(startup_server, field, value):
    state, url = startup_server
    state[field] = value
    with pytest.raises(RuntimeError, match="Startup timed out"):
        wait_ready([SimpleNamespace(poll=lambda: None)], url, url, timeout=0.1)


def test_early_service_exit_fails_startup():
    with pytest.raises(RuntimeError, match="exited before ready"):
        wait_ready(
            [SimpleNamespace(poll=lambda: 1)],
            "http://127.0.0.1:1",
            "http://127.0.0.1:2",
        )


def test_clean_refuses_readably_while_the_service_runs(tmp_path, monkeypatch, capsys):
    """A precondition someone can act on must not arrive as a traceback."""
    import os
    import sys

    from levi import maintenance

    monkeypatch.setattr(maintenance, "STATE", tmp_path)
    (tmp_path / "server.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(sys, "argv", ["levi-clean"])

    assert maintenance.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Stop the LEVI service" in captured.err
    # It names the process and the command that stops it.
    assert str(os.getpid()) in captured.err
    assert "levi stop" in captured.err


def test_clean_runs_once_the_service_is_gone(tmp_path, monkeypatch):
    import sys

    from levi import maintenance

    monkeypatch.setattr(maintenance, "STATE", tmp_path)
    # A pid file left behind by a process that has exited is not a service.
    (tmp_path / "server.pid").write_text("2147483646")
    monkeypatch.setattr(sys, "argv", ["levi-clean"])
    assert maintenance.main() == 0


def test_clean_survives_a_corrupt_pid_file(tmp_path, monkeypatch):
    import sys

    from levi import maintenance

    monkeypatch.setattr(maintenance, "STATE", tmp_path)
    (tmp_path / "server.pid").write_text("not-a-pid\n")
    monkeypatch.setattr(sys, "argv", ["levi-clean"])
    assert maintenance.main() == 0


def test_clean_reports_artifacts_left_by_a_removed_dataset(tmp_path, monkeypatch):
    """Nobody's review work is deleted with its dataset, so it must be visible."""
    import json

    from levi import catalog, maintenance

    monkeypatch.setattr(maintenance, "STATE", tmp_path)
    monkeypatch.setattr(catalog, "STATE", tmp_path)
    (tmp_path / "datasets.json").write_text(
        json.dumps({"kept": {"path": str(tmp_path / "kept")}})
    )
    (tmp_path / "dataset_aliases.json").write_text(
        json.dumps({"0badc0de": "gone", "old": "kept"})
    )
    (tmp_path / "annotations/kept").mkdir(parents=True)
    (tmp_path / "annotations/gone").mkdir(parents=True)
    (tmp_path / "object_annotations/empty_shell").mkdir(parents=True)
    (tmp_path / "annotations/gone/episode_000000.json").write_text("{}")

    report = maintenance.orphans()
    assert [row["dataset"] for row in report["removable_empty"]] == ["empty_shell"]
    kept = report["keeps_content"]
    assert [row["dataset"] for row in kept] == ["gone"]
    assert kept[0]["files"] == 1
    # A redirect to a dataset nobody has any more leads nowhere.
    assert report["stale_aliases"] == {"0badc0de": "gone"}


def test_clean_removes_only_the_empty_shells(tmp_path, monkeypatch):
    import json

    from levi import catalog, maintenance

    monkeypatch.setattr(maintenance, "STATE", tmp_path)
    monkeypatch.setattr(catalog, "STATE", tmp_path)
    (tmp_path / "datasets.json").write_text(json.dumps({}))
    (tmp_path / "annotations/empty_shell").mkdir(parents=True)
    (tmp_path / "annotations/has_work").mkdir(parents=True)
    (tmp_path / "annotations/has_work/episode_000000.json").write_text("{}")

    maintenance.clean(apply=True)
    assert not (tmp_path / "annotations/empty_shell").exists()
    assert (tmp_path / "annotations/has_work/episode_000000.json").is_file()
