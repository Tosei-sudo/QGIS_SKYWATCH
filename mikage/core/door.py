"""
A feature's door (core/<機能>/api.py, docs/architecture.md R2) read when asked: the names its
`if TYPE_CHECKING:` block imports are each imported from their module the first time someone
takes them, not all of them when the door is imported (the plugin's start reads only what it uses).

    if TYPE_CHECKING:  # the door: read by type checkers, the architecture test and lazy()
        from .model import ImagingRequest
        from .store import load_requests

    __getattr__, __dir__ = lazy(__name__)

`from mikage.core.request.api import load_requests` imports core/request/store.py then; the door
is the same list of names as before (tests/test_architecture.py checks each one comes).
"""
from __future__ import annotations

import ast
import importlib
import sys
from typing import Callable


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def door_names(source: str) -> dict[str, tuple[str, str]]:
    """{name the door gives: (the module, relative as written, the name in it)} of a door's source."""
    out: dict[str, tuple[str, str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for imp in node.body:
                if isinstance(imp, ast.ImportFrom):
                    where = "." * imp.level + (imp.module or "")
                    for alias in imp.names:
                        out[alias.asname or alias.name] = (where, alias.name)
    return out


def lazy(module_name: str) -> tuple[Callable[[str], object], Callable[[], list]]:
    """The door module's (`__getattr__`, `__dir__`)."""
    module = sys.modules[module_name]
    table: dict[str, tuple[str, str]] = {}

    def names() -> dict[str, tuple[str, str]]:
        if not table:
            with open(module.__file__, encoding="utf-8") as f:
                table.update(door_names(f.read()))
        return table

    def __getattr__(name: str) -> object:  # noqa: N807 (the module's)
        found = names().get(name) if not name.startswith("__") else None
        if found is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        where, attr = found
        if where.strip("."):
            value = getattr(importlib.import_module(where, module.__package__), attr)
        else:  # `from . import place_cache`: a module of the package
            value = importlib.import_module(where + attr, module.__package__)
        setattr(module, name, value)  # asked once
        return value

    def __dir__() -> list:  # noqa: N807
        return sorted(set(vars(module)) | set(names()))

    return __getattr__, __dir__
