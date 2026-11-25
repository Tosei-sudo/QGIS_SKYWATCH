"""
What each operation of 文で操作 does (core/text_command.OPERATIONS), on the
screen it is given: the values of the card put in it, then only what its
own button would that reads (計算, 検索, 表示). Writing, importing, saving and
sending stay the screen's own buttons, with their own questions.

Each feature hands the plugin the operations on its own docks
(features/<name>.py `text_operations`), calling these with them; each
function says what was done (the status bar's message).
"""
from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any, Optional

from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, QTime, QTimer

from ..core.catalog.api import SENSOR_SEPARATOR
from ..core.text_command import ALL, Reading, center_of, corners_of, extent_of, jst_bounds


def tr(text: str) -> str:
    return QCoreApplication.translate("TextCommand", text)


def qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def choose(combo, value) -> None:
    """The combo's item of that data (the first when none has it)."""
    combo.setCurrentIndex(max(0, combo.findData(value)))


def system_of(reading: Reading) -> Optional[str]:
    """The one system chosen (None: every one)."""
    s = reading.systems[0] if reading.systems else ALL
    return None if s == ALL else s


def system_label(systems, code: Optional[str]) -> str:
    if code is None:
        return tr("すべてのシステム")
    return next((s.label for s in systems if s.system == code), code)


def point_of(place) -> tuple[float, float]:
    """A place found (core/text_request.AreaChoice) as a point: the middle of its area."""
    return center_of(place.rings)


def front(dock):
    dock.show()
    dock.raise_()
    return dock


# ---- 撮像依頼 (features/requests.py) ---------------------------------------------------------------------

def plan_files(dock, r: Reading, systems) -> str:
    """撮像計画ファイル出力: the day and the system, then 「計算」 (writing the files is the dock's 「書き出し」)."""
    front(dock)
    d = r.day
    dock.day.setDate(qdate(d))
    code = system_of(r)
    choose(dock.system_filter, code)
    dock.run()
    return tr("撮像計画ファイル出力: {d}・{s} を計算（書き出しは画面で）").format(d=d.isoformat(), s=system_label(systems, code))


def schedule(dock, r: Reading) -> str:
    """撮像予定: the system and the day shown."""
    front(dock)
    choose(dock.system_filter, system_of(r))
    if r.day:
        dock.set_day(r.day)
    return tr("撮像予定: {d}").format(d=(r.day or dock.chosen_day()).isoformat())


def result_import(dock, r: Reading, systems) -> str:
    """撮像予定: the day read, then its 結果ファイル chosen (the import asks before it saves, as from its button)."""
    front(dock)
    code = system_of(r)
    choose(dock.system_filter, code)
    system = next((s for s in systems if s.system == code), None) if code else None
    if r.day:
        dock.set_day(r.day)
    if dock.day and (r.day is None or dock.day == r.day.isoformat()):
        QTimer.singleShot(0, lambda: dock.import_file(system=system))
    else:
        # The day read first (the file's rows are matched with that day's).
        dock.reload(then=lambda: dock.import_file(system=system))
    return tr("撮像予定: {s} の結果ファイルを選ぶ").format(s=system_label(systems, code))


def follow(dock, r: Reading) -> str:
    """計画と実績の照合: the period, read (照合, which saves to the history and the ledger, is the dialog's 「照合」)."""
    front(dock)
    dialog = dock.show_results(run=False)
    if r.period:
        for edit, d in ((dialog.first, r.period[0]), (dialog.last, r.period[1])):
            edit.setDate(qdate(d))
    dialog.run(follow=False)
    first, last = dialog.span()
    return tr("計画と実績の照合: {a}〜{b} を表示（照合は画面の「照合」で）").format(a=first.isoformat(), b=last.isoformat())


def quotas(dialog, r: Reading) -> str:
    """年間撮像上限: the year."""
    if dialog is not None and r.year_back:
        i = dialog.year.findData(r.year_back)
        if i >= 0:
            dialog.year.setCurrentIndex(i)
    return tr("年間撮像上限")


def place_schedule(dock, r: Reading, place) -> str:
    """地点ごとの撮像予定: the place added."""
    front(dock)
    lon, lat = point_of(place)
    dock.add_point(lon, lat, r.place or "")
    return tr("地点ごとの撮像予定: {p}").format(p=place.label)


def new_request(dock, r: Reading) -> str:
    """撮像依頼: a new request's form, the sentence given to 依頼文から下書き when it shows."""
    front(dock)
    dock.edit(new=True)
    form = dock.form
    row = getattr(form, "draft_row", None) if form is not None else None
    if row is not None and row.offered:
        row.edit.setPlainText(r.text)
        QTimer.singleShot(0, row.ask)
        return tr("撮像依頼: 新規の依頼に文から下書き（保存は画面で）")
    return tr("撮像依頼: 新規の依頼（保存は画面で）")


def outage(dialog, r: Reading) -> str:
    """衛星・システムの障害情報: the form to add one, with the satellite (else the system) and the sentence (saved from the form)."""
    form = dialog.add() if dialog is not None else None
    if form is not None:
        if r.satellites:
            form.satellite.setCurrentText(r.satellites[0])
        elif r.systems and r.systems[0] != ALL and form.kind.isEnabled():
            form.kind.setCurrentIndex(1)
            form.system.setCurrentText(r.systems[0])
        form.note.setPlainText(r.text)
    return tr("衛星・システムの障害情報: 登録の画面（保存は画面で）")


# ---- 画像 (features/images.py) --------------------------------------------------------------------------

def catalog(dock, r: Reading, place) -> str:
    """
    画像カタログ: searched over the period and the place (its polygon; else the map's extent), and the sensors 衛星・種別
    names (features/command.py: the catalog's; none: every sensor, whatever the センサー box had).
    """
    first, last = r.period or (None, None)
    start, end = (first.isoformat() if first else ""), (last.isoformat() if last else "")
    ring = None
    if place is not None and place.rings:
        ring = [list(p) for p in place.rings[0]] if place.kind == "範囲" else corners_of(place.rings)
    dock.search_area_period(start, end, ring, sensor=SENSOR_SEPARATOR.join(r.catalog_sensors))
    where = place.label if place is not None else tr("地図の表示範囲")
    said = tr("画像カタログ: {w}・{a}〜{b}").format(w=where, a=start or tr("指定なし"), b=end or tr("指定なし"))
    if r.catalog_sensors:
        said += tr("・{s}（{n} センサー）").format(s="・".join(r.sensors), n=len(r.catalog_sensors))
    return said + tr(" を検索")


def watch(dock, place) -> str:
    """新着画像の監視: the map's 「この地点の新着画像を監視…」 (its dialog after the card has closed)."""
    lon, lat = point_of(place)
    QTimer.singleShot(0, lambda: dock.watch_point(lon, lat))
    return tr("新着画像の監視: {p}（登録は画面で）").format(p=place.label)


# ---- 撮像計画 (features/plan.py) ------------------------------------------------------------------------

def passes(dock, r: Reading) -> str:
    """衛星パス: the satellites, over the day (else now)."""
    front(dock)
    if r.day:
        start, end = jst_bounds(r.day)
        dock.set_time(start=start, end=end)
    dock.choose(r.satellites)
    return tr("衛星パス: {s}").format(s="・".join(r.satellites))


def imaging_plan(dock, r: Reading, place, today: date, days: tuple) -> str:
    """撮像計画: the place the target, the period (its days as the dock offers them), then 「計算」."""
    if r.period:
        first, last = r.period
        start = max(first, today)
        dock.start.setDateTime(QDateTime(qdate(start), QTime(0, 0)))
        n = (last - start).days + 1
        choose(dock.days, next((d for d in days if d >= n), days[-1]))
    lon, lat = point_of(place)
    dock.plan_at(lon, lat)
    return tr("撮像計画: {p} を計算").format(p=place.label)


# ---- 観測記録 (features/observations.py) -----------------------------------------------------------------

def obs_export(dock, r: Reading) -> str:
    """観測記録のエクスポート: the conditions the sentence says (言葉で探す's readers, no model), the period of the card."""
    from ..core.text_tasks import search_draft

    dialog = dock.open_export()
    text, period = r.text, r.period

    def fill() -> None:
        places, roots, confidences = dialog.search_choices()
        found = search_draft("", places, roots, confidences, text, dialog.today(), dialog.search_kinds())
        if period is not None:
            found = dataclasses.replace(found, start=period[0], end=period[1])
        dialog.apply_search(found)

    dialog.prepare(fill)
    return tr("観測記録のエクスポート: 文の条件を入れた（保存は画面で）")


def handover(dock) -> str:
    dock.open_handover(1)
    return tr("引き継ぎメモ")


def alert(dock) -> str:
    dock.open_alerts(0)
    return tr("通報文の作成（送信は画面で）")


# ---- 地図 (features/maps.py) ----------------------------------------------------------------------------

def jump(box, r: Reading, place: Any) -> str:
    """座標・地名へ移動: a place found (its area, or its point), else the text as the field reads it."""
    if place is not None and place.source != "座標":
        from ..core.geocode import Place

        e = extent_of(place.rings) if place.kind == "範囲" else None
        box.go_to_place(Place(r.place or place.label, center_of(place.rings), place.source, extent=e))
        return tr("移動: {p}").format(p=place.label)
    box.field.setText(r.place or "")
    box.submit()
    return tr("移動: {p}").format(p=r.place or "")
