"""「使い方（ヘルプ）」: the bundled page holds together (links, menu entries, keys), opens from the menu, searches, follows its links."""
import os
import re

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtCore import QUrl  # noqa: E402
from qgis.PyQt.QtGui import QImage, QTextDocument  # noqa: E402

from mikage.plugin import MENU_GROUPS, MENU_TOP  # noqa: E402
from mikage.ui import help_dialog, shortcuts  # noqa: E402
from mikage.ui.help_dialog import OPEN, headings, links, read_page, sections  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401

PAGE = read_page()


def test_the_page_stands_alone_and_its_links_lead_somewhere():
    ids = [anchor for _level, anchor, _title in headings(PAGE)]
    assert len(ids) == len(set(ids)) and {"start", "first", "obs", "plan", "reading", "keys", "trouble", "offline"} <= set(ids)
    # Nothing from outside: the page works the same in a closed network.
    assert not re.search(r'(src|href)="(https?:)?//', PAGE) and "<script" not in PAGE and "@import" not in PAGE
    entries = {t for _title, texts in MENU_GROUPS for t in texts if t} | set(MENU_TOP)
    for href in links(PAGE):
        if href.startswith("#"):
            assert href[1:] in ids, href
        else:
            assert href.startswith(OPEN) and href[len(OPEN):] in entries, href
    # Every subsection has words; the admin matters stay in the README.
    texts = sections(PAGE)
    assert [a for level, a, _t in headings(PAGE) if level == 3 and len(texts[a]) < 40] == []
    assert "TBL_" not in PAGE and "config.json" in PAGE


def test_the_pictures_of_the_steps_are_there_and_show_in_the_help_window():
    folder = os.path.join(os.path.dirname(help_dialog.PAGE), "images")
    shown = re.findall(r'<img src="([^"]+)"', PAGE)
    # Every picture named is in the plugin, none left unused, each small and narrow enough for the window.
    assert shown and all(src.startswith("images/") and src.endswith(".png") for src in shown)
    on_disk = sorted(os.listdir(folder))
    assert sorted(src[len("images/"):] for src in set(shown)) == on_disk
    assert all(f'alt="' in tag for tag in re.findall(r"<img [^>]+>", PAGE))
    dialog = help_dialog.HelpDialog()
    doc = dialog.page.document()
    for src in sorted(set(shown)):
        image = QImage(os.path.join(folder, src[len("images/"):]))
        assert 0 < image.width() <= 760 and os.path.getsize(os.path.join(folder, src[len("images/"):])) < 200_000, src
        # The window finds it (the page is given as text, the pictures beside it).
        loaded = doc.resource(QTextDocument.ResourceType.ImageResource, QUrl(src))
        assert loaded is not None and not QImage(loaded).isNull(), (src, loaded)
    # Wide enough for the pictures beside the contents (the page's margins and scroll bar too).
    dialog.show()
    assert dialog.page.viewport().width() >= 760 + 2 * 12
    dialog.close()


def test_the_pictures_stand_without_a_gap_and_the_styles_are_read():
    # A paragraph of a picture alone is p.pic: QTextBrowser puts the body's line height (160 %) on a line holding only
    # a picture too, which left about 60 % of each picture's height blank under it in the help window.
    assert not re.search(r"<p>\s*<img", PAGE)
    assert all(re.fullmatch(r'\s*<img [^>]+>\s*', inner) for inner in re.findall(r'<p class="pic">(.*?)</p>', PAGE, re.S))
    # QTextBrowser stops reading the style sheet at @media: every rule before it, the dark colours last.
    style = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
    rules, media = style.split("@media", 1)
    assert "p.pic" in rules and ".ok" in rules and ".step" in rules
    depth, end = 0, None
    for i, c in enumerate(media):
        depth += c == "{"
        depth -= c == "}"
        if c == "}" and depth == 0:
            end = i
            break
    assert end is not None and media[end + 1:].strip() == ""


def test_every_key_that_works_everywhere_is_in_the_help():
    keys = {key.replace("PgDown", "PageDown").replace("PgUp", "PageUp") for _name, _label, key in shortcuts.GLOBAL}
    text = sections(PAGE)["keys-global"]
    assert [k for k in keys if k not in text] == []


def test_from_the_menu_with_search_and_links(plugin, monkeypatch):  # noqa: F811
    p, _iface, _dock, _ = plugin
    action = next(a for a in p.actions if a.objectName() == "SatObsHelp")
    assert action.text() in p._entries_in_menu_order()
    action.trigger()
    dialog = p.help_dialog
    assert dialog.isVisible() and dialog.tree.topLevelItemCount() == len([h for h in headings(PAGE) if h[0] == 2])
    assert "MIKAGE の使い方" in dialog.plain_text() and "<style" not in dialog.plain_text()
    # Search: every hit marked, the headings without one hidden.
    n = dialog.find("引き継ぎメモ")
    assert n >= 2 and len(dialog.page.extraSelections()) == n and dialog.hits_label.text() == f"1 / {n} 件"
    dialog.find_next()
    assert dialog.hits_label.text() == f"2 / {n} 件"
    top = {dialog.tree.topLevelItem(i).data(0, 256): dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount())}
    assert not top["obs"].isHidden() and top["words"].isHidden()
    assert dialog.find("") == 0 and not top["words"].isHidden() and dialog.page.extraSelections() == []
    # A link to a menu entry brings that dock to the front, and does not close it when open.
    observations = p.docks["SatObsObservations"]
    observations.hide()
    assert dialog.follow(QUrl(OPEN + "観測記録"))
    assert observations.isVisible()
    assert dialog.follow(QUrl(OPEN + "観測記録")) and observations.isVisible()
    assert not dialog.follow(QUrl(OPEN + "ないメニュー"))
    # A dialog entry runs.
    opened = []
    monkeypatch.setattr(p, "open_health_check", lambda: opened.append("health"))
    # One of the administrators' without that role: says which role has it, opens nothing.
    told = []
    monkeypatch.setattr(p.iface.messageBar(), "pushInfo", lambda title, text: told.append(text))
    assert not dialog.follow(QUrl(OPEN + "接続と表の点検…")) and opened == []
    assert told and "担当「管理者」の機能（" in told[0]
    p.role_keeper.choose(["ADMIN"])
    assert dialog.follow(QUrl(OPEN + "接続と表の点検…")) and opened == ["health"]
    # Outside links go to the system's browser; the page's own anchors stay here.
    sent = []
    monkeypatch.setattr(help_dialog.QDesktopServices, "openUrl", lambda url: sent.append(url.toString()))
    assert dialog.follow(QUrl("#keys")) and sent == []
    dialog.follow(QUrl("https://example.com/"))
    assert sent == ["https://example.com/"]
    # Opening again shows the same window, at a section if asked.
    assert p.open_help("trouble") is dialog
