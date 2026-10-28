"""
「ArcGIS Web マップを開く…」 and 「ArcGIS Web マップとして保存…」, as the
viewer's webmap-dialog.ts: a web map of a portal (by its item page, Map
Viewer link or item id) or a web map JSON file opened as QGIS layers, and
the project written as a web map, to a .json file or to a portal as a new
item or over the one opened (after asking).

The portal's sign-in is a QGIS authentication configuration (OAuth2, ESRI
token…), kept for the portal like the services' in the plugin settings.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

from qgis.core import QgsProject, QgsSettings
from qgis.gui import QgsAuthConfigSelect
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..core.webmap import (
    DEFAULT_PORTAL, MAX_WEBMAP_READ, SaveResult, WebMapItemRef, item_extent, item_page, parse_item_input, plan_web_map, portal_root,
)
from ..qgis_io import settings
from ..qgis_io.tasks import run_task
from ..qgis_io.webmap_io import (
    add_opened, auth_with_portal, fetch_web_map, open_plan, project_web_map, save_to_portal, view_in_degrees,
)

TITLE = "ArcGIS Web マップ"
PORTAL_KEY = "satellite_observation/webmap_portal"


def _tr(text: str) -> str:
    return QCoreApplication.translate("WebMapDialogs", text)


class PortalRow(QObject):
    """A portal (the settings file's `arcgisPortals`, ArcGIS Online, or typed) and its sign-in."""

    def __init__(self, context, parent):
        super().__init__(parent)
        self.context = context
        self.combo = QComboBox(parent)
        self.combo.setEditable(True)
        self.combo.setMinimumContentsLength(30)
        self.auth = QgsAuthConfigSelect(parent)
        self.combo.currentTextChanged.connect(self._portal_changed)

    def fill(self, wanted: str = "") -> None:
        urls = [p.url for p in self.context.config.arcgis_portals]
        labels = {p.url: p.label for p in self.context.config.arcgis_portals}
        remembered = QgsSettings().value(PORTAL_KEY, "") or ""
        self.combo.blockSignals(True)
        self.combo.clear()
        for url in dict.fromkeys([*urls, DEFAULT_PORTAL, *([remembered] if remembered else [])]):
            self.combo.addItem(url, labels.get(url, ""))
            if labels.get(url):
                self.combo.setItemData(self.combo.count() - 1, labels[url], Qt.ItemDataRole.ToolTipRole)
        self.combo.setEditText(wanted or remembered or (urls[0] if urls else DEFAULT_PORTAL))
        self.combo.blockSignals(False)
        self._portal_changed(self.combo.currentText())

    def _portal_changed(self, text: str) -> None:
        root = portal_root(text)
        self.auth.setConfigId(settings.auth_for(root) or "" if root else "")

    def portal(self) -> Optional[str]:
        return portal_root(self.combo.currentText() or DEFAULT_PORTAL)

    def keep(self, portal: str) -> Optional[str]:
        """Remembers the portal and its sign-in (for the services of its web maps too); the authcfg."""
        QgsSettings().setValue(PORTAL_KEY, portal)
        chosen = self.auth.configId() or None
        mapping = settings.auth_map()
        if chosen and mapping.get(portal) != chosen:
            settings.set_auth_map({**mapping, portal: chosen})
        return chosen or settings.auth_for(portal)


def _list_item(text: str, problem: bool) -> QListWidgetItem:
    item = QListWidgetItem(text)
    item.setToolTip(text)
    if problem:
        item.setForeground(Qt.GlobalColor.darkRed)
    return item


class WebMapOpenDialog(QDialog):
    def __init__(self, owner: "WebMapDialogs", parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setWindowTitle(_tr("ArcGIS Web マップを開く"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.item = QLineEdit()
        self.item.setPlaceholderText(_tr("https://…/home/item.html?id=…、Map Viewer の URL、またはアイテム ID"))
        self.item.returnPressed.connect(self.open_typed)
        form.addRow(_tr("アイテム"), self.item)
        self.portal = PortalRow(owner.context, self)
        form.addRow(_tr("ポータル（ID だけのとき）"), self.portal.combo)
        form.addRow(_tr("認証設定（非公開のアイテム）"), self.portal.auth)
        layout.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.result = QListWidget()
        self.result.setVisible(False)
        layout.addWidget(self.result, 1)
        buttons = QHBoxLayout()
        from_file = QPushButton(_tr("JSON ファイルから開く…"))
        from_file.clicked.connect(lambda: self.open_file())
        buttons.addWidget(from_file)
        buttons.addStretch(1)
        close = QPushButton(_tr("閉じる"))
        close.clicked.connect(self.reject)
        self.open_button = QPushButton(_tr("開く"))
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self.open_typed)
        buttons.addWidget(close)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        self.resize(620, 300)

    def prepare(self) -> None:
        self.portal.fill()
        self.status.clear()
        self.result.clear()
        self.result.setVisible(False)

    def open_typed(self) -> None:
        text = self.item.text().strip()
        if not text:
            self.status.setText(_tr("アイテムの URL か ID を入力（JSON ファイルは「JSON ファイルから開く…」）"))
            return
        ref = parse_item_input(text, self.portal.portal() or DEFAULT_PORTAL)
        if ref is None:
            self.status.setText(_tr("アイテムの URL（…/home/item.html?id=…、…/apps/mapviewer/index.html?webmap=…）か 32 桁のアイテム ID を入力"))
            return
        authcfg = self.portal.keep(ref.portal)
        self.owner.open_item(ref, authcfg)

    def open_file(self, path: Optional[str] = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, _tr("Web マップの JSON ファイル"), "", _tr("JSON (*.json)"))
            if not path:
                return
        portal = self.portal.portal()
        self.owner.open_file(path, portal, self.portal.keep(portal) if portal else None)

    def busy(self, on: bool) -> None:
        self.open_button.setEnabled(not on)
        if on:
            self.status.setText(_tr("読み込み中…"))
            self.result.clear()
            self.result.setVisible(False)

    def show_result(self, summary: str, lines: list) -> None:
        self.status.setText(summary)
        self.result.clear()
        for text, problem in lines:
            self.result.addItem(_list_item(text, problem))
        self.result.setVisible(bool(lines))


class WebMapSaveDialog(QDialog):
    def __init__(self, owner: "WebMapDialogs", parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setWindowTitle(_tr("ArcGIS Web マップとして保存"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit()
        form.addRow(_tr("タイトル"), self.title)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("satellite_observation")
        form.addRow(_tr("タグ（カンマ区切り）"), self.tags)
        layout.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.skipped = QListWidget()
        layout.addWidget(self.skipped, 1)
        self.portal_box = QGroupBox(_tr("ポータルに保存"))
        portal_form = QFormLayout(self.portal_box)
        self.portal = PortalRow(owner.context, self)
        portal_form.addRow(_tr("ポータル"), self.portal.combo)
        portal_form.addRow(_tr("認証設定"), self.portal.auth)
        self.user = QLineEdit()
        self.user.setPlaceholderText(_tr("空欄: サインインのユーザー"))
        portal_form.addRow(_tr("ユーザー名"), self.user)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(_tr("任意"))
        portal_form.addRow(_tr("フォルダー"), self.folder)
        self.update = QCheckBox()
        portal_form.addRow(self.update)
        self.portal_button = QPushButton(_tr("ポータルに保存…"))
        self.portal_button.clicked.connect(self.save_portal)
        portal_form.addRow(self.portal_button)
        layout.addWidget(self.portal_box)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(_tr("閉じる"))
        close.clicked.connect(self.reject)
        self.file_button = QPushButton(_tr("JSON として保存…"))
        self.file_button.clicked.connect(lambda: self.save_file())
        buttons.addWidget(close)
        buttons.addWidget(self.file_button)
        layout.addLayout(buttons)
        self.resize(620, 520)
        #: Asks before writing to a portal (replaced in tests).
        self.confirm: Callable[[str, str], bool] = self._confirm

    def prepare(self) -> None:
        opened = self.owner.opened
        self.title.setText(opened[1] if opened else (QgsProject.instance().baseName() or self.title.text()))
        self.portal.fill(opened[0].portal if opened and opened[0] else "")
        self.status.clear()
        self.show_update()
        self.show_summary(self.owner.build())

    def show_update(self) -> None:
        opened = self.owner.opened
        item = opened[0] if opened else None
        self.update.setVisible(bool(item))
        self.update.setChecked(False)
        if item:
            self.update.setText(_tr("開いた Web マップ（{id}）に上書き").format(id=item.id))

    def show_summary(self, result: SaveResult) -> None:
        if result.included:
            self.summary.setText(_tr("{n} レイヤーを書き込み: {names}").format(n=len(result.included), names="、".join(result.included)))
        else:
            self.summary.setText(_tr("書き込めるレイヤーなし（表示範囲と背景地図のみ書き込み）"))
        self.skipped.clear()
        for title, reason in result.skipped:
            self.skipped.addItem(_list_item(_tr("書き込み不可: {title} — {reason}").format(title=title, reason=reason), True))
        self.skipped.setVisible(bool(result.skipped))

    def name(self) -> str:
        opened = self.owner.opened
        return self.title.text().strip() or (opened[1] if opened else "") or QgsProject.instance().baseName() or "Web マップ"

    def save_file(self, path: Optional[str] = None) -> Optional[str]:
        result = self.owner.build()
        self.show_summary(result)
        if path is None:
            suggested = re.sub(r'[\\/:*?"<>|]+', "_", self.name()) + ".json"
            path, _ = QFileDialog.getSaveFileName(self, _tr("Web マップの JSON ファイル"), suggested, _tr("JSON (*.json)"))
            if not path:
                return None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.json, f, ensure_ascii=False)
        self.status.setText(_tr("{name} を保存").format(name=os.path.basename(path)))
        return path

    def _confirm(self, text: str, action: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, _tr("ArcGIS Web マップとして保存"), text, parent=self)
        yes = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(_tr("取消"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def save_portal(self) -> None:
        portal = self.portal.portal()
        if not portal:
            self.status.setText(_tr("ポータルの URL を入力"))
            return
        authcfg = self.portal.keep(portal)
        if not authcfg:
            self.status.setText(_tr("ポータルに保存するには認証設定を選択"))
            return
        opened = self.owner.opened
        item = opened[0] if opened else None
        update = item.id if self.update.isChecked() and item and item.portal == portal else None
        result = self.owner.build()
        self.show_summary(result)
        title, user, folder = self.name(), self.user.text().strip(), self.folder.text().strip()
        if update:
            text = _tr("{portal} の Web マップ（{id}）を「{title}」で上書き（{n} レイヤー）。よろしいですか？").format(
                portal=portal, id=update, title=title, n=len(result.included))
        else:
            where = (_tr("ユーザー {user}").format(user=user) if user else _tr("サインインのユーザー")) + (_tr(" のフォルダー「{f}」").format(f=folder) if folder else "")
            text = _tr("{portal} の{where}に、Web マップ「{title}」を新規作成（{n} レイヤー）。よろしいですか？").format(
                portal=portal, where=where, title=title, n=len(result.included))
        if not self.confirm(text, _tr("上書き") if update else _tr("作成")):
            self.status.setText(_tr("保存取消"))
            return
        self.status.setText(_tr("ポータルに書き込み中…"))
        self.portal_button.setEnabled(False)
        self.owner.save_portal(portal, authcfg, result, title, self.tags.text().strip(), user, folder, update)

    def saved(self, portal: str, item: str, updated: bool) -> None:
        self.portal_button.setEnabled(True)
        link = item_page(WebMapItemRef(portal, item))
        self.status.setText(_tr("{what}完了（アイテム <a href=\"{link}\">{id}</a>）").format(what=_tr("上書き保存") if updated else _tr("保存"), link=link, id=item))
        self.show_update()

    def failed(self, message: str) -> None:
        self.portal_button.setEnabled(True)
        self.status.setText(_tr("保存失敗: {e}").format(e=message))


class WebMapDialogs(QObject):
    """The two dialogs, and the web map opened (or saved) last, to save over it."""

    def __init__(self, iface, context, set_base_map: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.set_base_map = set_base_map
        #: (item or None, title) of the web map opened or saved last.
        self.opened: Optional[tuple[Optional[WebMapItemRef], str]] = None
        self.open_dialog = WebMapOpenDialog(self, iface.mainWindow())
        self.save_dialog = WebMapSaveDialog(self, iface.mainWindow())
        self.task = None

    def show_open(self) -> None:
        self.open_dialog.prepare()
        self.open_dialog.show()
        self.open_dialog.raise_()
        self.open_dialog.item.setFocus()

    def show_save(self) -> None:
        self.save_dialog.prepare()
        self.save_dialog.show()
        self.save_dialog.raise_()
        self.save_dialog.title.setFocus()

    # Opening.

    def open_item(self, ref: WebMapItemRef, authcfg: Optional[str]) -> None:
        auth = auth_with_portal(ref.portal, authcfg)
        base_maps = list(self.context.config.base_maps)

        def work(task):
            item, data = fetch_web_map(ref, auth)
            title = item.get("title") if isinstance(item.get("title"), str) and item["title"].strip() else ref.id
            return open_plan(plan_web_map(data), title, base_maps, auth, item_extent(item), task.check), ref

        self._run(_tr("ArcGIS Web マップの読み込み"), work)

    def open_file(self, path: str, portal: Optional[str] = None, authcfg: Optional[str] = None) -> None:
        try:
            if os.path.getsize(path) > MAX_WEBMAP_READ:
                raise ValueError(_tr("ファイルが大きすぎ"))
            with open(path, "rb") as f:
                try:
                    data = json.loads(f.read().decode("utf-8-sig"))
                except (ValueError, UnicodeDecodeError):
                    raise ValueError(_tr("JSON として読み込み不可")) from None
            plan = plan_web_map(data)
        except (OSError, ValueError) as error:
            self.open_dialog.show_result(_tr("読み込み失敗: {e}").format(e=error), [])
            return
        auth = auth_with_portal(portal, authcfg)
        base_maps = list(self.context.config.base_maps)
        title = re.sub(r"\.json$", "", os.path.basename(path), flags=re.IGNORECASE)
        self._run(_tr("ArcGIS Web マップの読み込み"), lambda task: (open_plan(plan, title, base_maps, auth, None, task.check), None))

    def _run(self, description: str, work) -> None:
        if self.task is not None:
            self.task.cancel()
        self.open_dialog.busy(True)
        self.task = run_task(description, work, self._opened, self._failed)

    def _failed(self, message: str) -> None:
        self.task = None
        self.open_dialog.busy(False)
        self.open_dialog.show_result(_tr("読み込み失敗: {e}").format(e=message), [])

    def _opened(self, result) -> None:
        self.task = None
        opened, ref = result
        self.open_dialog.busy(False)
        lines = []
        if opened.basemap_id:
            self.set_base_map(opened.basemap_id)
            lines.append((_tr("背景地図は「{name}」に設定").format(name=opened.basemap_title), False))
        elif opened.basemap_title:
            self.set_base_map("")
            lines.append((_tr("背景地図「{name}」はレイヤーとしてグループの下に追加").format(name=opened.basemap_title), False))
        add_opened(opened, self.iface.mapCanvas())
        if opened.bookmarks:
            lines.append((_tr("ブックマーク {n} 件を追加（グループ「{title}」）").format(n=len(opened.bookmarks), title=opened.title), False))
        lines += [(f"{title}: {note}", False) for title, note in opened.notes]
        lines += [(_tr("読み込み失敗: {title} — {reason}").format(title=title, reason=reason), True) for title, reason in opened.problems]
        self.opened = (ref, opened.title)
        count = len(opened.layers)
        summary = _tr("Web マップ「{title}」の {n} レイヤーを表示").format(title=opened.title, n=count)
        if opened.problems:
            summary += _tr("（{n} レイヤーは読み込み失敗）").format(n=len(opened.problems))
        self.open_dialog.show_result(summary, lines)
        self.iface.messageBar().pushInfo(TITLE, summary)

    # Saving.

    def build(self) -> SaveResult:
        return project_web_map(list(self.context.config.base_maps), self.iface.mapCanvas())

    def save_portal(self, portal: str, authcfg: Optional[str], result: SaveResult, title: str, tags: str, user: str, folder: str,
                    update: Optional[str]) -> None:
        auth = auth_with_portal(portal, authcfg)
        extent = view_in_degrees(self.iface.mapCanvas())

        def done(answer) -> None:
            item, _ = answer
            self.opened = (WebMapItemRef(portal, item), title)
            self.save_dialog.saved(portal, item, bool(update))
            self.iface.messageBar().pushSuccess(TITLE, _tr("Web マップ「{title}」をポータルに保存").format(title=title))

        run_task(_tr("ArcGIS Web マップの保存"), lambda task: save_to_portal(portal, auth, result, title, tags, user, folder, update, extent),
                 done, self.save_dialog.failed)

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        for dialog in (self.open_dialog, self.save_dialog):
            dialog.close()
            dialog.deleteLater()
