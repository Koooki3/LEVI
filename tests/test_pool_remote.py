"""Training pool, remote transfer: targets, rsync command, progress, cancel,
source refusal and real transfers (a local stand-in for ssh; real ssh to
localhost only when key login already works)."""

import json
import os
import stat
import subprocess
import time

import pytest

from levi.pool import jobs, remote


@pytest.fixture
def exports(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    monkeypatch.setenv("LEVI_WORKSPACE", str(workspace))
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path / "exports"))
    monkeypatch.delenv("LEVI_POOL_SSH", raising=False)
    folder = tmp_path / "exports" / "mix-v1"
    (folder / "meta").mkdir(parents=True)
    (folder / "pool_export.json").write_text(json.dumps({"name": "mix-v1"}))
    (folder / "meta" / "info.json").write_text("{}")
    (folder / "data.bin").write_bytes(os.urandom(256 * 1024))
    return {"folder": folder, "root": tmp_path, "workspace": workspace}


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """An ``ssh`` stand-in: drops the options and the host (recorded) and runs
    the remote command locally, like sshd's shell would."""
    script = tmp_path / "bin" / "fake-ssh"
    script.parent.mkdir()
    log = tmp_path / "ssh-hosts.log"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}.args\n'
        'while [ $# -gt 0 ]; do case "$1" in -o|-p|-l) shift 2;; -*) shift;; '
        "*) break;; esac; done\n"
        f'echo "$1" >> {log}\n'
        "shift\n"
        '[ -n "$FAKE_SSH_SLEEP" ] && sleep "$FAKE_SSH_SLEEP"\n'
        'exec sh -c "$*"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LEVI_POOL_SSH", str(script))
    return {"script": script, "log": log}


# ------------------------------------------------------------------ targets


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("gpu1:/data/train", {"host": "gpu1", "path": "/data/train"}),
        (
            "wk@10.0.0.7:~/datasets/",
            {"user": "wk", "host": "10.0.0.7", "path": "~/datasets"},
        ),
        ("a100-box.lab:/", {"host": "a100-box.lab", "path": "/"}),
    ],
)
def test_target_specs_accepted(spec, expected):
    target = remote.target_from("t1", {"spec": spec})
    assert target.model_dump(include=set(expected)) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "-oProxyCommand=touch /tmp/x:/data",  # option-like host
        "host:-rf",  # option-like path
        "host:relative/path",
        "user:secret@host:/data",  # a password in the spec
        "host:/data/../etc",
        "host:/data dir",
        "host;rm -rf ~:/data",
        "host:/data;reboot",
        "host:/data/$(id)",
        "-l@host:/data",
        "host",
        "",
    ],
)
def test_target_specs_refused(spec):
    with pytest.raises(ValueError):
        remote.target_from("t1", {"spec": spec})


def test_target_fields_and_names_are_strict():
    with pytest.raises(ValueError):
        remote.target_from("-t", {"spec": "gpu1:/data"})
    with pytest.raises(ValueError):
        remote.target_from("t", {"host": "gpu1", "path": "/d", "password": "pw"})
    with pytest.raises(ValueError):
        remote.target_from("t", {"host": "gpu1", "path": "/d", "port": 0})


def test_target_store_round_trip(exports):
    saved = remote.save(remote.target_from("gpu1", {"spec": "wk@gpu1:/data"}))
    assert saved["display"] == "wk@gpu1:/data"
    assert [t["name"] for t in remote.listing()] == ["gpu1"]
    stored = json.loads((exports["workspace"] / "pool" / "remotes.json").read_text())
    assert "password" not in json.dumps(stored)
    assert remote.get("gpu1").user == "wk"
    assert remote.delete("gpu1") and not remote.delete("gpu1")
    with pytest.raises(KeyError):
        remote.get("gpu1")


# ------------------------------------------------------------------ command


def test_rsync_command_is_an_argument_list_with_safe_ssh(exports):
    target = remote.target_from("gpu1", {"spec": "wk@gpu1:/data/", "port": 2222})
    command = remote.rsync_command(exports["folder"], target)
    assert command[0] == "rsync" and "--partial" in command
    assert "--info=progress2" in command and "--protect-args" in command
    shell = command[command.index("-e") + 1].split()
    assert shell[0] == "ssh"
    assert "BatchMode=yes" in shell and "StrictHostKeyChecking=yes" in shell
    assert "PasswordAuthentication=no" in shell
    assert "StrictHostKeyChecking=no" not in shell
    assert shell[-2:] == ["-p", "2222"]
    assert command[-3:] == [
        "--",
        f"{exports['folder']}/",
        "wk@gpu1:/data/mix-v1/",
    ]
    assert "--dry-run" in remote.rsync_command(exports["folder"], target, True)


def test_progress_parsing():
    line = "     12,345,678  42%   10.21MB/s    0:01:05 (xfr#3, to-chk=5/12)"
    assert remote.parse_progress(line) == {
        "bytes": 12345678,
        "percent": 42,
        "rate": "10.21MB/s",
        "eta_seconds": 65,
        "files_total": 12,
        "files_left": 5,
        "files_sent": 3,
    }
    assert (
        remote.parse_progress("          0   0%    0.00kB/s    0:00:00")["percent"] == 0
    )
    assert (
        remote.parse_progress("  1,024 100%  1.00MB/s  1:02:03 (xfr#1, ir-chk=0/4)")[
            "eta_seconds"
        ]
        == 3723
    )
    assert remote.parse_progress("sending incremental file list") is None
    assert remote.parse_progress("rsync error: some files could not be") is None
    chunks = [
        b"  1 0%  1kB/s  0:00:01\r  2 50% 1kB/s 0:00:01\r",
        b"  3 100% 1kB/s 0:00:00\n",
    ]

    class Stream:
        def read(self, _):
            return chunks.pop(0) if chunks else b""

    assert [remote.parse_progress(x)["percent"] for x in remote._updates(Stream())] == [
        0,
        50,
        100,
    ]


# ------------------------------------------------------------------ sources


def test_only_finished_pool_exports_can_be_pushed(exports, tmp_path):
    assert remote.check_source(exports["folder"]) == exports["folder"].resolve()
    plain = tmp_path / "exports" / "not-an-export"
    plain.mkdir()
    with pytest.raises(PermissionError, match="pool_export.json"):
        remote.check_source(plain)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "pool_export.json").write_text("{}")
    with pytest.raises(PermissionError, match="LEVI_EXPORT_ROOTS"):
        remote.check_source(outside)
    partial = tmp_path / "exports" / ".mix-v2.partial"
    partial.mkdir()
    (partial / "pool_export.json").write_text("{}")
    with pytest.raises(ValueError, match="finished"):
        remote.check_source(partial)
    link = tmp_path / "exports" / "link"
    link.symlink_to(exports["folder"])
    with pytest.raises(PermissionError, match="symbolic"):
        remote.check_source(link)
    with pytest.raises(ValueError):
        remote.check_source(tmp_path / "exports" / "missing")


# ------------------------------------------------------------------ transfer


def test_real_push_through_ssh_stand_in_and_resume(exports, fake_ssh, tmp_path):
    landing = tmp_path / "remote-disk"
    landing.mkdir()
    target = remote.target_from("box", {"spec": f"wk@box:{landing}"})
    dry = remote.push(exports["folder"], target, dry_run=True)
    assert dry["ok"] and not (landing / "mix-v1").exists()
    updates = []
    progress = tmp_path / "push.progress.json"
    result = remote.push(
        exports["folder"], target, progress_path=progress, echo=updates.append
    )
    assert result["ok"], result.get("error")
    assert result["destination"] == f"wk@box:{landing}/mix-v1/"
    copied = landing / "mix-v1"
    assert (copied / "data.bin").read_bytes() == (
        exports["folder"] / "data.bin"
    ).read_bytes()
    assert (copied / "pool_export.json").is_file()
    assert updates and updates[-1]["percent"] == 100
    assert json.loads(progress.read_text())["stage"] == "done"
    # rsync hands ssh the user with -l; the options reach ssh unchanged.
    assert fake_ssh["log"].read_text().split() == ["box", "box"]
    args = (fake_ssh["log"].parent / "ssh-hosts.log.args").read_text()
    assert "-o BatchMode=yes" in args and "-l wk box rsync --server" in args
    # Interrupted transfer: a damaged copy is repaired by pushing again.
    (copied / "data.bin").write_bytes(b"partial")
    again = remote.push(exports["folder"], target)
    assert again["ok"]
    assert (copied / "data.bin").read_bytes() == (
        exports["folder"] / "data.bin"
    ).read_bytes()


def test_push_failure_reports_a_hint(exports, fake_ssh, tmp_path):
    target = remote.target_from("box", {"spec": f"box:{tmp_path}/does/not/exist"})
    result = remote.push(exports["folder"], target)
    assert not result["ok"] and result["exit_code"] != 0 and result["error"]


def test_push_api_job_and_cancel(exports, fake_ssh, tmp_path, client, monkeypatch):
    landing = tmp_path / "remote-disk"
    landing.mkdir()
    body = {"spec": f"box:{landing}"}
    assert client.put("/api/levi/pool/remotes/box", json=body).status_code == 200
    refused = client.put(
        "/api/levi/pool/remotes/pw", json={**body, "password": "secret"}
    )
    assert refused.status_code == 422
    bad = client.put("/api/levi/pool/remotes/bad", json={"spec": "-oX=y:/data"})
    assert bad.status_code == 400
    listed = client.get("/api/levi/pool/remotes").json()["targets"]
    assert [t["name"] for t in listed] == ["box"]
    not_export = tmp_path / "exports" / "plain"
    not_export.mkdir()
    assert (
        client.post(
            "/api/levi/pool/push", json={"target": "box", "source": str(not_export)}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/levi/pool/push",
            json={"target": "nobody", "source": str(exports["folder"])},
        ).status_code
        == 404
    )
    job = client.post(
        "/api/levi/pool/push", json={"target": "box", "source": str(exports["folder"])}
    ).json()
    assert job["kind"] == "push" and job["status"] == "running"
    assert not jobs.wait_idle(120)
    done = client.get(f"/api/levi/pool/jobs/{job['id']}").json()
    assert done["status"] == "succeeded", (
        done.get("error"),
        (jobs.jobs_dir() / f"{job['id']}.log").read_text()[-2000:],
    )
    assert (landing / "mix-v1" / "data.bin").is_file()
    assert done["result"]["bytes"] > 0

    # Cancel: the stand-in waits before running rsync's remote side.
    monkeypatch.setenv("FAKE_SSH_SLEEP", "60")
    slow = client.post(
        "/api/levi/pool/push",
        json={"target": "box", "source": str(exports["folder"]), "dry_run": True},
    ).json()
    time.sleep(1.0)
    cancelled = client.post(f"/api/levi/pool/jobs/{slow['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelling"
    started = time.monotonic()
    assert not jobs.wait_idle(30)
    assert time.monotonic() - started < 20
    final = client.get(f"/api/levi/pool/jobs/{slow['id']}").json()
    assert final["status"] == "cancelled"
    alive = subprocess.run(
        ["pgrep", "-f", str(fake_ssh["script"])],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert not alive
    assert client.post(f"/api/levi/pool/jobs/{slow['id']}/cancel").status_code == 400
    assert client.delete("/api/levi/pool/remotes/box").status_code == 200


def _ssh_localhost_works() -> bool:
    try:
        return (
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "localhost",
                    "true",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(
    not _ssh_localhost_works(), reason="no key login to localhost over ssh"
)
def test_real_ssh_push_to_localhost(exports, tmp_path):
    landing = tmp_path / "ssh-landing"
    landing.mkdir()
    target = remote.target_from("local", {"spec": f"localhost:{landing}"})
    result = remote.push(exports["folder"], target)
    assert result["ok"], result.get("error")
    assert (landing / "mix-v1" / "data.bin").is_file()
