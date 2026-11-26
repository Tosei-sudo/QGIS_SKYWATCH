"""よく使うボタンの強調 (ui/emphasis.py): which buttons are coloured, and that colouring moves nothing."""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QAbstractButton, QDialogButtonBox, QPushButton, QToolButton  # noqa: E402

from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.ui import emphasis  # noqa: E402
from mikage.ui.emphasis import LIGHT, STRONG, emphasize, level_of  # noqa: E402

#: The docks' much used buttons (tsudo 2026-11-26): 強調 every time, 少し強調 often.
EXPECTED = {
    "SatObsImageCatalog": {STRONG: {"検索", "開く"}, LIGHT: {"範囲を描く", "ファイルなしで記録"}},
    "SatObsObservations": {STRONG: {"クリック記録", "記録（Enter）"}, LIGHT: {"◀", "▶", "消失", "不可", "すべて継続", "確認範囲"}},
    "SatObsImagingPlan": {STRONG: {"計算"}, LIGHT: {"地図で指定", "レイヤーとして追加"}},
    "SatObsRequests": {STRONG: {"新規…"}, LIGHT: set()},
    "SatObsPlanFiles": {STRONG: {"計算", "書き出し"}, LIGHT: set()},
    "SatObsSchedule": {STRONG: {"結果ファイルの取り込み…"}, LIGHT: set()},
    "SatObsAiResults": {STRONG: {"正（記録して次へ）", "誤検出（次へ）"}, LIGHT: {"次の要確認へ"}},
}


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    yield
    QgsSettings().remove("mikage")


@pytest.fixture(scope="module")
def plugin():
    from mikage import classFactory

    plugin = classFactory(FakeIface())
    plugin.initGui()
    yield plugin
    plugin.unload()


def marked(widget) -> dict:
    """{level: {texts}} of the buttons under `widget` that are coloured."""
    found = {STRONG: set(), LIGHT: set()}
    for b in widget.findChildren(QAbstractButton):
        if level_of(b):
            found[level_of(b)].add(b.text())
    return found


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_dock_colours_its_much_used_buttons(plugin, name):
    assert marked(plugin.docks[name]) == EXPECTED[name]
    # One or two a dock: more would no longer stand out.
    assert 1 <= len(EXPECTED[name][STRONG]) <= 2


def test_no_other_dock_is_coloured(plugin):
    for name, dock in plugin.docks.items():
        if name not in EXPECTED:
            assert marked(dock) == {STRONG: set(), LIGHT: set()}, name


def test_the_request_forms_save_is_strong():
    from mikage.core.request.model import ImagingRequest
    from mikage.ui.request_form import RequestForm

    form = RequestForm(FakeIface(), ImagingRequest(id=""), lambda _r: None)
    try:
        box = form.findChild(QDialogButtonBox)
        assert level_of(box.button(QDialogButtonBox.StandardButton.Save)) == STRONG
        assert level_of(box.button(QDialogButtonBox.StandardButton.Cancel)) == ""
    finally:
        form.close()


def laid(button) -> "QSize":
    """The size a layout gives the button (its size hint, at least its minimum size)."""
    return button.sizeHint().expandedTo(button.minimumSize())


def same_size(coloured, plain) -> bool:
    """As high, and as wide (a long text in bold: at most a few pixels wider)."""
    c, p = laid(coloured), laid(plain)
    return c.height() == p.height() and p.width() <= c.width() <= p.width() + 4


@pytest.mark.parametrize("level", [STRONG, LIGHT])
@pytest.mark.parametrize("text", ["検索", "結果ファイルの取り込み…", "◀", "正（記録して次へ）"])
def test_a_coloured_button_keeps_its_size(level, text):
    plain = QPushButton(text)
    coloured = emphasize(QPushButton(text), level)
    assert same_size(coloured, plain), (laid(coloured), laid(plain))


def test_a_tool_button_with_a_menu_keeps_its_size_and_arrow():
    def make() -> QToolButton:
        b = QToolButton()
        b.setText("不可")
        b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        return b

    plain, coloured = make(), emphasize(make(), LIGHT)
    assert same_size(coloured, plain), (laid(coloured), laid(plain))
    assert "::menu-button" in coloured.styleSheet()


def test_marked_again_it_is_measured_as_qgis_draws_it():
    b = emphasize(QPushButton("検索"))
    once = laid(b)
    emphasize(b, LIGHT)
    assert laid(b) == once == laid(QPushButton("検索"))
    assert level_of(b) == LIGHT


def test_the_colours_and_the_states():
    sheet = emphasis.style_sheet(STRONG, "QPushButton")
    assert emphasis.style_sheet(STRONG, "QPushButton", pad=5) != sheet
    # Blue fill, white bold text; darker while down (クリック記録中); grey when it cannot be pressed.
    assert "background: #2563eb" in sheet and "color: #ffffff" in sheet and "font-weight: bold" in sheet
    assert "QPushButton:checked { background: #1e3a8a" in sheet
    assert "QPushButton:disabled { background: #f3f4f6; border-color: #d1d5db; color: #9ca3af; }" in sheet
    light = emphasis.style_sheet(LIGHT, "QPushButton")
    assert "background: #ffffff; border: 1px solid #2563eb" in light and "color: #1d4ed8" in light
    with pytest.raises(ValueError):
        emphasize(QPushButton("x"), "red")
