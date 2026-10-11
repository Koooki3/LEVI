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


def _assert_clean_sdk(before, preserved=None):
    assert sys.meta_path == before
    preserved = preserved or {}
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] in {"torch", "vllm", "closed_factory"}:
            assert preserved.get(name) is module


@pytest.mark.parametrize("name", ["torch", "vllm", "closed_factory"])
def test_failed_install_rolls_back_owned_hooks_and_preserves_preload(
    sdk_tree, monkeypatch, name
):
    _installed, factory = sdk_tree
    before = list(sys.meta_path)
    existing = ModuleType(name)
    monkeypatch.setitem(sys.modules, name, existing)
    with pytest.raises(ValueError, match="preloaded"):
        factory.load()
    _assert_clean_sdk(before, {name: existing})


@pytest.mark.parametrize("stage", ["module", "build", "topology", "critical"])
def test_actual_factory_failure_rolls_back_entire_source_transaction(sdk_tree, stage):
    installed, factory = sdk_tree
    before = list(sys.meta_path)
    source = installed / "closed_factory.py"
    code = {
        "module": "raise RuntimeError('module failed')\ndef build(**kwargs): pass\n",
        "build": "def build(**kwargs): raise RuntimeError('build failed')\n",
        "topology": (
            "def build(device=None, sdk=None):\n"
            "    engine=sdk.AsyncLLM()\n"
            "    engine.vllm_config.parallel_config.tensor_parallel_size=2\n"
            "    return engine\n"
        ),
        "critical": (
            "import torch.cuda\n"
            "def build(device=None, sdk=None):\n"
            "    torch.cuda.synchronize=lambda *args: None\n"
            "    return sdk.AsyncLLM()\n"
        ),
    }[stage]
    source.write_text(code)
    factory = replace(
        factory, code_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    with pytest.raises((ValueError, RuntimeError)):
        factory.load()
    _assert_clean_sdk(before)


def test_successful_engine_close_is_idempotent_and_exact_owner_only(sdk_tree):
    _installed, factory = sdk_tree
    before = list(sys.meta_path)
    engine = factory.load()
    external = ModuleType("torch.cuda")
    sys.modules["torch.cuda"] = external
    factory.close_engine(engine)
    factory.close_engine(engine)
    _assert_clean_sdk(before, {"torch.cuda": external})
    with pytest.raises(ImportError, match="lifetime is closed"):
        engine._owned_sdk_bundles[0].import_module("torch")


def test_normal_serve_closes_factory_and_sdk_modules(sdk_tree, monkeypatch):
    _installed, factory = sdk_tree
    before = list(sys.meta_path)
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.closed and pipe.sent[0][0] == "ready"
    _assert_clean_sdk(before)


def test_failed_barrier_serve_also_closes_entire_source_lifetime(sdk_tree, monkeypatch):
    _installed, factory = sdk_tree
    before = list(sys.meta_path)

    class MutatingPipe(_SdkPipe):
        def send_bytes(self, value):
            super().send_bytes(value)
            if self.sent[-1][0] == "ready":
                sys.modules["vllm.v1.engine.async_llm"].synchronize = lambda: None

    pipe = MutatingPipe([("enrolled", "normal-load-chain"), ("barrier", 1, None)])
    _serve_normal_factory(monkeypatch, factory, pipe)
    assert pipe.closed and any(message[0] == "error" for message in pipe.sent)
    _assert_clean_sdk(before)


def test_closed_bundle_refuses_before_any_ordinary_import(sdk_tree, monkeypatch):
    _installed, factory = sdk_tree
    engine = factory.load()
    factory.close_engine(engine)

    def unsafe_import(*args, **kwargs):
        raise AssertionError("closed bundle reached ordinary import")

    monkeypatch.setattr(importlib, "import_module", unsafe_import)
    with pytest.raises(ImportError, match="lifetime is closed"):
        engine._owned_sdk_bundles[0].import_module("torch")


def test_standalone_factory_has_explicit_owned_cleanup_lifetime(sdk_tree):
    _installed, factory = sdk_tree
    before = list(sys.meta_path)
    build = factory.reviewed_factory()
    assert "closed_factory" in sys.modules
    build._owned_source_lifetime.close()
    build._owned_source_lifetime.close()
    _assert_clean_sdk(before)


@pytest.mark.parametrize("valid", [False, True])
def test_torch_load_uses_same_transaction_for_failure_and_success(sdk_tree, valid):
    installed, vllm_factory = sdk_tree
    before = list(sys.meta_path)
    source = installed / "closed_factory.py"
    source.write_text(
        "def build(device=None, sdk=None):\n"
        + (
            "    def model(payload): return payload\n    return model\n"
            if valid
            else "    return None\n"
        )
    )
    factory = FrozenTorchFactory(
        "closed_factory",
        "build",
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "frozen",
        "cuda:0",
        source_root=str(installed),
        torch_source_hashes=vllm_factory.torch_source_hashes,
    )
    if valid:
        _torch, _device, model = factory.load()
        model._owned_source_lifetime.close()
    else:
        with pytest.raises(TypeError, match="callable"):
            factory.load()
    _assert_clean_sdk(before)


def _refreeze_sdk(installed, factory):
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

    return replace(
        factory,
        code_sha256=hashlib.sha256(
            (installed / "closed_factory.py").read_bytes()
        ).hexdigest(),
        sdk_source_hashes=manifest("vllm"),
        torch_source_hashes=manifest("torch"),
        barrier_hashes=tuple(
            (
                relative,
                hashlib.sha256(
                    (installed / "vllm" / relative).read_bytes()
                ).hexdigest(),
            )
            for relative in BARRIER_FILES
        ),
    )


@pytest.mark.parametrize("case", ["instance", "holder", "default", "closure"])
def test_normal_factory_cannot_change_actual_critical_callable(
    sdk_tree, monkeypatch, case
):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    code = source.read_text()
    if case == "instance":
        code += "    async def alternate_pause(self, **kwargs): return None\n"
        mutation = "engine.pause_generation=engine.alternate_pause"
    elif case == "holder":
        code = code.replace(
            "class AsyncLLM:",
            "HOLDER=SimpleNamespace(sync=synchronize)\nclass AsyncLLM:",
        ).replace("synchronize()", "HOLDER.sync()")
        mutation = "module.HOLDER.sync=lambda: None"
    elif case == "default":
        code = code.replace(
            "class AsyncLLM:",
            "def pause_helper(holder=SimpleNamespace(sync=synchronize)): holder.sync()\nclass AsyncLLM:",
        ).replace("synchronize()", "pause_helper()")
        mutation = "module.pause_helper.__defaults__[0].sync=lambda: None"
    else:
        code = code.replace(
            "class AsyncLLM:",
            "def make_pause():\n    holder=SimpleNamespace(sync=synchronize)\n    async def pause(self, **kwargs): holder.sync()\n    return pause\nclass AsyncLLM:",
        )
        code += "AsyncLLM.pause_generation=make_pause()\n"
        mutation = "engine.pause_generation.__func__.__closure__[0].cell_contents.sync=lambda: None"
    source.write_text(code)
    (installed / "closed_factory.py").write_text(
        "import vllm.v1.engine.async_llm as module\n"
        "def build(device=None, sdk=None):\n"
        "    engine=sdk.AsyncLLM()\n    " + mutation + "\n    return engine\n"
    )
    before = list(sys.meta_path)
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert pipe.sent and all(message[1] is None for message in pipe.sent)
    assert not any(message[0] in ("ready", "idle", "result") for message in pipe.sent)
    _assert_clean_sdk(before)


@pytest.mark.parametrize("shadow", ["plain-function", "other-self", "same-self"])
def test_runtime_instance_shadow_is_refused_before_next_ack(
    sdk_tree, monkeypatch, shadow
):
    _installed, factory = sdk_tree

    class MutatingPipe(_SdkPipe):
        def send_bytes(self, value):
            super().send_bytes(value)
            if self.sent[-1][0] == "ready":
                # The observer changes only CPU test state; load/serve/binding stay real.
                instance = sys.modules["closed_factory"].LAST
                function = type(instance).pause_generation
                instance.pause_generation = {
                    "plain-function": function,
                    "other-self": function.__get__(type(instance)(), type(instance)),
                    "same-self": function.__get__(instance, type(instance)),
                }[shadow]

    installed = sdk_tree[0]
    (installed / "closed_factory.py").write_text(
        "LAST=None\ndef build(device=None, sdk=None):\n"
        "    global LAST\n    LAST=sdk.AsyncLLM()\n    return LAST\n"
    )
    pipe = MutatingPipe([("enrolled", "normal-load-chain"), ("barrier", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert pipe.sent[0][0] == "ready"
    assert any(message[0] == "error" for message in pipe.sent)
    assert not any(message[0] == "idle" for message in pipe.sent)


def test_transitive_callable_holder_counter_state_can_change(sdk_tree, monkeypatch):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    source.write_text(
        source.read_text()
        .replace(
            "class AsyncLLM:",
            "HOLDER=SimpleNamespace(inner=[{'sync':synchronize}], counter=0, other=0, stats={'count':0})\nclass AsyncLLM:",
        )
        .replace(
            "synchronize()",
            "HOLDER.inner[0]['sync'](); HOLDER.counter+=1; HOLDER.other+=1; HOLDER.stats['count']+=1",
        )
    )
    pipe = _SdkPipe(
        [("enrolled", "normal-load-chain"), ("barrier", 1, None), ("stop", 2, None)]
    )
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert [message[0] for message in pipe.sent] == ["ready", "idle"]


def test_runtime_nested_callable_holder_mutation_is_refused(sdk_tree, monkeypatch):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    source.write_text(
        source.read_text()
        .replace(
            "class AsyncLLM:",
            "HOLDER=SimpleNamespace(inner=[{'sync':synchronize}])\nclass AsyncLLM:",
        )
        .replace("synchronize()", "HOLDER.inner[0]['sync']()")
    )

    class MutatingPipe(_SdkPipe):
        def send_bytes(self, value):
            super().send_bytes(value)
            if self.sent[-1][0] == "ready":
                sys.modules["vllm.v1.engine.async_llm"].HOLDER.inner[0]["sync"] = (
                    lambda: None
                )

    pipe = MutatingPipe([("enrolled", "normal-load-chain"), ("barrier", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert pipe.sent[0][0] == "ready" and pipe.sent[-1][0] == "error"
    assert not any(message[0] == "idle" for message in pipe.sent)


@pytest.mark.parametrize("descriptor", ["classmethod", "staticmethod"])
def test_legal_critical_method_descriptor_semantics_are_preserved(
    sdk_tree, monkeypatch, descriptor
):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    signature = "cls, **kwargs" if descriptor == "classmethod" else "**kwargs"
    source.write_text(
        source.read_text().replace(
            "    async def pause_generation(self, **kwargs): synchronize()",
            "    @"
            + descriptor
            + "\n    async def pause_generation("
            + signature
            + "): synchronize()",
        )
    )
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert [message[0] for message in pipe.sent] == ["ready"]


def test_unknown_callable_holder_object_is_explicitly_refused(sdk_tree, monkeypatch):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    source.write_text(
        source.read_text()
        .replace(
            "class AsyncLLM:",
            "class Holder:\n    def __init__(self): self.sync=synchronize\nHOLDER=Holder()\nclass AsyncLLM:",
        )
        .replace("synchronize()", "HOLDER.sync()")
    )
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert pipe.sent and all(message[1] is None for message in pipe.sent)
    assert pipe.sent[0][0] == "error"


def test_instance_rebinding_during_pause_refuses_post_barrier_ack(
    sdk_tree, monkeypatch
):
    installed, factory = sdk_tree
    source = installed / "vllm/v1/engine/async_llm.py"
    source.write_text(
        source.read_text().replace(
            "async def pause_generation(self, **kwargs): synchronize()",
            "async def pause_generation(self, **kwargs): synchronize(); self.pause_generation=self.alternate_pause",
        )
        + "    async def alternate_pause(self, **kwargs): return None\n"
    )
    pipe = _SdkPipe([("enrolled", "normal-load-chain"), ("stop", 1, None)])
    _serve_normal_factory(monkeypatch, _refreeze_sdk(installed, factory), pipe)
    assert pipe.sent and all(message[1] is None for message in pipe.sent)
    assert not any(message[0] in ("ready", "idle", "result") for message in pipe.sent)
