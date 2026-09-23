"""GPU guardian: yield to others' compute, learn how they come and go, resume."""

import json
import subprocess

import pytest

from levi.agent.schema import ProviderConfig
from levi.inference import gpu

ACTOR = (
    "python3 -m scripts.train --algorithm=conrft --exp_name=insert_the_bread --actor"
)


@pytest.fixture(autouse=True)
def guarded(monkeypatch, tmp_path):
    # conftest allows sharing for every other test; these test the guardian.
    monkeypatch.delenv("LEVI_GPU_SHARING", raising=False)
    monkeypatch.setattr(gpu, "_state_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu, "_parent", lambda pid: None)


def config(url="http://127.0.0.1:11435"):
    return ProviderConfig(
        name="qwen-local",
        kind="ollama",
        base_url=url,
        model="qwen3.5:4b",
        vision=True,
        structured_output=True,
        allow_localhost=True,
    )


def on_gpu(monkeypatch, apps, commands, total=16000, used=None, util=5, at=None):
    """apps: [(pid, memory_mib)]; commands: pid -> command line."""
    used = used if used is not None else sum(m for _, m in apps) + 500

    def smi(query, fields):
        if query == "compute-apps":
            return [[str(pid), "python3", str(mib)] for pid, mib in apps]
        return [[str(total), str(used), str(util)]]

    monkeypatch.setattr(gpu, "_smi", smi)
    monkeypatch.setattr(gpu, "_cmdline", lambda pid: commands.get(pid, ""))
    if at is not None:
        monkeypatch.setattr(gpu.time, "time", lambda: at)


def test_cpu_only_mode_never_probes_or_starts_guardian(monkeypatch):
    from levi.agent import objects

    monkeypatch.setenv("LEVI_CPU_ONLY", "1")

    def forbidden(*args, **kwargs):
        raise AssertionError("CPU-only mode must not inspect accelerators")

    monkeypatch.setattr(gpu, "_smi", forbidden)
    monkeypatch.setattr(gpu.shutil, "which", forbidden)
    monkeypatch.setattr(objects.subprocess, "run", forbidden)
    assert gpu.sample()["cpu_only"] is True
    with pytest.raises(gpu.GpuBusy, match="LEVI_CPU_ONLY"):
        gpu.require_free(config())
    watch = gpu.Watch(None).start()
    assert watch.thread is None
    assert objects.gpu_headroom()["known"] is False
    watch.close()


def test_a_protected_workload_blocks_and_is_named(monkeypatch):
    on_gpu(
        monkeypatch, [(737719, 8500)], {737719: ACTOR + " --checkpoint_path=/media/x"}
    )
    with pytest.raises(gpu.GpuBusy, match="conrft.*protected"):
        gpu.require_free(config())


def test_ollamas_own_model_process_is_not_someone_else(monkeypatch):
    on_gpu(
        monkeypatch,
        [(810300, 3900)],
        {
            810300: "/opt/ollama/lib/ollama/llama-server --model x",
            810188: "/opt/ollama/bin/ollama serve",
        },
    )
    monkeypatch.setattr(gpu, "_parent", lambda pid: 810188)
    assert gpu.others() == []
    assert gpu.require_free(config())["state"] == "free"


def test_a_process_merely_named_like_a_runner_is_not_trusted(monkeypatch):
    on_gpu(
        monkeypatch,
        [(5, 3900)],
        {5: "/home/bofang/llama-server --model x", 1: "/sbin/init"},
    )
    monkeypatch.setattr(gpu, "_parent", lambda pid: 1)
    assert [row["pid"] for row in gpu.others()] == [5]


def test_unreadable_nvidia_smi_is_not_treated_as_free(monkeypatch):
    def broken(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 10)

    monkeypatch.setattr(gpu, "_smi", broken)
    with pytest.raises(gpu.GpuBusy, match="unreadable"):
        gpu.require_free(config())


def test_a_remote_endpoint_or_an_explicit_override_is_not_guarded(monkeypatch):
    on_gpu(monkeypatch, [(1, 100)], {1: "python3 train.py"})
    gpu.require_free(config("http://10.0.0.9:11434"))
    monkeypatch.setenv("LEVI_GPU_SHARING", "allow")
    gpu.require_free(config())


def test_a_restarted_workload_keeps_its_signature():
    first = gpu.signature(ACTOR + " --checkpoint_path=/media/T9/run/ckpt_100")
    second = gpu.signature(ACTOR + " --checkpoint_path=/media/T9/run/ckpt_200")
    assert first == second and "conrft" in first and "/media" not in first


def entry(gaps, last_end):
    """Appearance intervals ending at last_end, separated by the given gaps."""
    intervals, end = [], last_end
    for gap in reversed([None, *gaps]):
        start = end - 300
        intervals.insert(0, [start, end])
        if gap is not None:
            end = start - gap
    return {"intervals": intervals}


def test_the_window_is_learned_from_how_a_workload_restarts():
    quiet, basis = gpu.learned_quiet(entry([20, 25, 30, 22], 1000))
    assert basis["basis"] == "learned" and quiet == gpu.MIN_QUIET
    quiet, basis = gpu.learned_quiet(entry([200, 240, 180], 1000))
    assert quiet == pytest.approx(1.5 * 240) and basis["gap_p90_seconds"] == 240
    quiet, basis = gpu.learned_quiet(entry([30], 1000))
    assert basis["basis"] == "default" and quiet == gpu.DEFAULT_QUIET
    # A gap longer than a session is a new session, not a restart.
    quiet, basis = gpu.learned_quiet(entry([5000, 5000], 1000))
    assert basis["basis"] == "default"


def test_the_guardian_cools_down_then_opens(monkeypatch):
    sig = gpu.signature(ACTOR)
    history = {"workloads": {sig: entry([20, 25, 30], 10_000)}}
    gpu._write(gpu._history_path(), history)
    on_gpu(monkeypatch, [], {}, at=10_030)
    verdict = gpu.status()
    assert verdict["state"] == "cooling" and 0 < verdict["wait_seconds"] <= 31
    assert "learned from 3 restarts" in verdict["reason"]
    on_gpu(monkeypatch, [], {}, at=10_000 + gpu.MIN_QUIET + 1)
    assert gpu.status()["state"] == "free"


def test_the_guardian_records_appearances_across_samples(monkeypatch):
    on_gpu(monkeypatch, [(7, 8000)], {7: ACTOR}, at=100)
    gpu.status()
    on_gpu(monkeypatch, [], {}, at=130)
    gpu.status()
    on_gpu(monkeypatch, [(8, 8000)], {8: ACTOR + " --checkpoint_path=/x/2"}, at=150)
    gpu.status()
    intervals = gpu._read(gpu._history_path(), {})["workloads"][gpu.signature(ACTOR)][
        "intervals"
    ]
    assert intervals == [[100, 130], [150, None]]


def write_policy(rules, **share):
    gpu._write(
        gpu._policy_path(),
        {"rules": rules, "share": share} if share else {"rules": rules},
    )


def test_shareable_work_coexists_only_with_room(monkeypatch):
    write_policy([{"match": "jupyter", "class": "share"}])
    on_gpu(monkeypatch, [(9, 2000)], {9: "python3 -m jupyter kernel"}, util=10)
    assert gpu.require_free(config())["state"] == "shared"
    on_gpu(monkeypatch, [(9, 2000)], {9: "python3 -m jupyter kernel"}, util=90)
    with pytest.raises(gpu.GpuBusy, match="90% busy"):
        gpu.require_free(config())
    on_gpu(monkeypatch, [(9, 12000)], {9: "python3 -m jupyter kernel"}, util=10)
    with pytest.raises(gpu.GpuBusy, match="memory"):
        gpu.require_free(config())


def test_ignored_work_does_not_count(monkeypatch):
    write_policy([{"match": "Xorg", "class": "ignore"}])
    on_gpu(monkeypatch, [(3, 40)], {3: "/usr/lib/xorg/Xorg :0"})
    assert gpu.require_free(config())["state"] == "free"


class Store:
    def __init__(self, runs):
        self.runs = runs

    def list(self, kind):
        return [config().model_dump()] if kind == "providers" else self.runs


def test_the_watch_unloads_for_protected_work_and_resumes_when_clear(monkeypatch):
    unloaded, launched = [], []
    monkeypatch.setattr(
        gpu, "unload_all", lambda c: unloaded.append(c.name) or ["qwen3.5:4b"]
    )

    class Bench:
        def launch(self, run_id, pilot):
            launched.append((run_id, pilot))

    runs = [
        {
            "id": "temporal-a",
            "status": "blocked",
            "blocked_by": "gpu",
            "plan": {"pilot_episode": 0, "pilot_review": None},
            "completed": [],
        },
        {
            "id": "temporal-b",
            "status": "blocked",
            "blocked_by": None,
            "plan": {},
            "completed": [],
        },
    ]
    watch = gpu.Watch(Store(runs), workbench=Bench())
    on_gpu(monkeypatch, [(7, 8000)], {7: ACTOR}, at=1000)
    busy = watch.tick()
    assert busy["state"] == "busy" and unloaded == ["qwen-local"] and not launched
    # The first sample without it records when it left; the window runs from there.
    on_gpu(monkeypatch, [], {}, at=1010)
    assert watch.tick()["state"] == "cooling" and not launched
    on_gpu(monkeypatch, [], {}, at=1010 + gpu.DEFAULT_QUIET + 1)
    clear = watch.tick()
    assert clear["state"] == "free" and launched == [("temporal-a", True)]
    assert watch.interval() == watch.IDLE_INTERVAL


def test_a_request_cut_off_by_the_guardian_is_a_preemption(monkeypatch):
    from levi.inference.provider import OllamaProvider

    def cut(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(OllamaProvider, "_chat", staticmethod(cut))
    on_gpu(monkeypatch, [(7, 8000)], {7: ACTOR})
    budget = type("B", (), {"max_tokens": 1000, "max_seconds": 30})()
    cfg = config().model_copy(update={"model_digest": "a" * 64})
    monkeypatch.setattr(gpu, "require_free", lambda c=None: None)
    with pytest.raises(gpu.GpuBusy, match="Preempted"):
        OllamaProvider().generate(cfg, "goal", {"workflow": {}}, [], "/tmp", budget)


def test_the_report_explains_itself(monkeypatch):
    on_gpu(monkeypatch, [(7, 8000)], {7: ACTOR})
    value = gpu.report()
    assert value["decision"]["state"] == "busy"
    assert value["workloads"][0]["class"] == "protect"
    assert json.dumps(value)
