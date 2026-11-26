"""
Known individuals (既知の個体) with what they say of themselves: other names (別名), their own specifications,
photos and a note; kept in a table of individuals (TBL_TAXONOMY_MEMBER) or the local GeoPackage.
"""
import json

import pytest

from mikage.core.esri import EsriClient
from mikage.core.gpkg import open_local
from mikage.core.obs.book import ObservationBook, ObservedImage, Prefs
from mikage.core.obs.model import ObservationVocabulary
from mikage.core.obs.stores import EsriTaxonomyStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from mikage.core.obs.taxonomy import (
    DEFAULT_SPEC_FIELDS, Member, MemberHit, SpecField, Taxon, add_members, class_by_name, find, find_member, flatten, member_names,
    member_photos_at, member_specs_at, members_of, move_member, nest, remove_member, rename_member, spec_fields_of, taxa_from_json,
    taxonomy_of, taxonomy_table_of, update_member,
)
from mikage.tests.fake_esri import FakeEsri, FakeLayer

SHIPS = [{"name": "船舶", "children": [
    {"name": "いずも型", "specs": {"全長": "248", "別名": "DDH"}, "photos": ["https://x/izumo-class.jpg"],
     "members": ["いずも", {"name": "かが", "specs": {"別名": "DDH-184、Kaga", "番号": "184", "全長": "250"}, "note": "改修中"}]},
    {"name": "ひゅうが型", "members": ["ひゅうが"]},
]}]
IZUMO = ["船舶", "いずも型"]


def ships():
    return taxonomy_of(SHIPS, [])


class TestMembers:
    def test_members_are_names_or_objects_and_a_repeated_name_comes_once(self):
        names, info = members_of(["いずも", {"name": "かが", "specs": {"番号": 184}, "photos": ["a.jpg"], "note": " x ", "id": "m1"},
                                  "かが", {"name": " "}, 3, {"name": "ひゅうが"}])
        assert names == ["いずも", "かが", "ひゅうが"]
        assert info == {"かが": Member("かが", {"番号": "184"}, ["a.jpg"], "x", "m1")}
        assert members_of("いずも") == (None, None)

    def test_the_settings_file_and_the_viewers_json_keep_what_individuals_say(self):
        taxa = ships()
        izumo = find(taxa, IZUMO)
        assert izumo.members == ["いずも", "かが"]
        assert izumo.member("かが").specs == {"別名": "DDH-184、Kaga", "番号": "184", "全長": "250"}
        assert izumo.member("いずも") == Member("いずも")
        # Written back: names alone for those that say nothing.
        assert nest(taxa)[0]["children"][0]["members"] == [
            "いずも", {"name": "かが", "specs": {"別名": "DDH-184、Kaga", "番号": "184", "全長": "250"}, "note": "改修中"}]
        assert flatten(nest(taxa)) == taxa
        assert taxa_from_json([t.to_json() for t in taxa]) == taxa

    def test_an_individual_is_found_by_its_name_any_width_or_case_or_by_another_name(self):
        taxa = ships()
        assert find_member(taxa, "かが") == MemberHit(IZUMO, "かが")
        assert find_member(taxa, "ＤＤＨ－１８４") == MemberHit(IZUMO, "かが", "DDH-184")
        assert find_member(taxa, "kaga") == MemberHit(IZUMO, "かが", "Kaga")
        assert find_member(taxa, "ひゅうが") == MemberHit(["船舶", "ひゅうが型"], "ひゅうが")
        assert find_member(taxa, "いせ") is None
        assert member_names(find(taxa, IZUMO), "かが") == ["DDH-184", "Kaga"]
        # A class's own other name still finds the class first; an individual's finds its class.
        assert class_by_name(taxa, "DDH") == IZUMO
        assert class_by_name(taxa, "DDH-184") == IZUMO

    def test_an_individuals_specifications_come_before_its_classs(self):
        taxa = ships()
        specs = member_specs_at(taxa, IZUMO, "かが")
        assert specs["全長"] == ("250", [*IZUMO, "かが"])
        assert specs["番号"] == ("184", [*IZUMO, "かが"])
        assert member_specs_at(taxa, IZUMO, "いずも")["全長"] == ("248", IZUMO)
        assert member_photos_at(taxa, IZUMO, "かが") == (["https://x/izumo-class.jpg"], IZUMO)
        with_photo = update_member(taxa, IZUMO, "かが", photos=["https://x/kaga.jpg"])
        assert member_photos_at(with_photo, IZUMO, "かが") == (["https://x/kaga.jpg"], [*IZUMO, "かが"])

    def test_adds_changes_renames_moves_and_removes_an_individual_keeping_its_details_and_id(self):
        taxa = add_members(ships(), IZUMO, ["くまの", "かが", " "])
        assert find(taxa, IZUMO).members == ["いずも", "かが", "くまの"]
        assert find(taxa, IZUMO).member("くまの").id
        taxa = update_member(taxa, IZUMO, "いずも", specs={"番号": "183", "別名": ""}, note="")
        assert find(taxa, IZUMO).member("いずも").specs == {"番号": "183"}
        first_id = find(taxa, IZUMO).member("いずも").id
        assert first_id
        taxa = rename_member(taxa, IZUMO, "いずも", "出雲", keep_old=True)
        izumo = find(taxa, IZUMO)
        assert izumo.members == ["出雲", "かが", "くまの"]
        assert izumo.member("出雲").specs == {"番号": "183", "別名": "いずも"}
        assert izumo.member("出雲").id == first_id
        with pytest.raises(ValueError):
            rename_member(taxa, IZUMO, "出雲", "かが")
        taxa = move_member(taxa, IZUMO, "かが", ["船舶", "ひゅうが型"], before="ひゅうが")
        assert find(taxa, IZUMO).members == ["出雲", "くまの"]
        assert find(taxa, ["船舶", "ひゅうが型"]).members == ["かが", "ひゅうが"]
        assert find(taxa, ["船舶", "ひゅうが型"]).member("かが").note == "改修中"
        taxa = move_member(taxa, IZUMO, "くまの", IZUMO, before="出雲")
        assert find(taxa, IZUMO).members == ["くまの", "出雲"]
        taxa = remove_member(taxa, IZUMO, "出雲")
        assert find(taxa, IZUMO).members == ["くまの"]
        assert "出雲" not in (find(taxa, IZUMO).member_info or {})

    def test_a_specification_field_is_for_classes_individuals_or_both_and_may_be_for_some_roots(self):
        problems: list[str] = []
        fields = spec_fields_of(["全長", {"name": "番号", "use": "member"}, {"name": "排水量", "use": "class", "roots": ["船舶"]},
                                 {"name": "x", "use": "?"}], problems)
        assert [f.to_json() for f in fields] == [{"name": "全長", "type": "text"}, {"name": "番号", "type": "text", "use": "member"},
                                                 {"name": "排水量", "type": "text", "use": "class", "roots": ["船舶"]},
                                                 {"name": "x", "type": "text"}]
        assert problems == ["specFields[3].use は both・class・member のいずれかで指定"]
        number, tonnage = fields[1], fields[2]
        assert number.offered(["船舶"], True) and not number.offered(["船舶"], False)
        assert tonnage.offered(["船舶", "いずも型"], False) and not tonnage.offered(["航空機"], False)
        assert [f.name for f in DEFAULT_SPEC_FIELDS if f.use == "member"] == ["番号", "所属", "拠点", "状態"]

    def test_the_table_of_individuals_is_set_beside_the_classification_table(self):
        problems: list[str] = []
        table = taxonomy_table_of({"url": "https://x/FeatureServer/1", "members": {"url": "https://x/FeatureServer/2",
                                                                                     "fields": {"aliases": "OTHER_NAMES", "x": "X"}}},
                                  problems, "t")
        assert table.members.url == "https://x/FeatureServer/2"
        assert table.members.fields["aliases"] == "OTHER_NAMES"
        assert problems == ["t.members.fields.x は使えない役割"]
        assert taxonomy_table_of({"url": "https://x/FeatureServer/1", "members": "https://x/FeatureServer/1"}, problems, "t").members is None
        assert problems[-1] == "t.members は分類のテーブルとは別のテーブルで指定"


CLASSES = "https://x/FeatureServer/3"
MEMBERS = "https://x/FeatureServer/4"
MEMBER_FIELDS = ("MEMBER_ID", "CLASS_PATH", "MEMBER_NAME", "ALIASES", "SPECS", "PHOTOS", "NOTE", "SORT_ORDER")


def esri(members=True):
    fake = FakeEsri()
    classes = fake.add_layer(CLASSES, FakeLayer({n: "esriFieldTypeString" for n in ("PATH", "MEMBERS", "SPECS", "SORT_ORDER")}, ""))
    table = fake.add_layer(MEMBERS, FakeLayer({n: "esriFieldTypeString" for n in MEMBER_FIELDS}, "")) if members else None
    config = taxonomy_table_of({"url": CLASSES, **({"members": MEMBERS} if members else {})}, [], "t")
    return fake, classes, table, EsriTaxonomyStore(config, EsriClient(fake), "分類")


def rows(layer):
    return [{k: v for k, v in r["attributes"].items() if k != "OBJECTID"} for r in layer.rows]


class TestEsriTableOfIndividuals:
    def test_reads_individuals_details_from_their_table_and_names_only_from_members(self):
        fake, classes, table, store = esri()
        classes.add({"PATH": "船舶", "SORT_ORDER": 0})
        classes.add({"PATH": "船舶 > いずも型", "MEMBERS": "いずも,かが", "SORT_ORDER": 1})
        table.add({"MEMBER_ID": "m2", "CLASS_PATH": "船舶 > いずも型", "MEMBER_NAME": "かが", "ALIASES": "DDH-184,Kaga",
                   "SPECS": '{"番号":"184"}', "NOTE": "改修中", "SORT_ORDER": 1})
        table.add({"MEMBER_ID": "m3", "CLASS_PATH": "船舶 > いずも型", "MEMBER_NAME": "くまの", "SORT_ORDER": 2})
        table.add({"MEMBER_ID": "m9", "CLASS_PATH": "船舶 > 消えた型", "MEMBER_NAME": "x"})
        taxa = store.load()
        izumo = find(taxa, IZUMO)
        assert store.keeps_members
        # くまの (in the table, not in MEMBERS: added there, removed by an older version) comes after the others.
        assert izumo.members == ["いずも", "かが", "くまの"]
        assert izumo.member("かが") == Member("かが", {"番号": "184", "別名": "DDH-184、Kaga"}, None, "改修中", "m2")
        # One with no row yet says nothing but has its id already (a rename before its first save keeps it).
        assert not izumo.member("いずも").says() and izumo.member("いずも").id

    def test_writes_a_row_per_individual_and_changes_only_those_changed(self):
        fake, classes, table, store = esri()
        classes.add({"PATH": "船舶", "SORT_ORDER": 0})
        classes.add({"PATH": "船舶 > いずも型", "MEMBERS": "いずも,かが", "SORT_ORDER": 1})
        table.add({"MEMBER_ID": "m2", "CLASS_PATH": "船舶 > いずも型", "MEMBER_NAME": "かが", "ALIASES": "DDH-184", "SORT_ORDER": 1})
        taxa = store.load()
        # The first save adds いずも's row (it had none) and leaves かが's.
        store.save(taxa)
        assert [r["MEMBER_NAME"] for r in rows(table)] == ["かが", "いずも"]
        izumo_id = next(r["MEMBER_ID"] for r in rows(table) if r["MEMBER_NAME"] == "いずも")
        asked = len(fake.asked)
        store.save(taxa)
        assert not any("applyEdits" in url for url, _ in fake.asked[asked:])
        taxa = update_member(taxa, IZUMO, "かが", specs={"別名": "DDH-184、加賀", "番号": "184"})
        taxa = rename_member(taxa, IZUMO, "いずも", "出雲")
        store.save(taxa)
        kept = {r["MEMBER_ID"]: r for r in rows(table)}
        assert kept["m2"]["ALIASES"] == "DDH-184,加賀"
        assert json.loads(kept["m2"]["SPECS"]) == {"番号": "184"}
        # A renamed individual keeps its row (by its id when known, else by its class and name).
        assert kept[izumo_id]["MEMBER_NAME"] == "出雲"
        assert next(r for r in rows(classes) if r["PATH"] == "船舶 > いずも型")["MEMBERS"] == "出雲,かが"
        store.save(remove_member(taxa, IZUMO, "かが"))
        assert [r["MEMBER_NAME"] for r in rows(table)] == ["出雲"]

    def test_an_individual_someone_else_added_meanwhile_is_changed_not_added_again_and_a_double_goes(self):
        fake, classes, table, store = esri()
        classes.add({"PATH": "船舶 > いずも型", "MEMBERS": "いずも"})
        taxa = store.load()
        table.add({"MEMBER_ID": "other", "CLASS_PATH": "船舶 > いずも型", "MEMBER_NAME": "いずも", "NOTE": "x"})
        store.save(taxa)
        assert [(r["MEMBER_ID"], r["NOTE"]) for r in rows(table)] == [("other", "")]
        table.add({"MEMBER_ID": "double", "CLASS_PATH": "船舶 > いずも型", "MEMBER_NAME": "いずも"})
        store.save(store.load())
        assert [r["MEMBER_ID"] for r in rows(table)] == ["other"]

    def test_without_a_table_of_individuals_only_their_names_are_kept(self):
        fake, classes, _, store = esri(members=False)
        classes.add({"PATH": "船舶 > いずも型", "MEMBERS": "いずも"})
        taxa = update_member(store.load(), IZUMO, "いずも", specs={"番号": "183"})
        assert not store.keeps_members
        store.save(taxa)
        assert find(store.load(), IZUMO) == Taxon(IZUMO, members=["いずも"])
        assert classes.rows[0]["attributes"]["MEMBERS"] == "いずも"

    def test_says_which_attribute_the_table_of_individuals_lacks(self):
        fake = FakeEsri()
        fake.add_layer(CLASSES, FakeLayer({"PATH": "esriFieldTypeString"}, ""))
        fake.add_layer(MEMBERS, FakeLayer({n: "esriFieldTypeString" for n in ("MEMBER_ID", "CLASS_PATH", "MEMBER_NAME")}, ""))
        store = EsriTaxonomyStore(taxonomy_table_of({"url": CLASSES, "members": MEMBERS}, [], "t"), EsriClient(fake), "分類")
        assert store.lacks_member("aliases") == "ALIASES"
        assert store.lacks_member("name") is None


def test_the_local_store_keeps_what_individuals_say(tmp_path):
    with open_local(str(tmp_path / "o.gpkg")) as db:
        store = LocalTaxonomyStore(db, "ローカル")
        taxa = ships()
        store.save(taxa)
        assert store.load() == taxa
        assert store.keeps_members


@pytest.fixture
def book(tmp_path):
    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=Prefs(), say=said.append)
    b.said = said
    b.reload()
    b.set_taxonomy(SHIPS)
    b.set_open_images([ObservedImage("d1", "d1.tif", 1_790_000_000_000, [139.6, 35.5, 139.8, 35.7], "L1")], "L1")
    yield b
    db.close()


class TestBook:
    def test_adds_changes_moves_and_removes_individuals(self, book):
        assert book.add_members(IZUMO, ["くまの", "かが"]) == ["くまの"]
        assert book.update_member(IZUMO, "くまの", specs={"番号": "189", "別名": "Kumano"}, note="新造") == "くまの"
        assert book.find_member("kumano") == MemberHit(IZUMO, "くまの", "Kumano")
        assert book.move_member(IZUMO, "くまの", ["船舶", "ひゅうが型"])
        assert find(book.taxonomy(), ["船舶", "ひゅうが型"]).member("くまの").note == "新造"
        assert book.remove_member(["船舶", "ひゅうが型"], "くまの")
        assert book.find_member("くまの") is None
        assert book.update_member(IZUMO, "いせ") is None
        assert book.said[-1] == "個体「いせ」が分類「船舶 > いずも型」にない"

    def test_a_rename_keeps_the_old_name_as_another_name_when_records_use_it(self, book):
        assert book.add(139.7, 35.6, object="かが", kind="船舶 > いずも型") is not None
        assert book.update_member(IZUMO, "かが", new_name="加賀") == "加賀"
        assert "記録の古い名前は別名に" in book.said[-1]
        assert book.find_member("かが") == MemberHit(IZUMO, "加賀", "かが")
        assert book.update_member(IZUMO, "いずも", new_name="出雲") == "出雲"
        assert find(book.taxonomy(), IZUMO).member("出雲").specs is None


class TestTable:
    """表からの取り込み・書き出し: on a row naming one individual, 個体の別名 and 個体の諸元: x are that individual's."""

    COLUMNS = ["大分類", "級", "既知の個体", "全長（m）", "個体の別名", "番号", "個体の諸元: 母港"]

    def rows(self, table, columns=None):
        from mikage.core.obs import taxonomy_import as ti

        columns = columns or self.COLUMNS
        return [ti.TableRow(n, dict(zip(columns, r))) for n, r in enumerate(table, start=2)]

    def plan(self, table, mode="add", existing=None, columns=None):
        from mikage.core.obs import taxonomy_import as ti

        columns = columns or self.COLUMNS
        existing = ships() if existing is None else existing
        uses = ti.guess_uses(columns, existing, DEFAULT_SPEC_FIELDS)
        read = ti.read_rows(self.rows(table, columns), columns, uses)
        return uses, ti.plan_import(existing, DEFAULT_SPEC_FIELDS, read, columns, uses, mode)

    def test_columns_of_individuals_are_guessed_and_parsed(self):
        from mikage.core.obs import taxonomy_import as ti

        uses = ti.guess_uses(self.COLUMNS, [], DEFAULT_SPEC_FIELDS)
        # 番号 is for individuals only; 個体の… columns are an individual's, a field not known a new one.
        assert [u.label for u in uses] == ["階層 1", "階層 2", "既知の個体", "諸元: 全長", "個体の別名", "個体の諸元: 番号",
                                           "個体の諸元に追加（列の名前で）"]
        assert ti.member_column_name("個体の諸元: 全長（m）") == ("全長", "m")
        assert ti.parse_use("個体の別名") == ti.Use(ti.MEMBER_SPEC, "別名")
        assert ti.parse_use("memberSpec:番号", DEFAULT_SPEC_FIELDS) == ti.Use(ti.MEMBER_SPEC, "番号")
        assert ti.parse_use("個体の諸元に追加（列の名前で）") == ti.Use(ti.NEW_MEMBER_SPEC)
        with pytest.raises(ValueError):
            ti.parse_use("個体の諸元: 色", DEFAULT_SPEC_FIELDS)
        choices = ti.use_choices(3, DEFAULT_SPEC_FIELDS)
        assert ti.Use(ti.MEMBER_SPEC, "番号") in choices and ti.Use(ti.SPEC, "番号") not in choices
        assert "既知の個体" in ti.check_uses([ti.Use(ti.LEVEL, 1), ti.Use(ti.MEMBER_SPEC, "別名")])

    def test_a_row_naming_one_individual_gives_it_other_names_and_specifications(self):
        uses, p = self.plan([
            ["船舶", "いずも型", "いずも", "248", "DDH-183、Izumo", "183", "横須賀"],
            ["船舶", "いずも型", "かが", "", "", "185", ""],
            ["船舶", "いずも型", "くまの、もがみ", "", "FFM", "", ""],
            ["船舶", "ひゅうが型", "", "", "DDH-181", "", ""],
        ])
        izumo = find(p.taxa, IZUMO)
        assert izumo.members == ["いずも", "かが", "くまの", "もがみ"]
        assert izumo.member("いずも").specs == {"別名": "DDH-183、Izumo", "番号": "183", "母港": "横須賀"}
        # Adding: a blank keeps what it had; a value replaces.
        assert izumo.member("かが").specs == {"別名": "DDH-184、Kaga", "番号": "185", "全長": "250"} and izumo.member("かが").note == "改修中"
        assert izumo.specs["全長"] == "248"
        changes = {c.kind: c for c in p.classes}["船舶 > いずも型"]
        assert "いずもの別名 DDH-183、Izumo" in changes.changes and "かがの番号 184 → 185" in changes.changes
        assert "行 4 の個体の別名・諸元は使わない（個体が 2 つ以上。1 行に 1 個体で）" in changes.notes
        assert "行 5 の個体の別名・諸元は使わない（既知の個体が空欄）" in {c.kind: c for c in p.classes}["船舶 > ひゅうが型"].notes
        # The new field is for individuals.
        assert [(f.name, f.type, f.use) for f in p.spec_fields[-1:]] == [("母港", "text", "member")]
        # Replacing: a blank clears the individual's value.
        _, p = self.plan([["船舶", "いずも型", "かが", "", "", "185", ""]], mode="replace")
        assert find(p.taxa, IZUMO).member("かが").specs == {"番号": "185", "全長": "250"}

    def test_the_table_written_has_a_row_per_individual_and_reads_back(self):
        from mikage.core.obs import taxonomy_import as ti

        taxa = ships()
        head, rows = ti.taxonomy_table(taxa, DEFAULT_SPEC_FIELDS)
        assert head[-3:] == ["個体の別名", "個体の諸元: 全長（m）", "個体の諸元: 番号"]
        assert rows[1][:3] == ["船舶", "いずも型", ""]
        assert rows[2][:3] == ["船舶", "いずも型", ""] and rows[2][head.index("既知の個体")] == "いずも"
        assert rows[3][head.index("既知の個体")] == "かが" and rows[3][-3:] == ["DDH-184、Kaga", "250", "184"]
        uses = ti.guess_uses(head, [], DEFAULT_SPEC_FIELDS)
        assert [u.label for u in uses[-3:]] == ["個体の別名", "個体の諸元: 全長", "個体の諸元: 番号"]
        read = ti.read_rows([ti.TableRow(n, dict(zip(head, r))) for n, r in enumerate(rows, start=2)], head, uses)
        assert not ti.plan_import(taxa, DEFAULT_SPEC_FIELDS, read, head, uses, ti.REPLACE).changed
        back = ti.plan_import([], DEFAULT_SPEC_FIELDS, read, head, uses, ti.REPLACE).taxa
        assert find(back, IZUMO).members == ["いずも", "かが"] and find(back, IZUMO).member("かが").specs == find(taxa, IZUMO).member("かが").specs
        # Without individuals' specifications: one row per class, as before.
        head, rows = ti.taxonomy_table(taxonomy_of([{"name": "船舶", "members": ["いずも"]}], []), DEFAULT_SPEC_FIELDS)
        assert "個体の別名" not in head and rows[0][head.index("既知の個体")] == "いずも"


def test_individuals_and_their_other_names_are_offered_in_notes_and_never_pointed_out():
    from mikage.core.obs.glossary import Glossary, Term, member_terms

    terms = member_terms(ships())
    assert [(t.text, t.description) for t in terms[:3]] == [("いずも", "いずも型の個体"), ("かが", "いずも型の個体"), ("DDH-184", "かがの別名（いずも型）")]
    g = Glossary([Term("入港", variants=("入航",))]).with_terms(terms)
    assert [s.term.text for s in g.suggest("DDH-1", "船舶")] == ["DDH-184"]
    assert g.suggest("ｋａｇ", "船舶")[0].term.text == "Kaga"
    assert g.suggest("Kag", "航空機") == []
    # Only the glossary's own words are pointed out.
    assert [(v.found, v.term) for v in g.variants_in("ＫＡＧＡ が入航", "船舶")] == [("入航", "入港")]


def test_the_books_words_for_notes_follow_the_classification(book):
    words = book.note_words()
    assert book.note_words() is words and "ひゅうが" in [t.text for t in words]
    book.update_member(IZUMO, "いずも", specs={"別名": "DDH-183"})
    assert "DDH-183" in [t.text for t in book.note_words()]


def test_a_table_of_records_takes_the_class_of_the_known_individual_it_names():
    from mikage.core.obs import record_import as ri
    from mikage.core.obs.taxonomy import member_finder

    finder = member_finder(ships())
    assert finder("ＫＡＧＡ") == MemberHit(IZUMO, "かが", "Kaga") and finder("いずも") == MemberHit(IZUMO, "いずも")
    assert finder("あたご") is None and finder(" ") is None
    taxa = taxonomy_of([{"name": "船舶", "nameLabel": "艦名", "children": SHIPS[0]["children"]}], [])
    mapping = {"image_id": "img", "object": "name", "kind": "kind"}
    source = [ri.SourceRow(2, {"img": "d1", "name": "DDH-184", "kind": ""}, 139.7, 35.6),
              ri.SourceRow(3, {"img": "d1", "name": "いずも", "kind": "船舶"}, 139.8, 35.6)]
    planned = ri.plan(source, mapping, [], (), taxa)
    first, second = planned
    assert first.record.kind == "船舶 > いずも型" and first.record.object_name == "DDH-184"
    assert first.warnings == ["種別は既知の個体「かが」の分類（船舶 > いずも型）", "「DDH-184」は既知の個体「かが」の別名"]
    # A 種別 written stays.
    assert second.record.kind == "船舶" and second.warnings == []
    assert ri.query_for(source, mapping, taxa).names == ("DDH-184", "いずも")


def test_scripts_list_and_set_known_individuals(book):
    from mikage.core.obs.api import ObservationApi

    api = ObservationApi(lambda: book, lambda _d, work: work(book))
    [kaga] = api.known_individuals(name="ｋａｇａ")
    assert kaga == {"kind": "船舶 > いずも型", "name": "かが", "aliases": ["DDH-184", "Kaga"], "specs": {"番号": "184", "全長": "250"},
                    "photos": [], "note": "改修中", "id": kaga["id"]}
    assert [m["name"] for m in api.known_individuals(kind="船舶 > ひゅうが型")] == ["ひゅうが"]
    assert len(api.known_individuals()) == 3 and api.known_individuals(name="あたご") == []
    out = api.set_individual("船舶 > いずも型", "くまの", aliases="FFM-2、Kumano", specs={"番号": 2})
    assert (out["name"], out["aliases"], out["specs"]) == ("くまの", ["FFM-2", "Kumano"], {"番号": "2"})
    out = api.set_individual("船舶 > いずも型", "かが", specs={"全長": None}, aliases=[], new_name="加賀")
    assert (out["name"], out["aliases"], out["specs"], out["note"]) == ("加賀", [], {"番号": "184"}, "改修中")
    with pytest.raises(LookupError):
        api.set_individual("船舶 > 無い", "x")
