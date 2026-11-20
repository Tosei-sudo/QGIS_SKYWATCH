"""
The big docks' parts (docs/architecture.md 大きなドックの部分): each dock is made of every part of its
folder (ui/obs_dock, ui/plan_dock, ui/catalog_parts), and each method is in one place only (a second
one would silently win or lose by the order of the parts).
"""
import ast
import os

import pytest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")

#: (the dock's module, its class, the folder of its parts)
DOCKS = [
    ("observation_dock.py", "ObservationDock", "obs_dock"),
    ("imaging_plan_dock.py", "ImagingPlanDock", "plan_dock"),
    ("catalog_dock.py", "CatalogDock", "catalog_parts"),
]


def _classes(module, dock, folder):
    paths = [os.path.join(UI, module)]
    folder = os.path.join(UI, folder)
    paths += [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith(".py")]
    for path in paths:
        for node in ast.parse(open(path, encoding="utf-8").read()).body:
            if isinstance(node, ast.ClassDef) and (node.name == dock or node.name.endswith("Part")):
                yield os.path.relpath(path, UI), node


def _members(node):
    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name
        elif isinstance(n, ast.Assign):
            yield from (t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            yield n.target.id


@pytest.mark.parametrize("module, dock, folder", DOCKS)
def test_the_dock_is_made_of_every_part(module, dock, folder):
    classes = dict((node.name, node) for _path, node in _classes(module, dock, folder))
    bases = [ast.unparse(b) for b in classes[dock].bases]
    parts = sorted(n for n in classes if n.endswith("Part"))
    assert sorted(b for b in bases if b.endswith("Part")) == parts and bases[-1] == "PluginDock"


@pytest.mark.parametrize("module, dock, folder", DOCKS)
def test_each_method_is_in_one_place(module, dock, folder):
    seen = {}
    for path, node in _classes(module, dock, folder):
        for name in _members(node):
            assert name not in seen, f"{name}: {seen[name]} と {path} の両方にある"
            seen[name] = path
