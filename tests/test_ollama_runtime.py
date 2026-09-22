"""Owned-process lifecycle with fake processes, no Ollama/GPU execution."""

import json
import signal

import pytest

from levi.inference import runtime


def manager(tmp_path):
    return runtime.OllamaRuntimeManager(tmp_path, tmp_path / "outputs/LEVI/workbench")


def test_status_never_starts_or_probes_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected process")
    )
    instance = manager(tmp_path)
    state = instance.status()
    assert not state["installed"] and not state["running"]
    assert not instance.root.exists()
    with pytest.raises(ValueError, match="not installed"):
        instance.start()


def test_managed_start_has_dedicated_paths_and_no_inherited_keys(tmp_path, monkeypatch):
    executable = tmp_path / "ollama-fixture"
    executable.write_text("fixture, never executed")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: str(executable))
    monkeypatch.setenv("LEVI_MODEL_API_KEY", "do-not-inherit")
    captured = {}

    class Process:
        pid = 42

        def poll(self):
            return None

    def start(arguments, **kwargs):
        captured.update(arguments=arguments, **kwargs)
        return Process()

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            captured["bind"] = address

    monkeypatch.setattr(runtime.subprocess, "Popen", start)
    monkeypatch.setattr(runtime.socket, "socket", Socket)
    monkeypatch.setattr(
        runtime.OllamaRuntimeManager,
        "identity",
        staticmethod(
            lambda pid: {
                "start_ticks": "test",
                "boot": "fixture",
                "executable": str(executable),
            }
        ),
    )
    instance = manager(tmp_path)
    assert instance.start()["running"]
    assert captured["arguments"] == [str(executable), "serve"]
    assert captured["env"]["OLLAMA_NO_CLOUD"] == "1"
    assert captured["env"]["OLLAMA_MODELS"] == str(tmp_path / "checkpoints/ollama")
    assert "LEVI_MODEL_API_KEY" not in captured["env"]
    assert captured["bind"] == ("127.0.0.1", 11435)
    with pytest.raises(ValueError, match="changing its port"):
        instance.start(11436)


def test_stop_rejects_pid_reuse(tmp_path, monkeypatch):
    instance = manager(tmp_path)
    instance.root.mkdir(parents=True)
    instance.record.write_text(
        json.dumps(
            {"pid": 42, "identity": {"old": True}, "url": "http://127.0.0.1:11435"}
        )
    )
    monkeypatch.setattr(instance, "identity", lambda _: {"new": True})
    monkeypatch.setattr(
        runtime.os, "pidfd_open", lambda _: pytest.fail("must not open another process")
    )
    assert not instance.stop()["running"]


def test_pidfd_pins_target_before_second_identity_check(tmp_path, monkeypatch):
    instance = manager(tmp_path)
    instance.root.mkdir(parents=True)
    instance.record.write_text(
        json.dumps(
            {"pid": 42, "identity": {"owned": True}, "url": "http://127.0.0.1:11435"}
        )
    )
    identities = iter([{"owned": True}, {"other": True}])
    monkeypatch.setattr(instance, "identity", lambda _: next(identities))
    monkeypatch.setattr(runtime.os, "pidfd_open", lambda _: 999)
    closed = []
    monkeypatch.setattr(runtime.os, "close", closed.append)
    monkeypatch.setattr(
        signal, "pidfd_send_signal", lambda *args: pytest.fail("identity changed")
    )
    with pytest.raises(ValueError, match="identity changed"):
        instance.stop()
    assert closed == [999]


def test_symlink_model_store_outside_workspace_is_refused(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "checkpoints").mkdir(parents=True)
    outside = tmp_path / "external-models"
    outside.mkdir()
    (workspace / "checkpoints/ollama").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Model storage"):
        runtime.OllamaRuntimeManager(workspace, workspace / "outputs/workbench")


def test_failed_ownership_record_stops_only_new_child(tmp_path, monkeypatch):
    executable = tmp_path / "ollama-fixture"
    executable.write_text("never executed")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: str(executable))
    actions = []

    class Process:
        pid = 314

        def poll(self):
            return None

        def terminate(self):
            actions.append("terminate")

        def wait(self, timeout):
            actions.append("wait")
            return 0

    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **kw: Process())

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            pass

    monkeypatch.setattr(runtime.socket, "socket", Socket)
    monkeypatch.setattr(
        runtime.OllamaRuntimeManager,
        "identity",
        staticmethod(lambda _: {"owned": True}),
    )

    def fail_replace(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    instance = manager(tmp_path)
    with pytest.raises(OSError, match="injected disk failure"):
        instance.start()
    assert actions == ["terminate", "wait"]
    assert not instance.record.exists()
    assert not instance.record.with_suffix(".pending").exists()
    assert 314 not in runtime._CHILDREN
