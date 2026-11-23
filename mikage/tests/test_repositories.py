"""
The データ窓口 (docs/architecture.md): each repository method is the store function of the same name
with the client, the store (table, settings) and the token bound; the SQL's times are written in
core/sql.py alone (as before: the same text).
"""
import ast
import inspect
import os
from datetime import date

import pytest

from mikage.core import sql
from mikage.core.obs import repository as obs_repository
from mikage.core.request import repository as request_repository

REPOSITORIES = [
    (request_repository, request_repository.RequestRepository, "store"),
    (request_repository, request_repository.ScheduleRepository, "store"),
    (obs_repository, obs_repository.AssignmentRepository, "source"),
    (obs_repository, obs_repository.AlertLogRepository, "settings"),
]
#: The store's own parameter where the function names it otherwise.
HELD = {"read_groups": None, "waiting_ids": None, "load_groups": None, "group_table_fields": None, "save_group": None,
        "save_group_orders": None, "delete_groups": None}


def _methods(cls):
    for name, value in inspect.getmembers(cls, inspect.isfunction):
        if not name.startswith("_") and name not in HELD:
            yield name, value


@pytest.mark.parametrize("module, cls, held", REPOSITORIES, ids=lambda x: getattr(x, "__name__", str(x)))
def test_each_method_is_its_function_with_the_three_bound(module, cls, held, monkeypatch):
    client, store, token = object(), object(), object()
    repo = cls(client, store, token)
    for name, method in _methods(cls):
        function = getattr(module, name)
        target = inspect.signature(function)
        seen = {}
        monkeypatch.setattr(module, name, lambda *a, _t=target, **k: seen.update(_t.bind(*a, **k).arguments))
        given = {p: object() for p in list(inspect.signature(method).parameters)[1:]}
        getattr(repo, name)(**given)
        names = list(target.parameters)
        assert seen[names[0]] is client and seen[held if held in names else names[1]] is store, name
        assert seen.get("token") is token, f"{cls.__name__}.{name}: the token"
        for p, v in given.items():
            assert seen[p] is v, f"{cls.__name__}.{name}: {p}"


def test_the_ledgers_groups_and_waiting_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(request_repository, "read_groups", lambda *a: calls.append(("groups", a)))
    monkeypatch.setattr(request_repository, "waiting_ids", lambda *a: calls.append(("waiting", a)))
    store = type("S", (), {"groups": "G", "group_fields": {"name": "N"}})()
    repo = request_repository.RequestRepository("client", store, "token")
    repo.read_groups("cancelled")
    repo.waiting_ids()
    assert calls == [("groups", ("client", "G", {"name": "N"}, "token", "cancelled")), ("waiting", ("client", store))]


def test_the_groups_table_is_the_stores(monkeypatch):
    """「依頼グループ…」 (2026-11-23): the groups table's functions get the store's groups URL and attribute names."""
    calls = []
    for name in ("load_groups", "save_group", "save_orders", "delete_groups"):
        monkeypatch.setattr(request_repository, name, lambda *a, _n=name: calls.append((_n, a)))
    store = type("S", (), {"groups": "G", "group_fields": {"name": "N"}})()
    repo = request_repository.RequestRepository("client", store, "token")
    repo.load_groups("cancelled")
    repo.save_group("g")
    repo.save_group_orders(["g"])
    repo.delete_groups(["g"])
    assert calls == [("load_groups", ("client", "G", {"name": "N"}, "token", "cancelled")),
                     ("save_group", ("client", "G", {"name": "N"}, "g", "token")),
                     ("save_orders", ("client", "G", {"name": "N"}, ["g"], "token")),
                     ("delete_groups", ("client", "G", ["g"], "token"))]


def test_the_sql_times_are_written_as_before():
    ms = 1_790_000_000_123  # 2026-09-21 14:13:20.123 UTC
    assert sql.timestamp(ms) == "TIMESTAMP '2026-09-21 14:13:20'"
    assert sql.date_of(ms) == "DATE '2026-09-21'"
    assert sql.date_text("2026-09-22") == "DATE '2026-09-22'"
    assert sql.day_start(date(2026, 9, 21)) == "TIMESTAMP '2026-09-21 00:00:00'"


def test_no_other_module_writes_the_sql_times():
    """`TIMESTAMP '…'` / `DATE '…'` only in core/sql.py (and time_zone, which moves them to a service's zone)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allowed = {os.path.join("core", "sql.py"), os.path.join("core", "time_zone.py")}
    found = []
    for folder, _dirs, files in os.walk(root):
        if os.sep + "tests" in folder or "__pycache__" in folder or os.sep + "ext" in folder:
            continue
        for f in files:
            path = os.path.join(folder, f)
            rel = os.path.relpath(path, root)
            if not f.endswith(".py") or rel in allowed:
                continue
            for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
                if isinstance(node, ast.JoinedStr):
                    text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
                    if "TIMESTAMP '" in text or "DATE '" in text:
                        found.append(f"{rel}:{node.lineno}")
    assert not found, f"SQL の日時は core/sql.py で書く: {found}"
