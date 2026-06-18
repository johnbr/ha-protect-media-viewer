"""Pytest fixtures for offline unit tests.

The integration imports ``homeassistant.*``, ``aiohttp`` and ``uiprotect`` at
module load time, but the helpers exercised here are pure (HMAC token signing,
JPEG sniffing, LRU pruning, the card content fingerprint). To avoid pulling in
the full Home Assistant / uiprotect test stack, this conftest installs a
meta-path finder that fabricates a permissive stub module for *any* import
under those top-level namespaces, *before* the integration is imported.

Each stub module is treated as a package (so submodule imports resolve) and its
``__getattr__`` returns a do-nothing ``_Stub`` for any symbol — enough to import
a module and reach its pure helpers via ``from x import Y`` / ``import x.y``.
Anything that actually *uses* the stubbed HA/uiprotect runtime is out of scope
for these unit tests.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

# Top-level packages we never want to really import during unit tests.
_STUB_ROOTS = ("homeassistant", "aiohttp", "uiprotect")


class _StubMeta(type):
    """Resolve arbitrary *class-level* attribute access to a stub.

    Lets module-level code like ``EventType.SMART_DETECT`` import cleanly,
    where ``EventType`` is the stub standing in for a uiprotect enum.
    """

    def __getattr__(cls, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _Stub


class _Stub(metaclass=_StubMeta):
    """A class that is harmless to subclass, instantiate, subscript or attr-access."""

    def __init__(self, *args, **kwargs):
        pass

    def __init_subclass__(cls, **kwargs):
        return None

    def __class_getitem__(cls, item):
        return cls

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _Stub


def _make_stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    # Mark as a package so `import a.b.c` traverses without a real loader.
    mod.__path__ = []  # type: ignore[attr-defined]

    def _getattr(attr: str) -> object:
        # Let dunders (e.g. __path__, __all__) fall through to normal lookup;
        # only fabricate ordinary symbols imported by the integration.
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _Stub

    mod.__getattr__ = _getattr  # type: ignore[attr-defined]
    return mod


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return _make_stub_module(spec.name)

    def exec_module(self, module):  # already fully populated by create_module
        return None


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in _STUB_ROOTS:
            return importlib.machinery.ModuleSpec(fullname, _StubLoader(), is_package=True)
        return None


if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _StubFinder())


# Make `custom_components` importable from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
