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
        self.bundle._owned_modules[self.name] = module
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
        self._owned_modules = {}
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
        if not self._installed:
            raise ImportError("owned source lifetime is closed")
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
        if not self._installed:
            raise ImportError("owned source lifetime is closed")
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
        self._validate_name(name)
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
        if not self._installed:
            raise ImportError("owned source lifetime is closed")
        roots = {name.split(".", 1)[0] for name in self.sources}
        for name in tuple(sys.modules):
            if name.split(".", 1)[0] in roots:
                self.require_module(name)

    def close(self):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        self._installed = False
        for name, module in sorted(
            self._owned_modules.items(),
            key=lambda item: item[0].count("."),
            reverse=True,
        ):
            # Do not delete an external replacement or a pre-existing module.
            if sys.modules.get(name) is module:
                del sys.modules[name]
            parent_name, _, child_name = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and vars(parent).get(child_name) is module:
                del vars(parent)[child_name]
        self._owned_modules.clear()
        self._facades.clear()


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


class FrozenCriticalBindings:
    """Snapshot exact critical SDK objects before any factory executes.

    Python function code, its global aliases/defaults/closure cells and the
    loaded SDK module/class bindings remain fixed. Mutable referenced
    containers are checked too. Unsupported dynamic critical state must fail
    closed; this is not proof of an unimplemented remote/native call chain.
    """

    def __init__(self, bundles):
        self.bundles = tuple(bundles)
        self._checks = []
        self._seen = set()
        self._dependency_seen = set()
        self._class_bindings = {}
        for bundle in self.bundles:
            bundle.require_loaded()
            for name in bundle.sources:
                if name in sys.modules:
                    self._capture(sys.modules[name])

    def _owned(self, name):
        return next((bundle for bundle in self.bundles if name in bundle.sources), None)

    def _capture(self, value):
        if id(value) in self._seen:
            return
        self._seen.add(id(value))
        if isinstance(value, types.ModuleType):
            if self._owned(value.__name__) is None:
                return
            # The sealed object must stay the importable module: a removed and
            # re-executed copy would carry fresh, unsealed bindings.
            self._checks.append(("module", value.__name__, None, value))
            self._mapping(value.__dict__)
        elif isinstance(value, type):
            if self._owned(value.__module__) is None:
                return
            self._class_bindings[value] = dict(vars(value))
            for name, child in vars(value).items():
                self._checks.append(("class", value, name, child))
                self._capture(child)
        elif isinstance(value, (staticmethod, classmethod)):
            self._capture(value.__func__)
        elif isinstance(value, types.FunctionType):
            bundle = self._owned(value.__module__)
            if bundle is None:
                return
            bundle.require_function(value)
            for name in ("__code__", "__defaults__", "__kwdefaults__", "__closure__"):
                child = getattr(value, name)
                self._checks.append(("attribute", value, name, child))
                self._capture(child)
                if name != "__code__":
                    self._capture_dependency(child)
            for name in value.__code__.co_names:
                child = value.__globals__.get(name, _ABSENT)
                self._checks.append(("mapping", value.__globals__, name, child))
                self._capture(child)
                self._capture_dependency(child)
        elif isinstance(value, types.CellType):
            try:
                child = value.cell_contents
            except ValueError:
                child = _ABSENT
            self._checks.append(("cell", value, None, child))
            self._capture(child)
        elif isinstance(value, dict):
            self._mapping(value)
        elif isinstance(value, (list, tuple)):
            self._checks.append(("sequence", value, None, tuple(value)))
            for child in value:
                self._capture(child)
        elif isinstance(value, set):
            self._checks.append(("set", value, None, frozenset(value)))

    def _capture_dependency(self, value):
        if id(value) in self._dependency_seen or value is _ABSENT:
            return
        self._dependency_seen.add(id(value))
        if type(value) is types.SimpleNamespace:
            expected = self._callable_container(value, capture=True)
            self._checks.append(("callable_container", value, None, expected))
        elif type(value) in (dict, list, tuple, set, frozenset):
            children = value.values() if type(value) is dict else value
            for child in children:
                self._capture_dependency(child)
        elif isinstance(value, types.CellType):
            try:
                child = value.cell_contents
            except ValueError:
                return
            self._capture_dependency(child)
        elif not (
            value is None
            or type(value) in (bool, int, float, complex, str, bytes)
            or isinstance(value, (types.ModuleType, type, types.FunctionType))
        ):
            raise ValueError("unsupported critical callable dependency")

    def _callable_container(self, value, *, capture=False, seen=None):
        # Inspect only exact, passive object/container types. Numeric state is
        # deliberately absent from the signature; callable paths remain fixed.
        seen = set() if seen is None else seen
        if value is None or type(value) in (bool, int, float, complex, str, bytes):
            return None
        if isinstance(value, types.FunctionType):
            if self._owned(value.__module__) is None:
                raise ValueError("unbound critical callable dependency")
            if capture:
                self._capture(value)
            return ("function", id(value))
        if isinstance(value, type):
            if capture:
                self._capture(value)
            return ("class", id(value))
        if id(value) in seen:
            return ("cycle", id(value))
        seen.add(id(value))
        if type(value) is types.SimpleNamespace:
            children = vars(value).items()
        elif type(value) is dict:
            if any(type(key) not in (str, int, bool, bytes) for key in value):
                raise ValueError("unsupported critical callable container key")
            children = value.items()
        elif type(value) in (list, tuple):
            children = enumerate(value)
        else:
            raise ValueError("unsupported critical callable dependency")
        signature = []
        for key, child in children:
            shape = self._callable_container(child, capture=capture, seen=seen)
            if shape is not None:
                signature.append((key, shape))
        return (type(value), id(value), tuple(signature))

    def require_methods(self, instance, names, *, expected_type):
        import inspect

        if type(instance) is not expected_type:
            raise ValueError("critical SDK instance type changed")
        if (
            inspect.getattr_static(expected_type, "__getattribute__")
            is not object.__getattribute__
        ):
            raise ValueError("dynamic critical SDK method resolution unsupported")
        for name in names:
            descriptor = next(
                (
                    self._class_bindings[owner][name]
                    for owner in expected_type.__mro__
                    if owner in self._class_bindings
                    and name in self._class_bindings[owner]
                ),
                _ABSENT,
            )
            if inspect.getattr_static(instance, name, _ABSENT) is not descriptor:
                raise ValueError("critical SDK instance method binding changed")
            actual = getattr(instance, name)
            if isinstance(descriptor, types.FunctionType):
                matches = (
                    isinstance(actual, types.MethodType)
                    and actual.__func__ is descriptor
                    and actual.__self__ is instance
                )
            elif isinstance(descriptor, classmethod):
                matches = (
                    isinstance(actual, types.MethodType)
                    and actual.__func__ is descriptor.__func__
                    and actual.__self__ is expected_type
                )
            elif isinstance(descriptor, staticmethod):
                matches = actual is descriptor.__func__
            else:
                raise TypeError("unsupported critical SDK method descriptor")
            if not matches:
                raise ValueError("critical SDK resolved callable binding changed")
            self._owned(expected_type.__module__).require_function(actual)

    def _mapping(self, mapping):
        # Namespace mutation cannot introduce a new global helper after sealing.
        self._checks.append(("keys", mapping, None, tuple(mapping)))
        for key, value in tuple(mapping.items()):
            self._checks.append(("mapping", mapping, key, value))
            self._capture(value)

    def require(self):
        for bundle in self.bundles:
            bundle.require_loaded()
        for kind, owner, key, expected in self._checks:
            if kind == "module":
                matches = sys.modules.get(owner) is expected
            elif kind == "callable_container":
                matches = self._callable_container(owner) == expected
            elif kind == "class":
                matches = vars(owner).get(key, _ABSENT) is expected
            elif kind == "attribute":
                matches = getattr(owner, key, _ABSENT) is expected
            elif kind == "mapping":
                matches = owner.get(key, _ABSENT) is expected
            elif kind == "keys":
                matches = tuple(owner) == expected
            elif kind == "sequence":
                matches = len(owner) == len(expected) and all(
                    current is original
                    for current, original in zip(owner, expected, strict=True)
                )
            elif kind == "set":
                matches = frozenset(owner) == expected
            else:
                try:
                    matches = owner.cell_contents is expected
                except ValueError:
                    matches = expected is _ABSENT
            if not matches:
                raise ValueError("critical SDK binding changed after frozen import")


_ABSENT = object()


class OwnedSourceLifetime:
    """Only bundles installed by this load; close in reverse installation order."""

    def __init__(self, bundles):
        self.bundles = tuple(bundles)
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for bundle in reversed(self.bundles):
            bundle.close()


class SourceLoadTransaction:
    """Own every SDK/factory hook and module through one explicit transaction."""

    def __init__(self):
        self.bundles = []
        self._committed = False

    def __enter__(self):
        return self

    def install(self, bundle):
        # Include a partial install in the rollback set before doing any work.
        self.bundles.append(bundle)
        return bundle.install()

    def commit(self):
        if self._committed:
            raise ValueError("source load transaction already committed")
        lifetime = OwnedSourceLifetime(self.bundles)
        self._committed = True
        return lifetime

    def close(self):
        OwnedSourceLifetime(self.bundles).close()

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None or not self._committed:
            self.close()
