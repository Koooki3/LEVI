"""Only private tmp packages; no real SDK import, model or subprocess."""

import hashlib
import importlib.machinery
import os
import py_compile
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from levi.live.owned_runtime import FrozenTorchFactory


@pytest.fixture
def frozen_paths(monkeypatch):
    original = set(sys.modules)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    yield
    for name in set(sys.modules) - original:
        if name.startswith("closed_factory"):
            sys.modules.pop(name, None)


def factory(root, module="closed_factory", source_hashes=()):
    source = root.joinpath(*module.split(".")).with_suffix(".py")
    return FrozenTorchFactory(
        module,
        "build",
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "unused",
        "cuda:0",
        source_root=str(root),
        source_hashes=source_hashes,
    )


def test_timestamp_size_pyc_collision_executes_only_frozen_source(
    tmp_path, monkeypatch, frozen_paths
):
    source = tmp_path / "closed_factory.py"
    old = "def build(device=None, sdk=None): return 'unsafe'\n"
    frozen = "def build(device=None, sdk=None): return 'frozen'\n"
    assert len(old) == len(frozen)
    source.write_text(old)
    stamp = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_text(frozen)
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    monkeypatch.syspath_prepend(str(tmp_path))
    assert factory(tmp_path).reviewed_factory()() == "frozen"
    assert sys.modules["closed_factory"].__cached__ is None


def test_preloaded_factory_is_rejected_even_with_matching_origin(
    tmp_path, monkeypatch, frozen_paths
):
    source = tmp_path / "closed_factory.py"
    source.write_text("def build(device=None, sdk=None): return 'frozen'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    loaded = ModuleType("closed_factory")
    loaded.__file__ = str(source)
    monkeypatch.setitem(sys.modules, "closed_factory", loaded)
    with pytest.raises(ValueError, match="preloaded"):
        factory(tmp_path).reviewed_factory()


def test_custom_path_loader_cannot_execute_the_frozen_factory(
    tmp_path, monkeypatch, frozen_paths
):
    (tmp_path / "closed_factory.py").write_text(
        "raise RuntimeError('must not execute')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        importlib.machinery.PathFinder,
        "find_spec",
        lambda *args: SimpleNamespace(
            loader=object(), origin=str(tmp_path / "closed_factory.py")
        ),
    )
    with pytest.raises(ValueError, match="origin/loader"):
        factory(tmp_path).reviewed_factory()


def test_parent_package_and_local_dependency_are_executed_from_same_manifest(
    tmp_path, monkeypatch, frozen_paths
):
    package = tmp_path / "closed_factory"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'frozen-parent'\n")
    (package / "helper.py").write_text("VALUE = 'frozen-helper'\n")
    (package / "model.py").write_text(
        "from . import VALUE\nfrom .helper import VALUE as OTHER\n"
        "def build(device=None, sdk=None): return VALUE, OTHER\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    hashes = tuple(
        (name, hashlib.sha256(path.read_bytes()).hexdigest())
        for name, path in (
            ("closed_factory", package / "__init__.py"),
            ("closed_factory.helper", package / "helper.py"),
            ("closed_factory.model", package / "model.py"),
        )
    )
    value = factory(tmp_path, "closed_factory.model", hashes).reviewed_factory()()
    assert value == ("frozen-parent", "frozen-helper")


def test_missing_parent_manifest_is_refused_before_package_exec(
    tmp_path, monkeypatch, frozen_paths
):
    package = tmp_path / "closed_factory"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('unfrozen-parent')\n")
    (package / "model.py").write_text("def build(device=None, sdk=None): return 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError, match="parent package"):
        factory(tmp_path, "closed_factory.model").reviewed_factory()


def test_unlisted_import_is_rejected_even_if_dependency_is_preloaded(
    tmp_path, monkeypatch, frozen_paths
):
    (tmp_path / "closed_factory.py").write_text(
        "import unreviewed_dependency\ndef build(device=None, sdk=None): return 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(
        sys.modules, "unreviewed_dependency", ModuleType("unreviewed_dependency")
    )
    with pytest.raises(ImportError, match="outside the frozen closure"):
        factory(tmp_path).reviewed_factory()


def test_standard_library_name_cannot_select_shadow_from_pythonpath(
    tmp_path, monkeypatch, frozen_paths
):
    (tmp_path / "closed_factory.py").write_text(
        "import colorsys\ndef build(device=None, sdk=None): return 1\n"
    )
    (tmp_path / "colorsys.py").write_text("raise RuntimeError('shadow-interpreter')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    with pytest.raises(ImportError, match="outside the interpreter"):
        factory(tmp_path).reviewed_factory()


def test_source_replacement_after_manifest_read_does_not_change_executed_bytes(
    tmp_path, monkeypatch, frozen_paths
):
    from levi.live.owned_source import FrozenSourceBundle

    source = tmp_path / "closed_factory.py"
    source.write_text("def build(device=None, sdk=None): return 'frozen'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    bundle = FrozenSourceBundle(
        tmp_path, (("closed_factory", hashlib.sha256(source.read_bytes()).hexdigest()),)
    )
    source.write_text("raise RuntimeError('replacement-source')\n")
    bundle.install()
    assert bundle.import_module("closed_factory").build() == "frozen"


def test_factory_without_explicit_frozen_source_root_stays_closed():
    with pytest.raises(ValueError, match="source root"):
        replace(
            FrozenTorchFactory("closed_factory", "build", "ab" * 32, "unused", "cuda:0")
        ).reviewed_factory()
