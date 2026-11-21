"""
The features' doors (core/<機能>/api.py, core/door.py): each name a door gives is there when taken, imported from its
module only then, so the plugin's start reads only what it uses; and it is the object its import statement gives.
"""
import ast
import importlib
import subprocess
import symtable
import sys
import textwrap

import pytest

from mikage.core.door import door_names, type_checking_imports

DOORS = ["ai", "catalog", "enhance", "obs", "plan", "request"]


def _source(door) -> str:
    with open(door.__file__, encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("feature", DOORS)
def test_every_name_of_the_door_comes(feature):
    door = importlib.import_module(f"mikage.core.{feature}.api")
    names = door_names(_source(door))
    assert names, "the door lists its names in `if TYPE_CHECKING:`"
    for name in door.__all__:
        assert getattr(door, name) is not None, name
    assert set(door.__all__) <= set(dir(door))
    with pytest.raises(AttributeError):
        door.no_such_name  # noqa: B018


@pytest.mark.parametrize("feature", DOORS)
def test_a_name_of_the_door_is_what_its_import_statement_gives(feature):
    """The block's import statements run as they are (Python's own lookup): the door gives the same objects."""
    door = importlib.import_module(f"mikage.core.{feature}.api")
    scope = {"__name__": door.__name__, "__package__": door.__package__, "__spec__": door.__spec__}
    block = ast.Module(body=type_checking_imports(_source(door)), type_ignores=[])
    exec(compile(block, door.__file__, "exec"), scope)
    for name in set(door.__all__) & set(scope):
        assert getattr(door, name) is scope[name], name


@pytest.mark.parametrize("feature", DOORS)
def test_the_door_does_not_use_its_names_itself(feature):
    """
    A door's own code does not take a name of its block at run time: the block is not run, and a module's
    __getattr__ is not asked for its own globals (a NameError at run time, which type checkers do not see).
    Annotations are not run (`from __future__ import annotations`).
    """
    door = importlib.import_module(f"mikage.core.{feature}.api")
    source = _source(door)
    assert "from __future__ import annotations" in source
    lazy = set(door_names(source)) - _bound_outside_the_block(source)
    used = []

    def walk(table, top):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if name in lazy and symbol.is_referenced() and (top or symbol.is_global()):
                used.append(f"{table.get_name()}: {name}")
        for child in table.get_children():
            walk(child, False)

    walk(symtable.symtable(source, door.__file__, "exec"), True)
    assert not used, used


def _bound_outside_the_block(source: str) -> set[str]:
    out = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                out |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return out


def test_a_name_imports_only_its_module():
    """撮像依頼's door taken for one name does not read the others (follow_job's urllib, simulate…)."""
    code = ("import sys\n"
            "from mikage.core.request.api import ImagingRequest\n"
            "print(sorted(m for m in sys.modules if m.startswith('mikage.core.request.')))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "mikage.core.request.model" in out
    assert "mikage.core.request.follow_job" not in out and "mikage.core.request.simulate" not in out


# A door of a small package of its own: each form of `from … import …` as Python resolves it.
_FILES = {
    "__init__.py": "VALUE = 'an attribute of the package'\n",
    "a.py": "X = 1\nY = 2\n",
    "sub/__init__.py": "",
    "sub/leaf.py": "LEAF = 3\n",
    "sub/door.py": """
        from __future__ import annotations
        from typing import TYPE_CHECKING
        from mikage.core.door import lazy

        if TYPE_CHECKING:
            from .. import VALUE                 # an attribute of the package above
            from .. import a                     # a module of the package above
            from . import leaf                   # a submodule not imported yet
            from .leaf import LEAF as RENAMED
            from ..a import X
            from collections import OrderedDict  # absolute
            from ..a import Y                    # for type checkers only: not in __all__
            from ..broken import Z               # its module fails while imported
            from ..nowhere import W              # no such module

        __getattr__, __dir__ = lazy(__name__)

        __all__ = ["VALUE", "a", "leaf", "RENAMED", "X", "OrderedDict", "Z", "W"]
    """,
    "broken.py": "import os\nZ = os.no_such_function()\n",
}


@pytest.fixture
def door(tmp_path, monkeypatch):
    root = tmp_path / "doorpkg"
    for path, text in _FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(text), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield importlib.import_module("doorpkg.sub.door")
    for name in [m for m in sys.modules if m == "doorpkg" or m.startswith("doorpkg.")]:
        del sys.modules[name]


def test_each_form_of_import_gives_what_python_gives(door):
    import collections

    import doorpkg
    import doorpkg.a
    assert "doorpkg.sub.leaf" not in sys.modules
    assert door.VALUE is doorpkg.VALUE
    assert door.a is doorpkg.a
    assert door.leaf is importlib.import_module("doorpkg.sub.leaf")
    assert door.RENAMED == 3 and door.X == 1
    assert door.OrderedDict is collections.OrderedDict
    assert "X" in vars(door)  # taken once


def test_a_name_for_type_checkers_only_is_not_the_doors(door):
    with pytest.raises(AttributeError):
        door.Y  # noqa: B018
    assert "Y" not in dir(door) and "X" in dir(door)


def test_an_error_while_importing_a_name_is_not_hidden_as_no_attribute(door):
    with pytest.raises(ImportError) as raised:
        door.Z  # noqa: B018
    assert isinstance(raised.value.__cause__, AttributeError)
    with pytest.raises(ImportError):
        hasattr(door, "Z")  # not a quiet False
    with pytest.raises(ModuleNotFoundError):
        door.W  # noqa: B018
