"""
「ツールバーのボタン」 (tsudo, 2026-11-08): which of the plugin's entries the
MIKAGE toolbar shows, by role (担当, chosen on 「担当と個人設定」), and each
person's own choice on top.

* Each role of the examples of 「ドックのレイアウト」 has its own set (ROLE_SETS);
  no role, or a layout saved by the person under another name, gets DEFAULT
  (the buttons the toolbar always had).
* 「ツールバーのボタン…」 picks the buttons and their order for the role in use;
  kept per role in QgsSettings (`toolbarButtons`, JSON {role: [entry texts]}),
  so going back to a role brings back its own buttons. 「担当の標準に戻す」
  forgets the person's choice for that role.

Entries are named by their menu text (as MENU_GROUPS does); a text no longer
there (renamed in a later version) is left out.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

KEY = "mikage/toolbarButtons"

#: Without a role (and for a layout of the person's own): what the toolbar showed before roles.
DEFAULT = [
    "画像カタログ", "撮像計画", "撮像依頼", "撮像計画ファイル出力", "衛星パス", "雲量マップ", "観測記録", "分類体系の編集",
    "通知", "新着画像（大きな画面）", "画像補正", "影から高さ", "スペクトル", "移動体（AIS・ADS-B）", "ダッシュボード", "AI結果", "イベント", "データ分析",
    "移動目標撮像計画…", "背景地図", "スワイプ比較", "位置ずらし", "使い方（ヘルプ）…", "ツールバーのボタン…",
]

#: The roles of the examples (ui/dock_layouts.PRESETS): what each needs at hand.
ROLE_SETS = {
    "撮像計画の担当": [
        "撮像計画", "衛星パス", "移動目標撮像計画…", "雲量マップ", "撮像依頼", "撮像計画ファイル出力", "撮像予定",
        "画像カタログ", "イベント", "背景地図", "使い方（ヘルプ）…", "ツールバーのボタン…",
    ],
    "観測記録の担当": [
        "画像カタログ", "通知", "新着画像（大きな画面）", "画像補正", "観測記録", "AI結果", "分類体系の編集", "イベント", "通報文の作成…", "分担…",
        "引き継ぎメモ…", "背景地図", "スワイプ比較", "位置ずらし", "使い方（ヘルプ）…", "ツールバーのボタン…",
    ],
    "画像判読": [
        "画像カタログ", "通知", "新着画像（大きな画面）", "画像補正", "影から高さ", "スペクトル", "観測記録", "移動体（AIS・ADS-B）", "変化抽出（2 時期）…",
        "自動位置合わせ…", "背景地図", "スワイプ比較", "位置ずらし", "使い方（ヘルプ）…", "ツールバーのボタン…",
    ],
}


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


# ---- what is kept --------------------------------------------------------

def _own_all() -> dict:
    raw = QgsSettings().value(KEY, "")
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def own(role: str) -> Optional[list[str]]:
    """The person's own buttons for `role` (None: the role's standard)."""
    value = _own_all().get(role)
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else None


def set_own(role: str, texts: Optional[list[str]]) -> None:
    """Keeps the person's buttons for `role` (None: back to the role's standard)."""
    data = _own_all()
    if texts is None:
        data.pop(role, None)
    else:
        data[role] = list(texts)
    QgsSettings().setValue(KEY, json.dumps(data, ensure_ascii=False))


def standard(role: str) -> list[str]:
    """The role's buttons as the plugin comes (DEFAULT for no role or one of the person's own layouts)."""
    return list(ROLE_SETS.get(role, DEFAULT))


def chosen(role: str) -> list[str]:
    """The buttons shown for `role`: the person's own, else the role's standard."""
    texts = own(role)
    return standard(role) if texts is None else texts


# ---- the toolbar ---------------------------------------------------------

class ToolbarButtons:
    """
    Fills `toolbar` with the entries (text → action, in menu order) chosen for
    the role `role()` gives. `apply()` again after the role or the choice changed.
    """

    def __init__(self, toolbar: QToolBar, entries: dict, role: Callable[[], str]):
        self.toolbar = toolbar
        self.entries = dict(entries)
        self.role = role

    def shown(self) -> list[str]:
        return [t for t in chosen(self.role()) if t in self.entries]

    def apply(self) -> None:
        self.toolbar.clear()
        for text in self.shown():
            action = self.entries[text]
            self.toolbar.addAction(action)
            button = self.toolbar.widgetForAction(action)
            # 「背景地図」 and 「ドックのレイアウト」: the list right away on a click.
            if action.menu() is not None and isinstance(button, QToolButton):
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)


class ToolbarButtonsDialog(QDialog):
    """「ツールバーのボタン」: the entries to show for the role in use (ticked, in toolbar order; drag or 上へ・下へ to reorder)."""

    def __init__(self, buttons: ToolbarButtons, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SatObsToolbarButtons")
        self.buttons = buttons
        self.role_name = buttons.role()
        self.setWindowTitle(tr("ツールバーのボタン"))
        self.setMinimumSize(420, 560)
        layout = QVBoxLayout(self)
        who = tr("担当「{0}」").format(self.role_name) if self.role_name else tr("担当なし")
        intro = QLabel(tr("{0}のツールバーに出すボタンを選びます（チェックしたものを上から順に）。"
                          "担当はメニュー「担当と個人設定…」で変えられ、担当ごとに覚えます。").format(who))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        layout.addWidget(self.list, 1)
        self.fill(chosen(self.role_name))
        row = QHBoxLayout()
        for text, step in ((tr("上へ"), -1), (tr("下へ"), 1)):
            move = QPushButton(text)
            move.clicked.connect(lambda _=False, s=step: self.move(s))
            row.addWidget(move)
        row.addStretch(1)
        self.reset_button = QPushButton(tr("担当の標準に戻す"))
        self.reset_button.setToolTip(tr("この担当で選んだボタンを忘れ、プラグインの標準に戻す"))
        self.reset_button.clicked.connect(lambda: self.fill(standard(self.role_name)))
        row.addWidget(self.reset_button)
        layout.addLayout(row)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def fill(self, texts: list[str]) -> None:
        """Ticked first (in `texts` order), then the rest in menu order."""
        self.list.clear()
        entries = self.buttons.entries
        ticked = [t for t in texts if t in entries]
        for text in ticked + [t for t in entries if t not in ticked]:
            item = QListWidgetItem(entries[text].icon(), text)
            item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                          & ~Qt.ItemFlag.ItemIsDropEnabled)
            item.setCheckState(Qt.CheckState.Checked if text in ticked else Qt.CheckState.Unchecked)
            self.list.addItem(item)

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        to = row + step
        if row < 0 or not 0 <= to < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(to, item)
        self.list.setCurrentRow(to)

    def picked(self) -> list[str]:
        items = [self.list.item(i) for i in range(self.list.count())]
        return [i.text() for i in items if i.checkState() == Qt.CheckState.Checked]

    def accept(self) -> None:
        texts = self.picked()
        # The role's standard as it is: nothing of the person's own kept (a later version's standard then applies).
        same = texts == [t for t in standard(self.role_name) if t in self.buttons.entries]
        set_own(self.role_name, None if same else texts)
        self.buttons.apply()
        super().accept()
