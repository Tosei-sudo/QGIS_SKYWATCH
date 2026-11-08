"""
「接続と表の点検」: every ArcGIS service the settings file names, asked once
— whether it answers, whether the sign-in is accepted, whether the
attributes the settings expect are there, and whether the plugin may write
to the ones it writes to — so that a missing column or an expired sign-in
is found before someone's work stops on it (tsudo, 2026-11-08).

The services are found by walking the settings read (core/config.py's
PluginConfig): any entry with an ArcGIS REST `url`, its `fields` (role →
attribute) and `token`. Which entries the plugin writes to is known by
their kind (WRITTEN) and place (WRITTEN_PATHS). The edit stamps
(`editTracking`) are looked for on those written to.

Nothing is written: the layers' own descriptions (`?f=json`) say their
attributes and capabilities.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .esri import EsriClient, EsriError, InvalidTokenError

#: An ArcGIS REST address (a service or a layer of one).
_REST = re.compile(r"/rest/services/", re.IGNORECASE)
#: A layer or table of a feature or map service (one that has attributes).
_LAYER = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)

#: The kinds of settings entries whose layer the plugin adds to or changes (the class names).
WRITTEN = {
    "ObservationStoreConfig", "AreaStoreConfig", "TaxonomyTableConfig", "PlanStoreConfig", "RequestStoreConfig",
    "AssignmentSource", "DetectionOutput",
}
#: Entries written to by where they are in the settings (their path ends so).
WRITTEN_PATHS = ("assignments", "handover.table", "alertReports.table", "events.layer", "events.links", "history")
#: Roles without which the entry cannot be used at all (the others: only the feature that uses them).
NEEDED_ROLES = {"id", "imageId", "objectId", "assignee", "text", "itemId", "eventId"}

OK, NOTE, PROBLEM = "OK", "注意", "問題"


@dataclass
class Target:
    """A service to ask: where in the settings, its address, and what is expected of it."""
    path: str
    url: str
    token: Optional[str] = None
    #: role → attribute name(s).
    fields: dict[str, list[str]] = field(default_factory=dict)
    written: bool = False
    label: str = ""

    @property
    def name(self) -> str:
        return f"{self.path}（{self.label}）" if self.label else self.path


@dataclass
class Finding:
    level: str
    text: str


@dataclass
class Result:
    target: Target
    findings: list[Finding] = field(default_factory=list)

    @property
    def level(self) -> str:
        levels = {f.level for f in self.findings}
        return PROBLEM if PROBLEM in levels else NOTE if NOTE in levels else OK


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def targets(config: Any) -> list[Target]:
    """The ArcGIS services the settings name, each once per address and place, in the order of the file."""
    from .basemaps import DEFAULT_BASE_MAPS

    out: list[Target] = []
    # The built-in background maps (used when the file has none) are not the file's: not asked.
    seen: set[int] = {id(b) for b in DEFAULT_BASE_MAPS}

    def walk(value: Any, path: str) -> None:
        if id(value) in seen:
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            seen.add(id(value))
            url = getattr(value, "url", None)
            if isinstance(url, str) and _REST.search(url):
                raw = getattr(value, "fields", None)
                fields = {k: _names(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                fields = {k: v for k, v in fields.items() if v}
                written = type(value).__name__ in WRITTEN or any(path == p or path.endswith("." + p) for p in WRITTEN_PATHS)
                label = getattr(value, "label", "")
                out.append(Target(path, url.split("?", 1)[0], getattr(value, "token", None), fields, written,
                                  label if isinstance(label, str) else ""))
            for f in dataclasses.fields(value):
                sub = f"{path}.{_camel(f.name)}" if path else _camel(f.name)
                v = getattr(value, f.name)
                if f.name != "url" and isinstance(v, str) and _REST.search(v) and _LAYER.search(v.split("?", 1)[0]):
                    # Another layer named by its address alone (e.g. a ledger's `history`).
                    written = any(sub == p or sub.endswith("." + p) for p in WRITTEN_PATHS)
                    out.append(Target(sub, v.split("?", 1)[0], getattr(value, "token", None), {}, written))
                    continue
                walk(v, sub)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                if dataclasses.is_dataclass(v):
                    walk(v, f"{path}.{k}")

    walk(config, "")
    return out


def _capabilities(raw: dict[str, Any]) -> set[str]:
    caps = raw.get("capabilities")
    return {c.strip().lower() for c in caps.split(",")} if isinstance(caps, str) else set()


def check(client: EsriClient, target: Target, token: Optional[str], stamps: Iterable[str] = (),
          stamps_on: bool = False) -> Result:
    """
    Asks one service. A layer: its attributes (each role's, and the edit
    stamps of a written one), and, written to, whether it may be edited.
    """
    result = Result(target)
    say = result.findings.append
    try:
        if _LAYER.search(target.url):
            info = client.layer(target.url, token, refresh=True)
            raw = info.raw
        else:
            raw = client.ask(target.url, {}, token)
    except InvalidTokenError as error:
        say(Finding(PROBLEM, f"サインインが通らない: {error}（「設定…」の認証設定を確認）"))
        return result
    except EsriError as error:
        signin = "トークン" in str(error) or "権限" in str(error)
        say(Finding(PROBLEM, f"サインインが通らない: {error}（「設定…」の認証設定を確認）" if signin else f"応答なし: {error}"))
        return result
    if not _LAYER.search(target.url):
        say(Finding(OK, "応答あり"))
        return result
    types = {n.lower(): t for n, t in info.field_types.items()}
    if not types:
        say(Finding(NOTE, "属性の一覧が返らない（レイヤーの URL か確認）"))
    else:
        for role, names in target.fields.items():
            missing = [n for n in names if n.lower() not in types]
            if missing and len(missing) == len(names):
                level = PROBLEM if role in NEEDED_ROLES else NOTE
                what = "この表が使えない" if level == PROBLEM else "その項目を使う機能が働かない"
                say(Finding(level, f"属性 {'・'.join(missing)} がない（役割 {role}。{what}）"))
            elif missing:
                say(Finding(NOTE, f"属性 {'・'.join(missing)} がない（役割 {role}。ほかの名前は有る）"))
        if target.written and stamps_on:
            lacking = [n for n in stamps if n.lower() not in types]
            if lacking:
                say(Finding(NOTE, f"作成者・更新者の属性 {'・'.join(lacking)} がない（editTracking。書き込みは記録されない）"))
    if target.written:
        caps = _capabilities(raw)
        if caps and not ({"create", "update", "editing"} & caps):
            say(Finding(PROBLEM, f"編集できない（capabilities: {raw.get('capabilities')}）。このプラグインは書き込む"))
        elif caps and "update" not in caps and "editing" not in caps:
            say(Finding(NOTE, f"追加のみ（capabilities: {raw.get('capabilities')}）。変更・削除は失敗する"))
    if not result.findings:
        say(Finding(OK, "応答あり・属性あり" + ("・編集可" if target.written else "")))
    return result


def check_all(client: EsriClient, config: Any, token_for: Callable[[str, Optional[str]], Optional[str]],
              cancelled: Callable[[], bool] = lambda: False) -> list[Result]:
    """Every service of the settings, one after another (stops when cancelled)."""
    tracking = getattr(config, "edit_tracking", None)
    stamps_on = bool(tracking is not None and getattr(tracking, "enabled", False))
    stamps = list((getattr(tracking, "fields", None) or {}).values()) if stamps_on else []
    out = []
    for t in targets(config):
        if cancelled():
            break
        out.append(check(client, t, token_for(t.url, t.token), stamps, stamps_on))
    return out


def report_text(results: list[Result], when: str = "", where: str = "") -> str:
    """The results as text to copy (for whoever looks after the services)."""
    problems = sum(1 for r in results if r.level == PROBLEM)
    notes = sum(1 for r in results if r.level == NOTE)
    head = ["接続と表の点検" + (f"（{when}）" if when else "")]
    if where:
        head.append(f"設定ファイル: {where}")
    head.append(f"{len(results)} 件: 問題 {problems}・注意 {notes}・OK {len(results) - problems - notes}")
    lines = ["\n".join(head), ""]
    for r in sorted(results, key=lambda r: {PROBLEM: 0, NOTE: 1, OK: 2}[r.level]):
        lines.append(f"[{r.level}] {r.target.name}")
        lines.append(f"  {r.target.url}")
        for f in r.findings:
            lines.append(f"  - {f.text}" if f.level != OK else f"  {f.text}")
    return "\n".join(lines).rstrip() + "\n"
