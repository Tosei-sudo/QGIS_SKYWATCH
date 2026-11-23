"""分類体系の取り込み（表から） without the screen: columns guessed, rows read, the plan against the classification, the table written."""
import csv
import io

import pytest

from mikage.core.obs import taxonomy_import as ti
from mikage.core.obs.record_import import read_csv
from mikage.core.obs.taxonomy import DEFAULT_SPEC_FIELDS, SpecField, Taxon, kind_of, normalize

COLUMNS = ["大分類", "種別", "級・型式", "個体名", "別名", "全長（m）"]
TABLE = [
    ["船舶", "貨物船", "ｺﾝﾃﾅ船", "", "", ""],
    ["", "", "タンカー", "", "", ""],
    ["", "", "ばら積み船", "", "", ""],
    ["", "支援船", "〇〇級", "△△", "", ""],
    ["", "", "", "◇◇", "", ""],
    ["", "", "補給艦", "", "AOE", "221"],
    ["", "警備艇", "", "", "", ""],
    ["航空機", "輸送機", "C-2", "", "", "43.9"],
    ["", "哨戒機", "P-1", "", "", "38"],
    ["車両", "", "自走砲", "", "", ""],
    ["", "装輪車", "", "", "", ""],
    ["建物", "格納庫", "", "", "", ""],
]
#: As the mock's sample (scripts/mock_esri/sample.py TAXONOMY_ROWS).
EXISTING = normalize([
    Taxon(["船舶"], levels=["大分類", "種別", "級"], name_label="艦名", activities=["入港", "出港"]),
    Taxon(["船舶", "貨物船"]), Taxon(["船舶", "貨物船", "コンテナ船"]), Taxon(["船舶", "支援船"]),
    Taxon(["船舶", "支援船", "〇〇級"], members=["△△", "□□"]),
    Taxon(["航空機"], levels=["大分類", "種別", "型式"], name_label="機番"), Taxon(["航空機", "輸送機"]),
    Taxon(["車両"]), Taxon(["建物"]), Taxon(["その他"]),
])


def table_rows(rows=TABLE, columns=COLUMNS):
    return [ti.TableRow(n, dict(zip(columns, r))) for n, r in enumerate(rows, start=2)]


def plan(mode=ti.ADD, used=(), rows=TABLE, columns=COLUMNS, existing=EXISTING, fields=DEFAULT_SPEC_FIELDS, uses=None, **kw):
    uses = uses or ti.guess_uses(columns, existing, fields)
    read = ti.read_rows(table_rows(rows, columns), columns, uses, kw.pop("fill_down", True))
    return ti.plan_import(existing, fields, read, columns, uses, mode, used_kinds=used, **kw)


def by_kind(p):
    return {c.kind: c for c in p.classes}


def test_columns_are_guessed_from_level_names_individuals_and_specification_fields():
    uses = ti.guess_uses(COLUMNS, EXISTING, DEFAULT_SPEC_FIELDS)
    assert [u.label for u in uses] == ["階層 1", "階層 2", "階層 3", "既知の個体", "諸元: 別名", "諸元: 全長"]
    # The usual level names, the observation export's 分類1…, and the classification's names for individuals.
    assert [u.label for u in ti.guess_uses(["中分類", "小分類", "艦名", "メモ"], EXISTING)] == ["階層 1", "階層 2", "既知の個体", "使わない"]
    assert [u.label for u in ti.guess_uses(["分類1", "分類2", "第 3 階層", "LEVEL_4", "活動", "参考写真"])] == [
        "階層 1", "階層 2", "階層 3", "階層 4", "活動", "参考写真（URL）"]
    # No level found: the columns on the left are.
    assert [u.label for u in ti.guess_uses(["A", "B", "別名"], (), DEFAULT_SPEC_FIELDS)] == ["階層 1", "階層 2", "諸元: 別名"]
    # An item twice: the second is not used.
    assert [u.label for u in ti.guess_uses(["大分類", "個体名", "艦名"], EXISTING)] == ["階層 1", "既知の個体", "使わない"]


def test_uses_are_checked_levels_from_one_and_items_once():
    L, M = ti.LEVEL, ti.Use(ti.MEMBERS)
    assert ti.check_uses([ti.Use(L, 1), ti.Use(L, 2), M]) is None
    assert "階層 1" in ti.check_uses([M, ti.SKIPPED])
    assert "1 から順に" in ti.check_uses([ti.Use(L, 1), ti.Use(L, 3)])
    assert "1 から順に" in ti.check_uses([ti.Use(L, 1), ti.Use(L, 1)])
    assert "同じ項目" in ti.check_uses([ti.Use(L, 1), M, M])


def test_rows_take_the_blanks_on_the_left_from_the_row_above():
    uses = ti.guess_uses(COLUMNS, EXISTING, DEFAULT_SPEC_FIELDS)
    rows = {r.number: r for r in ti.read_rows(table_rows(), COLUMNS, uses)}
    assert rows[3].path == ["船舶", "貨物船", "タンカー"]
    # All level cells blank: the class above (one row per individual).
    assert rows[6].path == ["船舶", "支援船", "〇〇級"] and rows[6].values[ti.Use(ti.MEMBERS)] == "◇◇"
    # Blank on the right: the path ends there.
    assert rows[8].path == ["船舶", "警備艇"]
    # A blank between two names: left out; the next row still takes 車両 from it.
    assert rows[11].path is None and rows[11].error == "途中の階層「種別」が空欄（車両 › （空欄） › 自走砲）"
    assert rows[12].path == ["車両", "装輪車"]


def test_without_taking_from_above_a_blank_on_the_left_leaves_the_row_out():
    uses = ti.guess_uses(COLUMNS, EXISTING, DEFAULT_SPEC_FIELDS)
    rows = {r.number: r for r in ti.read_rows(table_rows(), COLUMNS, uses, fill_down=False)}
    assert rows[2].path == ["船舶", "貨物船", "ｺﾝﾃﾅ船"]
    assert rows[3].path is None and rows[3].error == "「大分類」が空欄"
    assert rows[6].error == "分類の列がすべて空欄"
    # A first row blank on the left has nothing above even when taking from above.
    first = ti.read_rows(table_rows([["", "支援船", ""]]), COLUMNS, uses)
    assert first[0].error == "「大分類」が空欄（上の行にもない）"


def test_names_are_trimmed_numbers_lose_their_point_and_separators_become_spaces():
    columns = ["大分類", "種別", "全長"]
    uses = ti.guess_uses(columns, (), DEFAULT_SPEC_FIELDS)
    rows = ti.read_rows([ti.TableRow(2, {"大分類": "　船舶 ", "種別": "A > B", "全長": 120.0}), ti.TableRow(3, {"大分類": None, "種別": "", "全長": None})],
                        columns, uses)
    assert len(rows) == 1 and rows[0].path == ["船舶", "A B"] and rows[0].values[ti.Use(ti.SPEC, "全長")] == "120"


def test_add_and_update_adds_classes_and_values_and_keeps_the_rest():
    p = plan()
    c = by_kind(p)
    assert p.summary() == "新規 9・変更 3・そのまま 7・取り込まない行 1"
    assert p.errors == [(11, "途中の階層「種別」が空欄（車両 › （空欄） › 自走砲）")]
    # ｺﾝﾃﾅ船 is コンテナ船 (its name kept).
    assert c["船舶 > 貨物船 > コンテナ船"].status == ti.SAME
    assert c["船舶 > 貨物船 > コンテナ船"].notes == ["行 2 の「ｺﾝﾃﾅ船」を「コンテナ船」とみなす（全角・半角などの違い）"]
    assert "船舶 > 貨物船 > ｺﾝﾃﾅ船" not in c
    assert c["船舶 > 支援船 > 〇〇級"].status == ti.CHANGED
    assert c["船舶 > 支援船 > 〇〇級"].text() == "既知の個体に ◇◇ を追加（行 5, 6）"
    assert c["船舶 > 支援船 > 補給艦"].status == ti.NEW
    assert c["船舶 > 支援船 > 補給艦"].text() == "別名 AOE、全長 221 m（行 7）"
    assert c["その他"].status == ti.SAME and c["その他"].text() == "表になし（そのまま残す）"
    # Level names only for the 大分類 that had none.
    assert c["車両"].status == ti.CHANGED and c["車両"].changes == ["階層名を「大分類 › 種別 › 級・型式」に"]
    after = {kind_of(t.path): t for t in p.taxa}
    assert after["車両"].levels == ["大分類", "種別", "級・型式"] and after["船舶"].levels == ["大分類", "種別", "級"]
    assert after["船舶 > 支援船 > 〇〇級"].members == ["△△", "□□", "◇◇"]
    assert after["船舶 > 支援船 > 補給艦"].specs == {"別名": "AOE", "全長": "221"}
    # New classes after their siblings, in the table's order; the input not changed.
    kinds = [kind_of(t.path) for t in p.taxa]
    assert kinds.index("船舶 > 貨物船 > コンテナ船") < kinds.index("船舶 > 貨物船 > タンカー") < kinds.index("船舶 > 貨物船 > ばら積み船")
    assert kinds[-1] == "その他" or kinds.index("その他") > kinds.index("建物")
    assert EXISTING[4].members == ["△△", "□□"]
    assert p.changed and p.spec_fields is None


def test_replacing_removes_what_the_table_lacks_but_keeps_classes_records_use():
    p = plan(ti.REPLACE)
    c = by_kind(p)
    assert c["その他"].status == ti.REMOVED and "その他" not in {kind_of(t.path) for t in p.taxa}
    assert c["船舶 > 支援船 > 〇〇級"].changes == ["既知の個体に ◇◇ を追加", "既知の個体から □□ を外す"]
    assert p.summary() == "新規 9・変更 3・削除 1・そのまま 6・取り込まない行 1"
    assert p.removed_tops() == ["その他"]
    # A class records use stays (and the classes above it).
    existing = normalize([*EXISTING, Taxon(["その他", "ブイ"])])
    p = plan(ti.REPLACE, used=["その他 > ブイ"], existing=existing)
    c = by_kind(p)
    assert c["その他"].status == ti.KEPT and c["その他 > ブイ"].status == ti.KEPT and not p.removed()
    # Without the record, the branch goes; only its top is asked about.
    p = plan(ti.REPLACE, existing=existing)
    assert [x.kind for x in p.removed()] == ["その他", "その他 > ブイ"] and p.removed_tops() == ["その他"]


def test_replacing_writes_a_columns_items_as_the_table_says():
    existing = normalize([Taxon(["船舶"], activities=["停泊"], specs={"別名": "SHIP", "英名": "Ship"}), Taxon(["船舶", "A"], photos=["local:1", "https://x/a.png"])])
    columns = ["大分類", "種別", "別名", "活動", "参考写真"]
    rows = [["船舶", "", "", "", ""], ["船舶", "A", "", "", "https://x/b.png"]]
    p = plan(ti.REPLACE, rows=rows, columns=columns, existing=existing, level_names=False)
    after = {kind_of(t.path): t for t in p.taxa}
    # A blank clears the column's item; 英名 (no column) stays; local photos are not the table's to take away.
    assert after["船舶"].specs == {"英名": "Ship"} and after["船舶"].activities is None
    assert after["船舶 > A"].photos == ["local:1", "https://x/b.png"]
    assert by_kind(p)["船舶"].changes == ["別名を消す", "活動を消す"]
    # 追加・更新 leaves them.
    p = plan(ti.ADD, rows=rows, columns=columns, existing=existing, level_names=False)
    after = {kind_of(t.path): t for t in p.taxa}
    assert after["船舶"].specs == {"別名": "SHIP", "英名": "Ship"} and after["船舶"].activities == ["停泊"]
    assert after["船舶 > A"].photos == ["local:1", "https://x/a.png", "https://x/b.png"]


def test_values_written_differently_in_two_rows_of_a_class_take_the_first():
    rows = [["船舶", "A", "X1"], ["", "A", "X2"]]
    p = plan(rows=rows, columns=["大分類", "種別", "別名"], existing=[])
    c = by_kind(p)["船舶 > A"]
    assert c.changes == ["別名 X1"] and c.notes == ["行 3 の別名「X2」は使わない（行 2 の「X1」）"]


def test_a_new_specification_field_is_added_with_its_unit_and_type():
    columns = ["大分類", "排水量（t）", "備考欄"]
    uses = [ti.Use(ti.LEVEL, 1), ti.Use(ti.NEW_SPEC), ti.Use(ti.NEW_SPEC)]
    p = plan(rows=[["船舶", "1,500", "主力"]], columns=columns, existing=[], fields=[SpecField("別名", "list")], uses=uses)
    assert [f.to_json() for f in p.spec_fields] == [{"name": "別名", "type": "list"}, {"name": "排水量", "type": "number", "unit": "t"},
                                                     {"name": "備考欄", "type": "text"}]
    assert p.taxa[0].specs == {"排水量": "1,500", "備考欄": "主力"}


def test_too_long_values_are_not_taken():
    many = "、".join(f"個体{i:04d}" for i in range(1200))
    p = plan(rows=[["船舶", many]], columns=["大分類", "個体名"], existing=[])
    c = by_kind(p)["船舶"]
    assert c.notes == ["既知の個体が長すぎて保存できないため取り込まない（8000 文字まで）"] and not p.taxa[0].members
    p = plan(rows=[["船舶", "あ" * 1001]], columns=["大分類", "種別"], existing=[])
    assert p.errors == [(2, "分類が長すぎる（1000 文字まで）")]


def test_level_names_that_only_number_the_levels_are_not_taken():
    p = plan(rows=[["船舶", "A"]], columns=["分類1", "分類2"], existing=[])
    assert p.taxa[0].levels is None
    p = plan(rows=[["船舶", "A"]], columns=["大分類", "種別"], existing=[], level_names=False)
    assert p.taxa[0].levels is None


def test_nothing_to_do_when_the_table_is_the_classification():
    head, rows = ti.taxonomy_table(EXISTING, DEFAULT_SPEC_FIELDS)
    p = plan(rows=rows, columns=head)
    assert not p.changed and p.summary() == "新規 0・変更 0・そのまま 10・取り込まない行 0"


def test_the_table_written_reads_back_as_the_same_classification(tmp_path):
    taxa = plan().taxa
    fields = DEFAULT_SPEC_FIELDS
    text = ti.taxonomy_csv(taxa, fields)
    assert text.startswith("\ufeff大分類,種別,級・型式,階層名,個体名の呼称,既知の個体,活動,参考写真,別名,英名,開発国,運用国,全長（m）")
    path = tmp_path / "t.csv"
    path.write_text(text, encoding="utf-8", newline="")
    columns, rows = read_csv(str(path))
    uses = ti.guess_uses(columns, [], fields)
    assert [u.label for u in uses[:8]] == ["階層 1", "階層 2", "階層 3", "階層名（この枝）", "個体名の呼称", "既知の個体", "活動", "参考写真（URL）"]
    read = ti.read_rows([ti.TableRow(r.number, r.values) for r in rows], columns, uses)
    back = ti.plan_import([], fields, read, columns, uses, ti.REPLACE)
    assert [t.to_json() for t in back.taxa] == [t.to_json() for t in normalize(taxa)]
    # Into the classification it came from: nothing to do.
    for mode in (ti.ADD, ti.REPLACE):
        assert not ti.plan_import(taxa, fields, read, columns, uses, mode).changed


def test_export_level_names_join_the_branches_names_and_never_repeat_one():
    assert ti.export_level_names(EXISTING, 3) == ["大分類", "種別", "級・型式"]
    assert ti.export_level_names([*EXISTING, Taxon(["X"], levels=["大分類", "種別", "級・型式"])], 3) == ["大分類", "種別", "級・型式"]
    assert ti.export_level_names([Taxon(["A"], levels=["X", "X"])], 3) == ["X", "階層 2", "階層 3"]


def test_the_csv_is_one_row_per_class_with_every_level_written():
    text = ti.taxonomy_csv(EXISTING, [SpecField("全長", "number", "m")])
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0] == ["大分類", "種別", "級・型式", "階層名", "個体名の呼称", "既知の個体", "活動", "参考写真", "全長（m）"]
    assert rows[1] == ["船舶", "", "", "大分類、種別、級", "艦名", "", "入港、出港", "", ""]
    assert ["船舶", "支援船", "〇〇級", "", "", "△△、□□", "", "", ""] in rows
    assert len(rows) == 1 + len(EXISTING)


@pytest.mark.parametrize("value,text", [(None, ""), (12.0, "12"), (43.9, "43.9"), (7, "7"), ("  x ", "x")])
def test_cells_as_text(value, text):
    assert ti.cell_text(value) == text


@pytest.fixture
def book(tmp_path):
    from mikage.core.gpkg import open_local
    from mikage.core.obs.book import ObservationBook, Prefs
    from mikage.core.obs.model import ObservationVocabulary
    from mikage.core.obs.stores import LocalAreaStore, LocalObservationStore, LocalTaxonomyStore

    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=Prefs(), say=said.append)
    b.said = said
    b.reload()
    assert b.set_taxonomy(list(EXISTING))
    yield b
    db.close()


def request(mode=ti.ADD, rows=TABLE, columns=COLUMNS):
    return ti.ImportRequest(list(columns), table_rows(rows, columns), ti.guess_uses(columns, EXISTING, DEFAULT_SPEC_FIELDS), mode, source="分類.csv")


def test_the_book_tries_then_takes_the_table_in_keeping_a_backup(book):
    tried = book.plan_taxonomy_import(request())
    assert tried.summary().startswith("新規 ") and book.kept_taxonomy() == normalize(EXISTING)
    kept: list[str] = []
    saved = book.import_taxonomy(request(), tried.signature(), lambda text: kept.append(text) or "/tmp/控え.json")
    assert saved is not None and saved.signature() == tried.signature()
    assert '"船舶"' in kept[0] and "取り込む前の分類体系の控え: /tmp/控え.json" in book.said[-1]
    assert "船舶 > 支援船 > 補給艦" in [kind_of(t.path) for t in book.kept_taxonomy()]
    assert book.spec_fields == saved.spec_fields or saved.spec_fields is None
    # Again: nothing left to change, nothing saved (no backup made).
    again = book.import_taxonomy(request(), None, lambda text: pytest.fail("no backup when nothing changes"))
    assert again is not None and not again.changed and "変更はない" in book.said[-1]


def test_the_book_takes_nothing_when_the_classification_changed_since_the_dry_run_or_the_backup_fails(book):
    tried = book.plan_taxonomy_import(request())
    assert book.add_class(["船舶", "貨物船", "タンカー"])
    assert book.import_taxonomy(request(), tried.signature()) is None and "もう一度試行" in book.said[-1]

    def broken(text):
        raise OSError("書き込めない")

    before = book.kept_taxonomy()
    assert book.import_taxonomy(request(), None, broken) is None and "控えを保存できなかった" in book.said[-1]
    assert book.kept_taxonomy() == before


def test_the_book_replacing_keeps_the_classes_records_use(book):
    from mikage.core.obs.book import FormValues, ObservedImage

    book.set_open_images([ObservedImage("i", "i.tif", 1, None, "L")], "L")
    assert book.submit(139.7, 35.6, FormValues(object="", kind="その他")) is not None
    p = book.plan_taxonomy_import(request(ti.REPLACE))
    assert by_kind(p)["その他"].status == ti.KEPT
    assert book.import_taxonomy(request(ti.REPLACE), p.signature()) is not None
    assert "その他" in [kind_of(t.path) for t in book.kept_taxonomy()]
    with pytest.raises(ValueError):
        book.plan_taxonomy_import(ti.ImportRequest(["メモ"], [], [ti.SKIPPED]))


def test_scripts_try_and_take_a_table_in_with_uses_written_as_shown(book):
    from mikage.core.obs.api import ObservationApi

    api = ObservationApi(lambda: book, lambda _d, work: work(book))
    rows = [dict(zip(COLUMNS, r)) for r in TABLE]
    tried = api.import_taxonomy(COLUMNS, rows)
    assert not tried["saved"] and tried["uses"][:4] == ["階層 1", "階層 2", "階層 3", "既知の個体"]
    assert tried["errors"] == [{"row": 11, "reason": "途中の階層「種別」が空欄（車両 › （空欄） › 自走砲）"}]
    assert book.kept_taxonomy() == normalize(EXISTING)
    kept = []
    done = api.import_taxonomy(COLUMNS, TABLE, uses={"大分類": "level1", "種別": "階層 2", "級・型式": "階層 3"}, save=True,
                               backup=lambda text: kept.append(text) or "控え.json")
    assert done["saved"] and kept and "表から取り込みました" in done["message"]
    assert "船舶 > 支援船 > 補給艦" in [kind_of(t.path) for t in book.kept_taxonomy()]
    with pytest.raises(ValueError):
        api.import_taxonomy(COLUMNS, TABLE, uses=["階層 1"])
    with pytest.raises(ValueError):
        api.import_taxonomy(COLUMNS, TABLE, mode="merge")
