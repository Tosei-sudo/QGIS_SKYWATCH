"""
The observation dock's parts (ui/obs_dock, docs/architecture.md 段階7): ObservationDock is made of
every part, and each method is in one place only (a second one would silently win or lose by the
order of the parts).
"""
import ast
import os

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _classes():
    paths = [os.path.join(UI, "observation_dock.py")]
    folder = os.path.join(UI, "obs_dock")
    paths += [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith(".py")]
    for path in paths:
        for node in ast.parse(open(path, encoding="utf-8").read()).body:
            if isinstance(node, ast.ClassDef) and (node.name == "ObservationDock" or node.name.endswith("Part")):
                yield os.path.relpath(path, UI), node


def _members(node):
    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name
        elif isinstance(n, ast.Assign):
            yield from (t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            yield n.target.id


def test_the_dock_is_made_of_every_part():
    classes = dict((node.name, node) for _path, node in _classes())
    bases = [ast.unparse(b) for b in classes["ObservationDock"].bases]
    parts = sorted(n for n in classes if n.endswith("Part"))
    assert sorted(b for b in bases if b.endswith("Part")) == parts and bases[-1] == "PluginDock"


def test_each_method_is_in_one_place():
    seen = {}
    for path, node in _classes():
        for name in _members(node):
            assert name not in seen, f"{name}: {seen[name]} と {path} の両方にある"
            seen[name] = path
