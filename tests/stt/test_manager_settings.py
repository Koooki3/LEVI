"""``levi/stt/settings.py``: the three settings and what ``worker_state`` says
about the files, without importing anything heavy and without naming a path."""

import subprocess
import sys
from pathlib import Path

import pytest

from levi import paths
from levi.stt import settings

PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ("LEVI_STT_WORKER_PYTHON", "LEVI_STT_MODEL_DIR", "LEVI_STT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "ROOT", tmp_path / "workspace")
    monkeypatch.setattr(paths, "CHECKPOINTS", tmp_path / "workspace" / "checkpoints")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A worker project with its source file."""
    root = tmp_path / "stt"
    (root / "levi_stt_worker").mkdir(parents=True)
    (root / "levi_stt_worker" / "cli.py").write_text("")
    monkeypatch.setattr(settings, "worker_project", lambda: root)
    return root


def install_python(project):
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    return python


def install_model(name=settings.DEFAULT_MODEL):
    folder = settings.model_dir() / name
    folder.mkdir(parents=True)
    (folder / settings.MANIFEST).write_text("")
    return folder


def test_the_defaults():
    assert settings.model_name() == "qwen3-asr-1.7b-hf"
    assert settings.provider() == "qwen3-asr"
    assert settings.model_dir() == (paths.CHECKPOINTS / "stt").resolve()
    assert settings.worker_python() == PROJECT / "integrations/stt/.venv/bin/python"


def test_the_three_settings_are_read_from_the_environment(monkeypatch, tmp_path):
    inside = paths.ROOT / "elsewhere" / "stt"
    monkeypatch.setenv("LEVI_STT_WORKER_PYTHON", str(tmp_path / "py"))
    monkeypatch.setenv("LEVI_STT_MODEL_DIR", str(inside))
    monkeypatch.setenv("LEVI_STT_MODEL", "other-model")
    assert settings.worker_python() == tmp_path / "py"
    assert settings.model_dir() == inside.resolve()
    assert settings.model_name() == "other-model"


def test_the_model_folder_must_stay_inside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEVI_STT_MODEL_DIR", str(tmp_path / "outside"))
    with pytest.raises(ValueError):
        settings.model_dir()


def test_the_numbers_of_the_contract():
    assert settings.MAX_SECONDS == 10 and settings.MAX_BYTES == 400_000
    assert settings.IDLE_SECONDS == 120 and settings.START_TIMEOUT_S == 45
    assert settings.REQUEST_TIMEOUT_S == 20 and settings.RATE_PER_MINUTE == 10
    assert settings.STT_VRAM_MIB == 5000 and settings.DEVICE == "cuda"


def test_without_the_worker_source_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "worker_project", lambda: tmp_path / "none")
    assert settings.worker_state() == settings.WorkerState(
        False, False, "worker_source_missing"
    )


def test_without_the_worker_python_nothing_is_installed(project):
    assert settings.worker_state() == settings.WorkerState(
        False, False, "worker_python_missing"
    )


def test_a_worker_without_the_model_says_so(project):
    install_python(project)
    assert settings.worker_state() == settings.WorkerState(True, False, "model_missing")
    folder = settings.model_dir() / settings.DEFAULT_MODEL
    folder.mkdir(parents=True)
    assert settings.worker_state().model is False, "a folder without a manifest"


def test_a_worker_with_its_model_and_manifest_is_installed(project):
    install_python(project)
    install_model()
    assert settings.worker_state() == settings.WorkerState(True, True, None)


@pytest.mark.parametrize("name", ["../x", "a/b", "-x", "x y", "a" * 65, ".."])
def test_a_model_name_that_could_leave_the_folder_is_refused(
    project, monkeypatch, name
):
    install_python(project)
    monkeypatch.setenv("LEVI_STT_MODEL", name)
    assert settings.worker_state().reason == "model_name_invalid"


def test_a_model_folder_outside_the_workspace_is_reported_not_raised(
    project, monkeypatch, tmp_path
):
    install_python(project)
    monkeypatch.setenv("LEVI_STT_MODEL_DIR", str(tmp_path / "outside"))
    assert settings.worker_state() == settings.WorkerState(
        True, False, "model_dir_outside_workspace"
    )


def test_the_fake_model_needs_neither_a_python_nor_a_folder(project, monkeypatch):
    monkeypatch.setenv("LEVI_STT_MODEL", "fake")
    assert settings.provider() == "fake"
    assert settings.worker_state() == settings.WorkerState(True, True, None)


def test_the_answer_names_no_path(project):
    state = settings.worker_state()
    assert str(project) not in repr(state)


def test_the_command_line_of_a_real_model(project, monkeypatch):
    python = install_python(project)
    monkeypatch.setenv("LEVI_STT_MODEL", "my-model")
    assert settings.command("8-15") == [
        str(python),
        "-m",
        "levi_stt_worker.cli",
        "serve",
        "--model-dir",
        str(settings.model_dir()),
        "--model",
        "my-model",
        "--device",
        "cuda",
        "--max-seconds",
        "10",
        "--cpu-affinity",
        "8-15",
        "--provider",
        "qwen3-asr",
    ]


def test_the_fake_provider_runs_with_the_python_of_levi(monkeypatch):
    monkeypatch.setenv("LEVI_STT_MODEL", "fake")
    argv = settings.command("")
    assert argv[0] == sys.executable
    assert argv[-2:] == ["--provider", "fake"]
    assert argv[argv.index("--cpu-affinity") + 1] == ""


def test_the_core_modules_import_without_the_model_stack():
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT)!r}); "
        "import levi.stt.manager, levi.stt.settings, levi.stt.wav, "
        "levi.marvel0.speech_api; levi.stt.settings.worker_state(); "
        "heavy = ('torch', 'transformers', 'ctranslate2', 'faster_whisper'); "
        "bad = [m for m in heavy if m in sys.modules]; assert not bad, bad"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
