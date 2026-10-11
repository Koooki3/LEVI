"""Package A/B, preload and code-origin repros use only tiny private fake SDKs."""

import hashlib
import importlib.metadata
import os
import pickle
import py_compile
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from levi.live.owned_runtime import FrozenTorchFactory
from levi.live.owned_source import frozen_distribution_bundle
from levi.live.owned_vllm import BARRIER_FILES, FrozenVllmFactory


@pytest.fixture
def sdk_tree(tmp_path, monkeypatch):
    original = set(sys.modules)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    installed = tmp_path / "installed"
    for relative in (
        "torch/__init__.py",
        "torch/cuda/__init__.py",
        "vllm/__init__.py",
        "vllm/v1/__init__.py",
        "vllm/v1/engine/__init__.py",
        "vllm/v1/worker/__init__.py",
        "vllm/v1/executor/__init__.py",
    ):
        path = installed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    for relative in BARRIER_FILES:
        path = installed / "vllm" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# frozen fake barrier module\n")
    (installed / "torch/__init__.py").write_text(
        "from . import cuda\n__version__ = 'frozen'\n"
        "class device:\n    def __init__(self, name): self.name = name\n"
    )
    (installed / "torch/cuda/__init__.py").write_text(
        "def is_available(): return True\ndef synchronize(device=None): return None\n"
    )
    (installed / "vllm/v1/engine/async_llm.py").write_text(
        "from types import SimpleNamespace\nfrom torch.cuda import synchronize\n"
        "class AsyncLLM:\n"
        "    def __init__(self):\n"
        "        self.vllm_config=SimpleNamespace(parallel_config=SimpleNamespace("
        "tensor_parallel_size=1,pipeline_parallel_size=1,data_parallel_size=1),kv_transfer_config=None)\n"
        "    async def pause_generation(self, **kwargs): synchronize()\n"
        "    async def resume_generation(self): return None\n"
        "    async def generate(self, *args, **kwargs):\n"
        "        yield SimpleNamespace(outputs=[SimpleNamespace(text='fake')])\n"
        "    def shutdown(self): return None\n"
    )
    (installed / "vllm/__init__.py").write_text(
        "from .v1.engine.async_llm import AsyncLLM\n"
        "def SamplingParams(**kwargs): return kwargs\n"
    )
    source = installed / "closed_factory.py"
    source.write_text("def build(device=None, sdk=None): return sdk.AsyncLLM()\n")

    def manifest(package):
        rows = []
        for path in sorted((installed / package).rglob("*.py")):
            pieces = path.relative_to(installed).with_suffix("").parts
            if pieces[-1] == "__init__":
                pieces = pieces[:-1]
            rows.append(
                (".".join(pieces), hashlib.sha256(path.read_bytes()).hexdigest())
            )
        return tuple(rows)

    monkeypatch.syspath_prepend(str(installed))
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="frozen",
            locate_file=lambda package: installed / package,
        ),
    )
    factory = FrozenVllmFactory(
        "closed_factory",
        "build",
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "frozen",
        "cuda:0",
        "frozen",
        tuple(
            (
                relative,
                hashlib.sha256(
                    (installed / "vllm" / relative).read_bytes()
                ).hexdigest(),
            )
            for relative in BARRIER_FILES
        ),
        source_root=str(installed),
        torch_source_hashes=manifest("torch"),
        sdk_source_hashes=manifest("vllm"),
    )
    yield installed, factory
    for name in set(sys.modules) - original:
        if name.split(".", 1)[0] in {"torch", "vllm", "closed_factory"}:
            sys.modules.pop(name, None)


def test_actual_sdk_bundle_loads_only_manifest_compiled_functions(sdk_tree):
    _installed, factory = sdk_tree
    engine = factory.load()
    factory.require_engine(engine)
    assert engine.__class__.__module__ == "vllm.v1.engine.async_llm"


def test_distribution_a_cannot_authorize_shadow_package_b(
    sdk_tree, tmp_path, monkeypatch
):
    _installed, factory = sdk_tree
    shadow = tmp_path / "shadow"
    (shadow / "vllm").mkdir(parents=True)
    (shadow / "vllm/__init__.py").write_text(
        "raise RuntimeError('untrusted-sdk-must-not-execute')\n"
    )
    monkeypatch.syspath_prepend(str(shadow))
    with pytest.raises(ValueError, match="origin/loader"):
        factory.load()
    assert "vllm" not in sys.modules and "closed_factory" not in sys.modules


@pytest.mark.parametrize("name", ["vllm", "vllm.v1.engine.async_llm", "torch"])
def test_preloaded_sdk_is_rejected_before_factory_execution(
    sdk_tree, monkeypatch, name
):
    _installed, factory = sdk_tree
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    with pytest.raises(ValueError, match="preloaded"):
        factory.load()
    assert "closed_factory" not in sys.modules


def test_eight_barrier_files_are_insufficient_for_full_sdk_execution(sdk_tree):
    _installed, factory = sdk_tree
    reduced = tuple(
        row
        for row in factory.sdk_source_hashes
        if row[0]
        in {"vllm." + relative[:-3].replace("/", ".") for relative in BARRIER_FILES}
    )
    with pytest.raises(ValueError, match="every installed Python module"):
        replace(factory, sdk_source_hashes=reduced).load()


def test_replacing_pause_code_with_matching_module_filename_is_rejected(sdk_tree):
    installed, factory = sdk_tree
    engine = factory.load()
    namespace = {}
    exec(  # noqa: S102 - synthesize deliberately forged code in this CPU regression
        compile(
            "async def pause_generation(self, **kwargs): return None\n",
            str(installed / "vllm/v1/engine/async_llm.py"),
            "exec",
        ),
        namespace,
    )
    replacement = namespace["pause_generation"]
    replacement.__module__ = "vllm.v1.engine.async_llm"
    engine.__class__.pause_generation = replacement
    with pytest.raises(
        ValueError, match="critical SDK binding|compiled from the frozen source bytes"
    ):
        factory.require_engine(engine)


def test_native_or_third_party_dependency_is_refused_without_separate_binding(sdk_tree):
    _installed, factory = sdk_tree
    bundle = frozen_distribution_bundle(
        "vllm", "vllm", "frozen", factory.sdk_source_hashes
    )
    bundle.install()
    with pytest.raises(ImportError, match="outside the frozen"):
        bundle.checked_import("unbound_native_sdk")


def test_torch_default_entry_refuses_missing_sdk_manifest_without_import():
    factory = FrozenTorchFactory(
        "closed_factory", "build", "ab" * 32, "frozen", "cuda:0"
    )
    with pytest.raises(ValueError, match="complete installed SDK"):
        factory.load()


def test_sdk_timestamp_size_pyc_collision_never_executes_cached_pause(sdk_tree):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    frozen = source.read_bytes()
    unsafe = frozen.replace(b"synchronize()", b"None         ")
    assert unsafe != frozen and len(unsafe) == len(frozen)
    source.write_bytes(unsafe)
    stamp = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_bytes(frozen)
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    engine = factory.load()
    assert "synchronize" in engine.pause_generation.__func__.__code__.co_names
    factory.require_engine(engine)


class _SdkPipe:
    def __init__(self, incoming):
        self.incoming, self.sent = list(incoming), []
        self.closed = False

    def recv_bytes(self, limit):
        encoded = pickle.dumps(self.incoming.pop(0))
        assert len(encoded) <= limit
        return encoded

    def send_bytes(self, value):
        self.sent.append(pickle.loads(value))

    def close(self):
        self.closed = True


def _serve_normal_factory(monkeypatch, factory, pipe):
    from levi.live import owned_vllm
    from levi.live.workload_broker import WorkerIdentity

    monkeypatch.setattr(
        owned_vllm, "process_identity", lambda pid: WorkerIdentity(101, 1, "fake")
    )
    owned_vllm.serve_vllm(pipe, factory, "normal-load-chain")


def test_normal_factory_load_to_serve_generates_real_software_ack(
    sdk_tree, monkeypatch
):
    _installed, factory = sdk_tree
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.sent[0][0] == "ready" and pipe.sent[0][1].request_epoch == 0
    assert not any(message[0] == "error" for message in pipe.sent)


def test_frozen_factory_cannot_replace_actual_sdk_sync_before_first_ack(
    sdk_tree, monkeypatch
):
    installed, factory = sdk_tree
    source = installed / "closed_factory.py"
    source.write_text(
        "import torch.cuda\nimport vllm.v1.engine.async_llm as module\n"
        "def build(device=None, sdk=None):\n"
        "    engine=sdk.AsyncLLM()\n"
        "    def replacement(*args, **kwargs): return None\n"
        "    torch.cuda.synchronize=replacement\n"
        "    module.synchronize=replacement\n"
        "    return engine\n"
    )
    factory = replace(
        factory, code_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.sent and all(message[1] is None for message in pipe.sent)
    assert not any(message[0] in ("ready", "idle", "result") for message in pipe.sent)


def test_runtime_global_alias_replacement_is_rejected_before_next_ack(
    sdk_tree, monkeypatch
):
    _installed, factory = sdk_tree
    calls = []

    class MutatingPipe(_SdkPipe):
        def send_bytes(self, value):
            super().send_bytes(value)
            if self.sent[-1][0] == "ready":
                module = sys.modules["vllm.v1.engine.async_llm"]
                module.synchronize = lambda: calls.append("unfrozen")

    pipe = MutatingPipe([("enrolled", "normal-load-chain"), ("barrier", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.sent[0][0] == "ready"
    assert any(message[0] == "error" for message in pipe.sent)
    assert not any(message[0] == "idle" for message in pipe.sent)
    assert calls == []


def test_sync_replacing_itself_is_rejected_by_post_pause_binding(sdk_tree, monkeypatch):
    installed, factory = sdk_tree
    source = installed / "torch/cuda/__init__.py"
    source.write_text(
        "def is_available(): return True\n"
        "def synchronize(device=None):\n"
        "    global synchronize\n"
        "    synchronize = lambda *args: None\n"
    )
    hashes = tuple(
        (
            name,
            hashlib.sha256(source.read_bytes()).hexdigest()
            if name == "torch.cuda"
            else sha,
        )
        for name, sha in factory.torch_source_hashes
    )
    factory = replace(factory, torch_source_hashes=hashes)
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.sent and all(message[1] is None for message in pipe.sent)
    assert not any(message[0] == "ready" for message in pipe.sent)
