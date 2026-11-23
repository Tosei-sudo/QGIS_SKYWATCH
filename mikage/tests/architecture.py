"""
The plugin's architecture rules, read from the source (no import): which
module imports which, and which reaches into another object's private
members. test_architecture.py checks them against
architecture_baseline.json, the violations there were when the rules came
(2026-11-20): a new one fails the test, one fixed must leave the list, so
the list only gets shorter.

Rules (docs/architecture.md):
- R1 layers: `core` imports neither QGIS / Qt nor the plugin's other
  layers; `qgis_io` not `ui`, `features`, `processing_provider` or the
  plugin; `ui` and `processing_provider` not `features` or the plugin; a
  module of `features` no other feature's (only `features.base`).
- R2 a feature's door: a module outside a feature package of `core`
  (`core/plan`, `core/request`, `core/obs`, `core/catalog`, `core/enhance`,
  `core/ai`, `core/training`) imports it only through its `api` module.
- R3 no reaching in: no `other._name` (another object's private member) in
  `ui`, `features` and plugin.py.
- R4 the shared base knows no feature: the kernel modules (KERNEL) import
  only each other (and Python's / QGIS's own).
- R5 no hidden cycles: no import cycle (imports inside functions count)
  spans two feature packages.

Run `MIKAGE_UPDATE_ARCHITECTURE=1 pytest mikage/tests/test_architecture.py`
to write the baseline again (only to take entries out of it).
"""
from __future__ import annotations

import ast
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

PACKAGE = "mikage"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture_baseline.json")

#: The feature packages of `core`: imported from outside only through `<package>.api`.
FEATURE_PACKAGES = ("core.ai", "core.catalog", "core.enhance", "core.obs", "core.plan", "core.request", "core.training")

#: The shared base (共通基盤): imports no feature and nothing above it.
KERNEL = (
    "core.esri", "core.util", "core.time_zone", "core.time_text", "core.audit", "core.edit_stamp", "core.gpkg", "core.offline",
    "core.auth", "core.json_pieces", "core.coords", "core.shapes", "core.sql", "core.sources", "core.win64",
    # The features' doors read when asked (core/<機能>/api.py).
    "core.door",
    # Shared by two features each: an image recorded on without its file (the catalog makes them, the observations
    # keep them) and the values of the files written for others (撮像依頼の日次出力, 観測記録のエクスポート).
    "core.listed_image", "core.json_schema", "core.file_values",
    # The query results kept a few minutes (データ分析 reads them; core.esri forgets a layer's on an edit).
    "core.analysis_cache",
    "qgis_io.tasks", "qgis_io.network", "qgis_io.geometry",
    # 「一番上に固定」: where whatever adds at the top of the layer tree puts it (below the pinned nodes).
    "qgis_io.pinned_layers",
    "ui.fit", "ui.flow_layout", "ui.wheel_guard",
)

#: Private names that are public by convention (namedtuple's, Qt's).
PUBLIC_UNDERSCORE = {"_replace", "_asdict", "_fields", "_make", "_field_defaults"}


@dataclass
class Module:
    name: str  # without the package: "ui.observation_dock"
    path: str
    tree: ast.Module
    #: (imported module, at the top of the module or inside a function)
    imports: list = field(default_factory=list)


def _layer(name: str) -> str:
    return name.split(".", 1)[0] if "." in name else name


def modules(root: str = ROOT) -> dict[str, Module]:
    """Every module of the plugin but its tests and the bundled libraries, by name ("plugin", "ui.docks"…)."""
    out: dict[str, Module] = {}
    for folder, dirs, files in os.walk(root):
        rel = os.path.relpath(folder, root)
        parts = [] if rel == "." else rel.split(os.sep)
        if parts[:1] in (["tests"], ["ext"], ["help"], ["resources"]) or "__pycache__" in parts:
            dirs[:] = []
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            stem = f[:-3]
            name = ".".join(parts + ([] if stem == "__init__" else [stem])) or "__init__"
            path = os.path.join(folder, f)
            with open(path, encoding="utf-8") as h:
                out[name] = Module(name, path, ast.parse(h.read(), path))
    for m in out.values():
        m.imports = list(_imports(m, out))
    return out


def _imports(m: Module, known: dict) -> Iterable[tuple[str, bool]]:
    is_package = m.path.endswith("__init__.py")
    inside_functions = {id(child) for node in ast.walk(m.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                        for child in ast.walk(node) if isinstance(child, (ast.Import, ast.ImportFrom))}
    top_level = {id(node) for node in ast.walk(m.tree) if isinstance(node, (ast.Import, ast.ImportFrom))} - inside_functions
    for node in ast.walk(m.tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = m.name.split(".") if m.name != "__init__" else []
                base = base if is_package else base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
                if target == PACKAGE or target.startswith(PACKAGE + "."):
                    target = target[len(PACKAGE) + 1:]
                else:
                    yield target, id(node) in top_level
                    continue
            for alias in node.names:
                full = f"{target}.{alias.name}" if target else alias.name
                names.append(full if full in known else target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    names.append(alias.name[len(PACKAGE) + 1:])
                else:
                    yield alias.name, id(node) in top_level
        for name in names:
            while name and name not in known and "." in name:
                name = name.rsplit(".", 1)[0]
            if name in known and name != m.name:
                yield name, id(node) in top_level
            elif name and name not in known:
                yield name, id(node) in top_level  # outside the plugin (ext.sgp4…)


def feature_of(name: str) -> Optional[str]:
    for package in FEATURE_PACKAGES:
        if name == package or name.startswith(package + "."):
            return package
    return None


# ---- the rules -------------------------------------------------------------------------------------------


def layer_violations(mods: dict[str, Module]) -> list[str]:
    """R1: imports against the layers' direction."""
    forbidden = {
        "core": ("qgis", "PyQt5", "PyQt6", "qgis_io", "ui", "features", "processing_provider", "plugin", "plugin_api", "scheduled_follow"),
        "qgis_io": ("ui", "features", "processing_provider", "plugin", "plugin_api"),
        "ui": ("features", "plugin", "plugin_api"),
        "processing_provider": ("ui", "features", "plugin", "plugin_api"),
    }
    out = []
    for m in mods.values():
        bad = forbidden.get(_layer(m.name), ())
        for target, _top in m.imports:
            if _layer(target) in bad or target.split(".")[0] in bad:
                out.append(f"{m.name} -> {target}")
            elif _layer(m.name) == "features" and _layer(target) == "features" and target not in ("features", "features.base"):
                out.append(f"{m.name} -> {target}")  # one feature into another: through the plugin (its commands, host)
    return sorted(set(out))


def deep_imports(mods: dict[str, Module]) -> list[str]:
    """R2: a feature package's inside imported from outside it (not through its `api`)."""
    out = set()
    for m in mods.values():
        own = feature_of(m.name)
        for target, _top in m.imports:
            package = feature_of(target)
            if package is None or package == own:
                continue
            if target in (package + ".api",):
                continue
            out.add(f"{m.name} -> {target}")
    return sorted(out)


def private_access(mods: dict[str, Module]) -> dict[str, int]:
    """R3: `x._name` where x is not self / cls, per module of ui, features and plugin.py."""
    out: dict[str, int] = {}
    for m in mods.values():
        if _layer(m.name) not in ("ui", "features", "plugin"):
            continue
        n = 0
        for node in ast.walk(m.tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            if not attr.startswith("_") or attr.startswith("__") or attr in PUBLIC_UNDERSCORE:
                continue
            value = node.value
            if isinstance(value, ast.Name) and value.id in ("self", "cls"):
                continue
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "super":
                continue
            n += 1
        if n:
            out[m.name] = n
    return dict(sorted(out.items()))


def kernel_violations(mods: dict[str, Module]) -> list[str]:
    """R4: a kernel module importing a module of the plugin that is not in the kernel."""
    out = []
    for name in KERNEL:
        m = mods.get(name)
        if m is None:
            continue
        for target, _top in m.imports:
            if target in mods and target not in KERNEL:
                out.append(f"{name} -> {target}")
    return sorted(set(out))


def cycles(mods: dict[str, Module], lazy: bool = True) -> list[list[str]]:
    """The import cycles (strongly connected modules), imports inside functions counted when `lazy`."""
    graph: dict[str, set] = defaultdict(set)
    for m in mods.values():
        for target, top in m.imports:
            if target in mods and (top or lazy):
                graph[m.name].add(target)
    index: dict = {}
    low: dict = {}
    stack: list = []
    on: set = set()
    found: list = []
    counter = [0]

    def visit(v) -> None:
        # Iterative Tarjan (deep chains of imports would pass Python's recursion limit).
        work = [(v, iter(sorted(graph.get(v, ()))))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, children = work[-1]
            advanced = False
            for w in children:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    found.append(sorted(comp))

    for v in sorted(mods):
        if v not in index:
            visit(v)
    return sorted(found)


def feature_cycles(mods: dict[str, Module]) -> list[str]:
    """R5: the cycles that span two feature packages (or a feature package and the rest of `core`)."""
    out = []
    for comp in cycles(mods):
        features = {feature_of(n) for n in comp if feature_of(n)}
        if len(features) > 1 or (features and any(n.startswith("core.") and not feature_of(n) for n in comp)):
            out.append(" / ".join(comp))
    return sorted(out)


def unresolved_imports(mods: dict[str, Module]) -> list[str]:
    """
    The relative imports (in functions too) naming a module the plugin does not have: a module moved into a folder
    (ui/obs_dock…) whose imports were not shifted one level down fails only when that function runs.
    """
    out = []
    for m in mods.values():
        is_package = m.path.endswith("__init__.py")
        for node in ast.walk(m.tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            base = m.name.split(".") if m.name != "__init__" else []
            base = base if is_package else base[:-1]
            if node.level - 1 > len(base):
                out.append(f"{m.name}:{node.lineno} {'.' * node.level}{node.module or ''}")
                continue
            base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            target = ".".join(base + ([node.module] if node.module else []))
            if target and target not in mods and target.split(".")[0] != "ext":  # ext: the bundled libraries
                out.append(f"{m.name}:{node.lineno} {'.' * node.level}{node.module or ''}")
    return sorted(out)


def measure(root: str = ROOT) -> dict:
    mods = modules(root)
    return {
        "layers": layer_violations(mods),
        "deep_imports": deep_imports(mods),
        "private_access": private_access(mods),
        "kernel": kernel_violations(mods),
        "feature_cycles": feature_cycles(mods),
    }


def load_baseline() -> dict:
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def write_baseline(measured: dict) -> None:
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(measured, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def summary(measured: dict) -> str:
    return ", ".join(f"{k} {len(v) if not isinstance(v, dict) else sum(v.values())}" for k, v in measured.items())


if __name__ == "__main__":  # pragma: no cover
    print(summary(measure()))
    print(re.sub(r"\s+", " ", json.dumps(measure(), ensure_ascii=False))[:2000])
