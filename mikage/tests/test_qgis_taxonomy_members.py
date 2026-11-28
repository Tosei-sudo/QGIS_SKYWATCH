"""
Known individuals in QGIS (tsudo 2026-11-27): their rows in 「分類体系の編集」 and their own form (name, class, 別名,
specifications with the class's shown faintly, note), drags and two-press deletes; in 「観測記録」 an other name typed
in 「モノ」 brings the class and a one-click button for the individual's own name, the specifications shown are the
individual's first, and 「このモノの詳細」 says what the classification knows of the thing.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from mikage.core.obs.taxonomy import MemberHit, find  # noqa: E402
from mikage.tests.test_qgis_observations import _flow_button, plugin  # noqa: E402,F401 (the fixture)

SHIPS = [{"name": "船舶", "levels": ["大分類", "種別", "級"], "nameLabel": "艦名", "children": [
    {"name": "護衛艦", "children": [
        {"name": "いずも型", "specs": {"全長": "248", "全幅": "38"},
         "members": ["いずも", {"name": "かが", "specs": {"別名": "DDH-184", "番号": "184"}}]},
        {"name": "ひゅうが型", "specs": {"全長": "197"}, "members": ["ひゅうが"]},
    ]},
]}]
IZUMO = ["船舶", "護衛艦", "いずも型"]


def _editor(p, dock):
    editor = p.docks["SatObsTaxonomy"]
    assert dock.book.set_taxonomy(SHIPS)
    dock.edit_taxonomy(IZUMO)
    return editor


def _row(editor, name):
    def walk(item):
        for i in range(item.childCount()):
            c = item.child(i)
            if c.data(0, 0x0100 + 2) == name:
                return c
            found = walk(c)
            if found is not None:
                return found
        return None

    return walk(editor.tree.invisibleRootItem())


def test_individuals_are_rows_of_the_tree_with_a_form_of_their_own(plugin):
    p, iface, dock, layers, _ = plugin
    editor = _editor(p, dock)
    row = _row(editor, "かが")
    assert row is not None and row.parent().text(0) == "いずも型" and row.text(1) == "艦名（個体）"
    assert editor.members_line.text() == "いずも・かが"
    editor.tree.setCurrentItem(row)
    assert editor.member == "かが" and editor.member_form.isVisible() and not editor.form.isVisible()
    assert editor.member_form.title() == "船舶 > 護衛艦 > いずも型 ▸ かが（艦名）"
    assert editor.member_alias_input.text() == "DDH-184"
    # The class's values show faintly; the individual's own are in the fields.
    assert editor._member_spec_inputs["全長"].placeholderText() == "248 m（いずも型）"
    assert editor._member_spec_inputs["番号"].text() == "184"
    # 番号 is for individuals only: not on the class's form.
    editor.select(IZUMO)
    assert "番号" not in editor._spec_inputs and "全長" in editor._spec_inputs
    # Changed in its form: other names, a specification of its own, a note.
    editor.select(IZUMO, "かが")
    editor.member_alias_input.setText("DDH-184、Kaga")
    editor._member_spec_inputs["全長"].setText("250")
    editor.member_note_input.setPlainText("改修中")
    editor.apply_member()
    kaga = find(dock.book.taxonomy(), IZUMO).member("かが")
    assert kaga.specs == {"番号": "184", "全長": "250", "別名": "DDH-184、Kaga"} and kaga.note == "改修中"
    # Found by an other name in the search.
    editor.query.setText("kaga")
    assert _row(editor, "かが") is not None and _row(editor, "いずも") is None
    editor.query.clear()
    # Renamed where it stands (F2).
    _row(editor, "いずも").setText(0, "出雲")
    assert find(dock.book.taxonomy(), IZUMO).members == ["出雲", "かが"] and editor.member == "出雲"
    # Dragged onto another class: moved there.
    editor._dropped("船舶 > 護衛艦 > いずも型", "船舶 > 護衛艦 > ひゅうが型", "inside", "出雲", None)
    assert find(dock.book.taxonomy(), ["船舶", "護衛艦", "ひゅうが型"]).members == ["ひゅうが", "出雲"]
    # Before another individual.
    editor._dropped("船舶 > 護衛艦 > ひゅうが型", "船舶 > 護衛艦 > ひゅうが型", "before", "出雲", "ひゅうが")
    assert find(dock.book.taxonomy(), ["船舶", "護衛艦", "ひゅうが型"]).members == ["出雲", "ひゅうが"]
    # Added from the class (several at once), deleted in two presses.
    editor.add_members(IZUMO, ["くまの", "もがみ"])
    assert find(dock.book.taxonomy(), IZUMO).members == ["かが", "くまの", "もがみ"] and editor.member == "くまの"
    editor._delete_key()
    assert "もう一度で個体「くまの」を削除" in editor.status.text()
    editor._delete_key()
    assert find(dock.book.taxonomy(), IZUMO).members == ["かが", "もがみ"] and editor.member is None


def test_an_other_name_typed_in_the_form_brings_the_class_and_offers_the_individuals_own_name(plugin):
    p, iface, dock, layers, _ = plugin
    _editor(p, dock)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.kind_input.setText("")
    dock.object_input.setText("ddh-184")
    dock._object_typed()
    assert dock.kind_input.text() == "船舶 > 護衛艦 > いずも型"
    assert dock.member_hint.isVisibleTo(dock.form) and dock.member_hint_label.text() == "「ddh-184」は「かが」の別名（いずも型）"
    # The individual's own specifications first, then the class's.
    assert dock.specs_label.text().startswith("別名 DDH-184・番号 184・") and "全長 248 m" in dock.specs_label.text()
    dock.member_hint_button.click()
    assert dock.object_input.text() == "かが" and not dock.member_hint.isVisibleTo(dock.form)
    # 「かが（DDH-184）」 chosen from the list: its own name goes in.
    assert "かが（DDH-184）" in dock.objects_model.stringList()
    dock.object_input.setText("かが（DDH-184）")
    dock._object_typed()
    assert dock.object_input.text() == "かが"
    # 備考 offers the individuals' names and other names (without a glossary too).
    dock.note_input.setText("DDH-1")
    dock.note_input.setCursorPosition(5)
    assert [s.term.text for s in dock.note_assist.suggest()] == ["DDH-184"]
    dock.note_input.clear()
    dock.submit()
    [record] = dock.book.living()
    assert record.object_name == "かが" and record.kind == "船舶 > 護衛艦 > いずも型"
    assert dock.book.find_member("DDH-184") == MemberHit(IZUMO, "かが", "DDH-184")
    # 「このモノの詳細」: what the classification knows of the thing, and the way to it.
    dock.render()
    APP.processEvents()
    _flow_button(dock.table, 0).click()
    dock.flow_more.set_open(True)
    APP.processEvents()
    assert dock.flow_member.isVisible()
    assert dock.flow_member_label.text() == "かが（船舶 > 護衛艦 > いずも型）・別名 DDH-184・番号 184"
    dock.flow_member_button.click()
    editor = p.docks["SatObsTaxonomy"]
    assert editor.isVisible() and editor.member == "かが" and editor.member_form.isVisible()
