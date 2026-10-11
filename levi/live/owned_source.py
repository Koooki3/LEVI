"""Frozen local source execution; no cached bytecode or unverified imports.

This loader binds reviewed bytes to actual executed Python code. It is not a
sandbox: the complete manifest/code and native SDK remain independent review
boundaries. Nothing installs a hook or imports a model at module import time.
"""

import builtins
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import sys
import sysconfig
import types
from pathlib import Path

_STDLIB_ROOT = Path(sysconfig.get_path("stdlib")).resolve()
_SITE_ROOTS = tuple(
    Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")
)


def _stdlib_spec(name, path=None):
    if name.split(".", 1)[0] not in sys.stdlib_module_names:
        raise ImportError("dependency is outside the frozen source closure")
    spec = importlib.machinery.BuiltinImporter.find_spec(name)
    if spec is None:
        spec = importlib.machinery.FrozenImporter.find_spec(name)
    if spec is None:
        if path is None and "." in name:
            parent = sys.modules.get(name.rpartition(".")[0])
            path = getattr(parent, "__path__", None)
        spec = importlib.machinery.PathFinder.find_spec(name, path)
    if spec is None:
        raise ImportError("trusted interpreter module could not be resolved")
    if spec.loader in (
        importlib.machinery.BuiltinImporter,
        importlib.machinery.FrozenImporter,
    ):
        expected = (
            "built-in"
            if spec.loader is importlib.machinery.BuiltinImporter
            else "frozen"
        )
        if spec.origin != expected:
            raise ImportError("interpreter module origin changed")
    elif type(spec.loader) in (
        importlib.machinery.SourceFileLoader,
        importlib.machinery.ExtensionFileLoader,
    ):
        origin = Path(spec.origin).resolve(strict=True)
        if not origin.is_relative_to(_STDLIB_ROOT) or any(
            origin.is_relative_to(site) for site in _SITE_ROOTS
        ):
            raise ImportError("standard library name resolved outside the interpreter")
    else:
        raise ImportError("untrusted standard library loader")
    existing = sys.modules.get(name)
    if existing is not None:
        actual = getattr(existing, "__spec__", None)
        if (
            actual is None
            or actual.origin != spec.origin
            or (
                actual.loader is not spec.loader
                if spec.loader
                in (
                    importlib.machinery.BuiltinImporter,
                    importlib.machinery.FrozenImporter,
                )
                else type(actual.loader) is not type(spec.loader)
            )
        ):
            raise ImportError("preloaded standard library origin/loader differs")
    return spec


class _SourceLoader(importlib.abc.Loader):
    def __init__(self, bundle, name, origin, source, package):
        self.bundle, self.name = bundle, name
        self.origin, self.source, self.package = origin, source, package

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if module.__spec__.loader is not self or module.__spec__.origin != self.origin:
            raise ValueError("frozen source loader/spec changed")
        module.__file__ = self.origin
        module.__cached__ = None
        namespace = dict(vars(builtins))
        namespace["__import__"] = self.bundle.checked_import
        module.__dict__["__builtins__"] = namespace
        # Execute exactly the verified immutable bytes, never a .pyc or a
        # second read of a path that could change after verification.
        code = compile(self.source, self.origin, "exec", dont_inherit=True)
        self.codes = []

        def remember(value):
            self.codes.append(value)
            for child in value.co_consts:
                if isinstance(child, types.CodeType):
                    remember(child)

        remember(code)
        exec(code, module.__dict__)  # noqa: S102 - verified immutable source bytes


class FrozenSourceBundle(importlib.abc.MetaPathFinder):
    """An explicit root and complete Python module manifest.

    Expected module paths are derived under that exact root. Only ordinary
    source files/packages are supported. SDKs/native code must be separately
    reviewed and bound; allowing an SDK import requires its own installed
    bundle, not a version string or caller-supplied type.
    """

    def __init__(self, root, hashes, *, allowed_bundles=()):
        self.root = Path(root).resolve(strict=True)
        self.hashes = dict(hashes)
        if len(self.hashes) != len(hashes) or not self.hashes:
            raise ValueError("complete unique frozen source manifest required")
        self.allowed_bundles = tuple(allowed_bundles)
        self.sources = {}
        self._facades = {}
        self._installed = False
        for name, expected in hashes:
            pieces = name.split(".")
            if not all(piece.isidentifier() for piece in pieces):
                raise ValueError("invalid frozen module name")
            for end in range(1, len(pieces)):
                if ".".join(pieces[:end]) not in self.hashes:
                    raise ValueError("every factory/SDK parent package must be frozen")
            relative = self.root.joinpath(*pieces)
            package = relative / "__init__.py"
            origin = (
                package if package.is_file() else relative.with_suffix(".py")
            ).resolve(strict=True)
            if not origin.is_relative_to(self.root):
                raise ValueError("frozen source escaped its declared root")
            source = origin.read_bytes()
            if hashlib.sha256(source).hexdigest() != expected:
                raise ValueError("implementation differs from frozen source")
            self.sources[name] = (str(origin), source, origin.name == "__init__.py")

    def _allowed(self, name):
        return (
            name in self.sources
            or name.split(".", 1)[0] in sys.stdlib_module_names
            or any(name in bundle.sources for bundle in self.allowed_bundles)
        )

    def _validate_name(self, name):
        if not self._allowed(name):
            raise ImportError("factory dependency is outside the frozen closure")
        for end in range(1, len(name.split(".")) + 1):
            parent = ".".join(name.split(".")[:end])
            if parent.split(".", 1)[0] in sys.stdlib_module_names:
                _stdlib_spec(parent)
            elif parent in sys.modules:
                self._validate_module(sys.modules[parent])

    def _validate_module(self, module):
        name = getattr(module, "__name__", "")
        if not self._allowed(name):
            raise ImportError("imported module is outside the frozen closure")
        if module is not sys.modules.get(name) and module is not self._facades.get(
            name
        ):
            raise ImportError("import returned a noncanonical cached module")
        if name.split(".", 1)[0] in sys.stdlib_module_names:
            _stdlib_spec(name)
        else:
            bundle = next(
                bundle
                for bundle in (self, *self.allowed_bundles)
                if name in bundle.sources
            )
            if bundle.require_module(name) is not module:
                raise ImportError("imported module is outside its frozen loader")

    def _validate_value(self, value):
        if isinstance(value, types.ModuleType):
            self._validate_module(value)
        elif isinstance(value, types.FunctionType):
            name = value.__module__
            self._validate_name(name)
            if name.split(".", 1)[0] not in sys.stdlib_module_names:
                bundle = next(
                    bundle
                    for bundle in (self, *self.allowed_bundles)
                    if name in bundle.sources
                )
                bundle.require_function(value)

    def resolve_import(self, name, package=None):
        absolute = (
            importlib.util.resolve_name(name, package) if name.startswith(".") else name
        )
        if absolute.startswith("importlib."):
            raise ImportError("low-level dynamic import helpers are not supported")
        self._validate_name(absolute)  # include cached sys.modules hits
        module = importlib.import_module(absolute)
        self._validate_module(module)
        return self._facade(module)

    def _facade(self, module):
        name = module.__name__
        if name not in ("importlib", "builtins"):
            return module
        if name not in self._facades:
            proxy = types.ModuleType(name)
            for attr in (
                "__spec__",
                "__loader__",
                "__file__",
                "__package__",
                "__path__",
            ):
                if hasattr(module, attr):
                    setattr(proxy, attr, getattr(module, attr))
            if name == "importlib":
                proxy.import_module = self.resolve_import
                # Low-level loaders/reload could bypass cached dependency
                # validation. They are explicitly unsupported in this contract.
            else:
                proxy.__dict__.update(vars(module))
                proxy.__import__ = self.checked_import
            self._facades[name] = proxy
        return self._facades[name]

    def checked_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        absolute = (
            importlib.util.resolve_name(
                "." * level + name, (globals or {}).get("__package__", "")
            )
            if level
            else name
        )
        if absolute.startswith("importlib."):
            raise ImportError("low-level dynamic import helpers are not supported")
        self._validate_name(absolute)
        result = builtins.__import__(name, globals, locals, fromlist, level)
        self._validate_module(result)
        if fromlist:
            names = fromlist
            if "*" in names:
                names = getattr(
                    result,
                    "__all__",
                    tuple(n for n in vars(result) if not n.startswith("_")),
                )
            for attr in names:
                if hasattr(result, attr):
                    self._validate_value(getattr(result, attr))
        return self._facade(result)

    def install(self):
        if self._installed:
            raise ValueError("frozen source bundle cannot be installed twice")
        roots = {name.split(".", 1)[0] for name in self.sources}
        if any(name.split(".", 1)[0] in roots for name in sys.modules):
            raise ValueError(
                "preloaded factory/SDK package is not an owned source import"
            )
        # Check the ordinary resolver without executing package code. Namespace
        # packages, shadow paths and arbitrary path-hook loaders are refused.
        search_paths = {}
        for name in sorted(self.sources, key=lambda item: (item.count("."), item)):
            parent = name.rpartition(".")[0]
            search = search_paths.get(parent, sys.path if not parent else None)
            spec = importlib.machinery.PathFinder.find_spec(name, search)
            origin, _, package = self.sources[name]
            if (
                spec is None
                or type(spec.loader) is not importlib.machinery.SourceFileLoader
                or spec.origin is None
                or Path(spec.origin).resolve() != Path(origin)
            ):
                raise ValueError(
                    "actual import origin/loader differs from frozen source"
                )
            if package:
                if tuple(spec.submodule_search_locations or ()) != (
                    str(Path(origin).parent),
                ):
                    raise ValueError(
                        "actual package search path differs from frozen root"
                    )
                search_paths[name] = [str(Path(origin).parent)]
        sys.meta_path.insert(0, self)
        self._installed = True
        return self

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.sources:
            origin, source, package = self.sources[fullname]
            loader = _SourceLoader(self, fullname, origin, source, package)
            spec = importlib.util.spec_from_loader(
                fullname, loader, origin=origin, is_package=package
            )
            if package:
                spec.submodule_search_locations = [str(Path(origin).parent)]
            return spec
        if not self._allowed(fullname):
            raise ImportError("dependency is outside the frozen source closure")
        if fullname.split(".", 1)[0] in sys.stdlib_module_names:
            return _stdlib_spec(fullname, path)
        return None

    def require_module(self, name):
        module = sys.modules.get(name)
        loader = getattr(getattr(module, "__spec__", None), "loader", None)
        if (
            name not in self.sources
            or type(loader) is not _SourceLoader
            or loader.bundle is not self
            or getattr(module, "__file__", None) != self.sources[name][0]
            or module.__spec__.origin != self.sources[name][0]
        ):
            raise ValueError("module is not executing this frozen source bundle")
        return module

    def import_module(self, name):
        importlib.import_module(name)
        return self.require_module(name)

    def require_function(self, function):
        function = getattr(function, "__func__", function)
        module = self.require_module(getattr(function, "__module__", ""))
        loader = module.__spec__.loader
        if not any(
            getattr(function, "__code__", None) is code for code in loader.codes
        ):
            raise ValueError(
                "SDK function is not compiled from the frozen source bytes"
            )

    def require_loaded(self):
        roots = {name.split(".", 1)[0] for name in self.sources}
        for name in tuple(sys.modules):
            if name.split(".", 1)[0] in roots:
                self.require_module(name)

    def close(self):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        self._installed = False


def frozen_distribution_bundle(
    distribution_name, package, version, hashes, *, allowed_bundles=()
):
    """Bind the actual resolver to every Python source in one installed package.

    Native/third-party modules outside these explicitly composed bundles are
    refused. They need their own reviewed binding before real SDK deployment.
    """
    if not hashes:
        raise ValueError("complete installed SDK Python source manifest required")
    distribution = importlib.metadata.distribution(distribution_name)
    if distribution.version != version:
        raise ValueError("installed SDK differs from frozen version")
    root = Path(distribution.locate_file(package)).resolve(strict=True)
    expected = set()
    for path in root.rglob("*.py"):
        if not path.resolve().is_relative_to(root):
            raise ValueError("installed SDK source escapes its package root")
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        expected.add(".".join((package, *parts)))
    if {name for name, _ in hashes} != expected:
        raise ValueError("SDK manifest does not freeze every installed Python module")
    return FrozenSourceBundle(root.parent, hashes, allowed_bundles=allowed_bundles)
