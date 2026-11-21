"""
A feature's door (core/<機能>/api.py, docs/architecture.md R2) read when asked: the names of its `__all__` that
its `if TYPE_CHECKING:` block imports are each imported the first time someone takes them, not all of them when the
door is imported (the plugin's start reads only what it uses).

    if TYPE_CHECKING:  # the door: read by type checkers, the architecture test and lazy()
        from .model import ImagingRequest
        from .store import load_requests

    __getattr__, __dir__ = lazy(__name__)

    __all__ = ["ImagingRequest", "load_requests"]

`from mikage.core.request.api import load_requests` runs `from .store import load_requests` in the door then, as Python
runs that statement, so the same object as an import at the top would have given (a submodule, an attribute of the
package, a relative or an absolute module alike). A name of the block that is not in `__all__` stays for type
checkers only, as Python has it: taking it at run time is an AttributeError. An error while the name's module is
imported comes out as an ImportError with that error as its cause, not as "no attribute" (which `hasattr` and
`from … import` would hide). tests/test_doors.py checks every door against the import statements themselves.
"""
from __future__ import annotations

import ast
import sys
from typing import Callable


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def type_checking_imports(source: str) -> list[ast.ImportFrom]:
    """The `from … import …` statements of a module's top-level `if TYPE_CHECKING:` blocks (not their `else:`)."""
    return [imp for node in ast.parse(source).body if isinstance(node, ast.If) and _is_type_checking(node.test)
            for imp in node.body if isinstance(imp, ast.ImportFrom)]


def door_names(source: str) -> dict[str, tuple[str, str]]:
    """{name the block binds: (the module, relative as written, the name in it)} of a door's source (`__all__` aside)."""
    out: dict[str, tuple[str, str]] = {}
    for imp in type_checking_imports(source):
        for alias in imp.names:
            out[alias.asname or alias.name] = ("." * imp.level + (imp.module or ""), alias.name)
    return out


def lazy(module_name: str) -> tuple[Callable[[str], object], Callable[[], list]]:
    """The door module's (`__getattr__`, `__dir__`)."""
    module = sys.modules[module_name]
    table: dict[str, tuple[str, int, str]] = {}

    def names() -> dict[str, tuple[str, int, str]]:
        if not table:
            with open(module.__file__, encoding="utf-8") as f:
                imports = type_checking_imports(f.read())
            public = set(getattr(module, "__all__", ()))
            table.update({alias.asname or alias.name: (imp.module or "", imp.level, alias.name)
                          for imp in imports for alias in imp.names
                          if (alias.asname or alias.name) in public})  # the door's; the others are the type checkers'
        return table

    def __getattr__(name: str) -> object:  # noqa: N807 (the module's)
        found = names().get(name) if not name.startswith("__") else None
        if found is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        value = _import_from(module, *found)
        setattr(module, name, value)  # asked once
        return value

    def __dir__() -> list:  # noqa: N807
        return sorted(set(vars(module)) | set(names()))

    return __getattr__, __dir__


def _import_from(module, where: str, level: int, attr: str) -> object:
    """
    `from <where> import <attr>` (with `level` dots) as the door's own statement runs it: Python's __import__ with the
    door's globals, then the name, or the submodule the import made (as the interpreter's IMPORT_FROM).
    """
    try:
        base = __import__(where, vars(module), None, (attr,), level)
    except ImportError:
        raise
    except Exception as error:  # e.g. an AttributeError in the module: not "no attribute" of the door
        raise ImportError(f"{module.__name__}: importing {'.' * level + where!r} for {attr!r} failed: {error!r}",
                          name=module.__name__) from error
    try:
        return getattr(base, attr)
    except AttributeError:
        submodule = sys.modules.get(f"{base.__name__}.{attr}")
        if submodule is not None:
            return submodule
        raise ImportError(f"cannot import name {attr!r} from {base.__name__!r}", name=base.__name__) from None
