"""tools/arcgis（ArcGIS でテーブルを作り、ポータルに公開するスクリプト）がプラグインの期待と合っているか。

ArcPy はないので、ArcPy の呼び出しは小さな偽物で確かめる。テーブルの定義は README の表（プラグインの既定と照合済み、
test_readme_tables.py）と 1 属性ずつ同じであること。
"""
from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace

import pytest

from mikage.core import edit_stamp
from mikage.core.config import load_jsonc, parse_config_text

from .test_readme_tables import DEFAULTS, NAME, _section, _tables

TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "arcgis")
sys.path.insert(0, os.path.abspath(TOOLS))

import create_tables  # noqa: E402
import publish_service  # noqa: E402
import mikage_schema as schema  # noqa: E402

TYPES = {"Date": ("DATE", None), "Double": ("DOUBLE", None), "Integer": ("LONG", None), "SmallInteger": ("SHORT", None)}
#: Oracle・SQL Server・PostgreSQL の予約語のうち、属性名になりそうなもの。
RESERVED = {"MODE", "FILE", "DATE", "LEVEL", "SIZE", "USER", "COMMENT", "ORDER", "GROUP", "TABLE", "INDEX", "NUMBER",
            "UID", "ROWID", "SESSION", "ACCESS", "ROW", "ROWS", "START", "VALUE", "SHAPE", "OBJECTID", "PUBLIC"}
#: ArcSDE が形状（ST_Geometry）の中身に使う名前（Esri 「Words by reservation only」）。Oracle の予約語ではないが避ける。
ARCSDE_NAMES = {"FID", "AREA", "LEN", "POINTS", "NUMOFPTS", "ENTITY", "EMINX", "EMINY", "EMAXX", "EMAXY", "EMINZ", "EMAXZ",
                "MIN_MEASURE", "MAX_MEASURE"}


def _readme_overview() -> dict:
    """「テーブルの一覧」の表: テーブル → (形状, 読み書き)。"""
    out = {}
    for m in re.finditer(r"^\| \[`(TBL_\w+)`\]\([^)]*\) \| ([^|]+) \| ([^|]+) \|", _section(), re.MULTILINE):
        out[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
    return out


def test_tables_are_the_readme_tables_in_order():
    readme = _tables(_section())
    assert [t.name for t in schema.TABLES] == list(readme)
    overview = _readme_overview()
    for table in schema.TABLES:
        shape, rw = overview[table.name]
        assert table.writable == rw.startswith("書き込み"), table.name
        expected = "POLYGON" if shape.startswith("ポリゴン") else "POINT" if shape.startswith("ポイント") else None
        assert table.geometry == expected, table.name
        assert table.title in _section().split(f"### `{table.name}`", 1)[1].splitlines()[0], table.name


def test_fields_are_the_readme_rows():
    readme = _tables(_section())
    for table in schema.TABLES:
        rows = readme[table.name]
        assert [f.name for f in table.fields] == [r[0] for r in rows], table.name
        for f, row in zip(table.fields, rows):
            m = re.match(r"String\((\d+)\)$", row[2])
            assert (f.type, f.length) == (("TEXT", int(m.group(1))) if m else TYPES[row[2]]), f"{table.name}.{f.name}"
            assert f.alias == row[1], f"{table.name}.{f.name}"
            if table.name != "TBL_PLACE_NAME":  # 地名検索は役割ではなく `field` の順
                assert f.role == row[4], f"{table.name}.{f.name}"


def test_every_default_name_of_the_plugin_is_created():
    for table_name, defaults in DEFAULTS.items():
        fields = {f.role: f.name for f in schema.BY_NAME[table_name].fields}
        for role, name in defaults.items():
            assert fields.get(role) == name, f"{table_name} の {role}"


def test_writable_tables_get_the_edit_tracking_fields():
    assert {f.role: f.name for f in schema.EDIT_FIELDS} == edit_stamp.DEFAULT_FIELDS
    for table in schema.TABLES:
        names = [f.name for f in schema.all_fields(table)]
        has = set(edit_stamp.DEFAULT_FIELDS.values()) <= set(names)
        assert has == table.writable, table.name
        assert [f.name for f in schema.all_fields(table, edit_tracking=False)] == [f.name for f in table.fields]


def test_names_follow_the_naming_rule():
    for table in schema.TABLES:
        assert NAME.match(table.name) and table.name.startswith("TBL_"), table.name
        names = [f.name for f in schema.all_fields(table)]
        assert len(names) == len(set(names)), table.name
        for name in names:
            assert NAME.match(name), f"{table.name}.{name}"
            assert name not in RESERVED | ARCSDE_NAMES, f"{table.name}.{name}"
        for field in table.indexes:
            assert field in names, f"{table.name} のインデックス {field}"
    index_names = [schema.index_name(t.name, f) for t in schema.TABLES for f in t.indexes]
    assert len(index_names) == len(set(index_names))
    assert all(NAME.match(n) for n in index_names)


def test_renamed_fields_are_the_plugin_legacy_names():
    from mikage.core.obs.alerts import DEFAULT_RULE_FIELDS, LEGACY_RULE_FIELDS
    from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, LEGACY_HISTORY_FIELDS
    plugin = {
        "TBL_ALERT_RULE": {DEFAULT_RULE_FIELDS[r]: old for r, old in LEGACY_RULE_FIELDS.items()},
        "TBL_REQUEST_HISTORY": {DEFAULT_HISTORY_FIELDS[r]: old for r, old in LEGACY_HISTORY_FIELDS.items()},
    }
    assert schema.LEGACY_FIELDS == plugin
    for table, renamed in schema.LEGACY_FIELDS.items():
        names = {f.name for f in schema.BY_NAME[table].fields}
        assert set(renamed) <= names and not set(renamed.values()) & names, table


def test_plugin_config_is_the_readme_example():
    example = _section().split("```jsonc\n", 1)[1].split("```", 1)[0]
    urls = {t.name: f"https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/{i}" for i, t in enumerate(schema.TABLES)}
    assert schema.plugin_config(urls) == load_jsonc(example)


def test_plugin_config_of_some_tables_reads_without_problems():
    import json
    urls = {"TBL_OBSERVATION": "https://gis.example/s/FeatureServer/7", "TBL_TAXONOMY": "https://gis.example/s/FeatureServer/9",
            "TBL_ALERT_LOG": "https://gis.example/s/FeatureServer/13"}
    config = schema.plugin_config(urls)
    assert set(config) == {"observationStores", "alertReports"}
    assert "areas" not in config["observationStores"][0]
    parsed, problems = parse_config_text(json.dumps(config, ensure_ascii=False))
    assert problems == []
    assert parsed.observation_stores[0].url.endswith("/7")
    _, problems = parse_config_text(json.dumps(schema.plugin_config({t.name: f"https://g/x/FeatureServer/{i}"
                                                                     for i, t in enumerate(schema.TABLES)}), ensure_ascii=False))
    assert problems == []


# --- create_tables.py with a stand-in ArcPy -------------------------------------------------------------

class FakeArcpy:
    """ArcPy の、create_tables.py が使うところだけ（ジオデータベースを辞書で持つ）。"""

    TYPES = {"TEXT": "String", "DATE": "Date", "DOUBLE": "Double", "LONG": "Integer", "SHORT": "SmallInteger"}

    def __init__(self, workspace):
        self.workspace = workspace
        self.tables: dict = {}
        self.calls: list = []
        m = SimpleNamespace(CreateFeatureclass=self._create_fc, CreateTable=self._create_table, AddField=self._add_field,
                            AlterField=self._alter_field, AddIndex=self._add_index, ChangePrivileges=self._privileges,
                            CalculateField=self._calculate)
        self.rows: dict = {}
        self.management = m

    def _name(self, path):
        return os.path.basename(path)

    def Exists(self, path):  # noqa: N802
        return path == self.workspace or self._name(path) in self.tables

    def _create_fc(self, workspace, name, geometry, spatial_reference=None):
        self.calls.append(("CreateFeatureclass", name, geometry))
        shape = {"POLYGON": "Polygon", "POINT": "Point"}[geometry]
        self.tables[name] = {"shape": shape, "fields": [SimpleNamespace(name="OBJECTID", type="OID", length=4, aliasName="OBJECTID"),
                                                        SimpleNamespace(name="SHAPE", type="Geometry", length=0, aliasName="SHAPE")],
                             "indexes": [], "alias": "", "grants": {}}

    def _create_table(self, workspace, name):
        self.calls.append(("CreateTable", name))
        self.tables[name] = {"shape": None, "fields": [SimpleNamespace(name="OBJECTID", type="OID", length=4, aliasName="OBJECTID")],
                             "indexes": [], "alias": "", "grants": {}}

    def _add_field(self, path, name, type_, field_length=None, field_alias="", field_is_nullable="NULLABLE"):
        self.calls.append(("AddField", self._name(path), name))
        self.tables[self._name(path)]["fields"].append(
            SimpleNamespace(name=name, type=self.TYPES[type_], length=field_length or (255 if type_ == "TEXT" else 8), aliasName=field_alias))

    def _alter_field(self, path, field, new_field_alias=""):
        self.calls.append(("AlterField", self._name(path), field))
        for f in self.tables[self._name(path)]["fields"]:
            if f.name == field:
                f.aliasName = new_field_alias

    def _calculate(self, path, field, expression, expression_type="PYTHON3"):
        self.calls.append(("CalculateField", self._name(path), field, expression))
        source = re.fullmatch(r"!(\w+)!", expression).group(1)
        for row in self.rows.get(self._name(path), []):
            row[field] = row.get(source)

    def _add_index(self, path, fields, name, unique, ascending):
        self.calls.append(("AddIndex", self._name(path), name))
        table = self.tables[self._name(path)]
        by_name = {f.name: f for f in table["fields"]}
        table["indexes"].append(SimpleNamespace(name=name, fields=[by_name[f] for f in fields]))

    def _privileges(self, path, user, view, edit):
        self.calls.append(("ChangePrivileges", self._name(path), user, view, edit))
        self.tables[self._name(path)]["grants"][user] = (view, edit)

    def Describe(self, path):  # noqa: N802
        t = self.tables[self._name(path)]
        return SimpleNamespace(dataType="FeatureClass" if t["shape"] else "Table", shapeType=t["shape"])

    def ListFields(self, path):  # noqa: N802
        return list(self.tables[self._name(path)]["fields"])

    def ListIndexes(self, path):  # noqa: N802
        return list(self.tables[self._name(path)]["indexes"])

    def AlterAliasName(self, path, alias):  # noqa: N802
        self.tables[self._name(path)]["alias"] = alias


def _run_all(arcpy, tables=None, **kw):
    report = create_tables.Report()
    for table in tables or schema.TABLES:
        create_tables.ensure_table(arcpy, arcpy.workspace, table, None, report, **kw)
    return report


def test_create_tables_makes_everything_and_a_second_run_changes_nothing(capsys):
    arcpy = FakeArcpy("C:/gis/owner.sde")
    report = _run_all(arcpy)
    assert report.warnings == []
    assert set(arcpy.tables) == set(schema.BY_NAME)
    for table in schema.TABLES:
        made = arcpy.tables[table.name]
        assert made["shape"] == create_tables.SHAPE_TYPES.get(table.geometry)
        assert made["alias"] == table.title
        fields = {f.name: f for f in made["fields"]}
        for f in schema.all_fields(table):
            assert fields[f.name].type == FakeArcpy.TYPES[f.type] and fields[f.name].aliasName == f.alias
            if f.type == "TEXT":
                assert fields[f.name].length == f.length
        assert sorted(i.fields[0].name for i in made["indexes"]) == sorted(table.indexes)
    arcpy.calls.clear()
    report = _run_all(arcpy)
    assert arcpy.calls == [] and report.done == [] and report.warnings == []


def test_create_tables_adds_missing_fields_and_leaves_different_ones():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    obs = schema.BY_NAME["TBL_OBSERVATION"]
    arcpy._create_fc(arcpy.workspace, obs.name, "POINT")
    arcpy._add_field(arcpy.workspace + "/" + obs.name, "OBS_ID", "TEXT", 64, "記録ID")
    arcpy._add_field(arcpy.workspace + "/" + obs.name, "NOTE", "TEXT", 255, "")        # 短い
    arcpy._add_field(arcpy.workspace + "/" + obs.name, "PRESENT", "DOUBLE", None, "在不在")  # 型が違う
    arcpy._add_field(arcpy.workspace + "/" + obs.name, "EXTRA", "TEXT", 10, "")        # 表にない
    arcpy.calls.clear()
    report = _run_all(arcpy, [obs])
    added = [c[2] for c in arcpy.calls if c[0] == "AddField"]
    assert "OBS_ID" not in added and "NOTE" not in added and "PRESENT" not in added
    assert set(added) == {f.name for f in schema.all_fields(obs)} - {"OBS_ID", "NOTE", "PRESENT"}
    assert ("AlterField", obs.name, "NOTE") in arcpy.calls
    assert any("NOTE" in w for w in report.warnings) and any("PRESENT" in w for w in report.warnings)
    assert "EXTRA" in {f.name for f in arcpy.tables[obs.name]["fields"]}


def test_create_tables_copies_a_renamed_field():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    rule = schema.BY_NAME["TBL_ALERT_RULE"]
    arcpy._create_table(arcpy.workspace, rule.name)
    for name in ("KIND", "ACTIVITIES", "AREA"):
        arcpy._add_field(arcpy.workspace + "/" + rule.name, name, "TEXT", 255, "")
    arcpy.rows[rule.name] = [{"KIND": "船舶", "AREA": "東京湾"}, {"KIND": "航空機", "AREA": None}]
    arcpy.calls.clear()
    report = _run_all(arcpy, [rule])
    assert ("CalculateField", rule.name, "GEOFENCE_NAME", "!AREA!") in arcpy.calls
    assert [r["GEOFENCE_NAME"] for r in arcpy.rows[rule.name]] == ["東京湾", None]
    assert any("AREA" in w and "消してよい" in w for w in report.warnings)
    assert "AREA" in {f.name for f in arcpy.tables[rule.name]["fields"]}  # the old one is left
    arcpy.calls.clear()
    report = _run_all(arcpy, [rule])
    assert not any(c[0] == "CalculateField" for c in arcpy.calls)  # copied only when the new one is added


def test_create_tables_warns_when_the_copy_fails():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    rule = schema.BY_NAME["TBL_ALERT_RULE"]
    arcpy._create_table(arcpy.workspace, rule.name)
    arcpy._add_field(arcpy.workspace + "/" + rule.name, "AREA", "TEXT", 255, "")

    def fail(*a, **kw):
        raise RuntimeError("ERROR 000539")
    arcpy.management.CalculateField = fail
    report = _run_all(arcpy, [rule])
    assert "GEOFENCE_NAME" in {f.name for f in arcpy.tables[rule.name]["fields"]}
    assert any("写せない" in w and "000539" in w for w in report.warnings)


def test_dry_run_tells_the_copy():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    rule = schema.BY_NAME["TBL_ALERT_RULE"]
    arcpy._create_table(arcpy.workspace, rule.name)
    arcpy._add_field(arcpy.workspace + "/" + rule.name, "AREA", "TEXT", 255, "")
    arcpy.calls.clear()
    report = _run_all(arcpy, [rule], dry_run=True)
    assert "AREA の値を GEOFENCE_NAME に写す" in report.done and arcpy.calls == []


def test_create_tables_warns_about_a_different_geometry():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    arcpy._create_fc(arcpy.workspace, "TBL_EVENT", "POINT")
    report = _run_all(arcpy, [schema.BY_NAME["TBL_EVENT"]])
    assert any("形状" in w for w in report.warnings)
    assert arcpy.tables["TBL_EVENT"]["shape"] == "Point"


def test_dry_run_changes_nothing():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    report = _run_all(arcpy, dry_run=True)
    assert arcpy.tables == {} and arcpy.calls == []
    assert len(report.done) > len(schema.TABLES)


def test_plan_tables_options():
    assert [t.name for t in create_tables.plan_tables(["TBL_OBSERVATION", "TBL_TAXONOMY"])] == ["TBL_OBSERVATION", "TBL_TAXONOMY"]
    names = [t.name for t in create_tables.plan_tables(skip=["TBL_IMAGE_CATALOG"])]
    assert "TBL_IMAGE_CATALOG" not in names and len(names) == len(schema.TABLES) - 1
    tables = {t.name: t for t in create_tables.plan_tables(geometry={"TBL_DETECTION": "point", "TBL_EVENT": "NONE"})}
    assert tables["TBL_DETECTION"].geometry == "POINT" and tables["TBL_EVENT"].geometry is None
    with pytest.raises(SystemExit):
        create_tables.plan_tables(["TBL_NOPE"])
    with pytest.raises(SystemExit):
        create_tables.plan_tables(geometry={"TBL_OBSERVATION": "POLYGON"})


def test_grant_gives_edit_only_on_writable_tables():
    arcpy = FakeArcpy("C:/gis/owner.sde")
    _run_all(arcpy)
    for name in ("TBL_OBSERVATION", "TBL_SATELLITE", "TBL_IMAGE_CATALOG"):
        create_tables.grant(arcpy, arcpy.workspace + "/" + name, schema.BY_NAME[name], ["VIEWER"], ["EDITOR"])
    assert arcpy.tables["TBL_OBSERVATION"]["grants"] == {"VIEWER": ("GRANT", "AS_IS"), "EDITOR": ("GRANT", "GRANT")}
    # The satellite tables are written by tools/satellite_editor.html.
    assert arcpy.tables["TBL_SATELLITE"]["grants"] == {"VIEWER": ("GRANT", "AS_IS"), "EDITOR": ("GRANT", "GRANT")}
    assert arcpy.tables["TBL_IMAGE_CATALOG"]["grants"] == {"VIEWER": ("GRANT", "AS_IS"), "EDITOR": ("GRANT", "AS_IS")}


def test_main_runs_with_the_stand_in(monkeypatch, tmp_path):
    arcpy = FakeArcpy(str(tmp_path / "test.gdb"))
    arcpy.SpatialReference = lambda code: ("sr", code)
    monkeypatch.setitem(sys.modules, "arcpy", arcpy)
    assert create_tables.main(["--workspace", arcpy.workspace, "--tables", "TBL_OBSERVATION", "--grant-edit", "X"]) == 0
    assert set(arcpy.tables) == {"TBL_OBSERVATION"}
    assert arcpy.tables["TBL_OBSERVATION"]["grants"] == {}  # ファイル ジオデータベースには付けない


# --- publish_service.py --------------------------------------------------------------------------------

SDDRAFT = """<?xml version="1.0" encoding="utf-8"?>
<SVCManifest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:typens="http://www.esri.com/schemas/ArcGIS/3.0.0">
<Configurations><SVCConfiguration><Definition>
<ConfigurationProperties><PropertyArray>
<PropertySetProperty><Key>maxRecordCount</Key><Value>2000</Value></PropertySetProperty>
</PropertyArray></ConfigurationProperties>
<Extensions>
<SVCExtension><Enabled>false</Enabled><Info><PropertyArray>
<PropertySetProperty><Key>WebEnabled</Key><Value>true</Value></PropertySetProperty>
</PropertyArray></Info><TypeName>KmlServer</TypeName></SVCExtension>
<SVCExtension>
  <Enabled>false</Enabled>
  <Info><PropertyArray>
    <PropertySetProperty><Key>WebCapabilities</Key><Value>Query</Value></PropertySetProperty>
  </PropertyArray></Info>
  <Props><PropertyArray>
    <PropertySetProperty><Key>maxRecordCount</Key><Value>2000</Value></PropertySetProperty>
  </PropertyArray></Props>
  <TypeName>FeatureServer</TypeName>
</SVCExtension>
</Extensions></Definition></SVCConfiguration></Configurations></SVCManifest>"""


def _extension(xml, type_name):
    from xml.dom import minidom
    doc = minidom.parseString(xml.encode("utf-8"))
    for t in doc.getElementsByTagName("TypeName"):
        if t.firstChild.data == type_name:
            ext = t.parentNode
            props = {}
            for prop in ext.getElementsByTagName("PropertySetProperty"):
                props[prop.getElementsByTagName("Key")[0].firstChild.data] = prop.getElementsByTagName("Value")[0].firstChild.data
            return ext.getElementsByTagName("Enabled")[0].firstChild.data, props
    raise AssertionError(type_name)


def test_enable_feature_access():
    out = publish_service.enable_feature_access(SDDRAFT, max_records=5000)
    enabled, props = _extension(out, "FeatureServer")
    assert enabled == "true"
    assert props["WebCapabilities"] == publish_service.CAPABILITIES
    for cap in ("Query", "Create", "Update", "Delete", "Editing"):
        assert cap in props["WebCapabilities"].split(",")
    assert props["allowGeometryUpdates"] == "true" and props["maxRecordCount"] == "5000"
    assert _extension(out, "KmlServer")[0] == "false"
    assert "<Key>maxRecordCount</Key><Value>5000</Value>" in out.split("<Extensions>")[0]
    # 何度かけても同じ
    assert publish_service.enable_feature_access(out, max_records=5000) == publish_service.enable_feature_access(
        publish_service.enable_feature_access(out, max_records=5000), max_records=5000)
    with pytest.raises(ValueError):
        publish_service.enable_feature_access(SDDRAFT.replace("FeatureServer", "WmsServer"))


def test_layer_urls_and_config(tmp_path):
    service = {"layers": [{"id": 0, "name": "TBL_IMAGE_CATALOG"}, {"id": 7, "name": "MIKAGE.TBL_OBSERVATION"}],
               "tables": [{"id": 9, "name": "tbl_taxonomy"}, {"id": 30, "name": "Other"}]}
    urls, unknown = publish_service.layer_urls("https://g/server/rest/services/MIKAGE/FeatureServer/", service)
    assert urls == {"TBL_IMAGE_CATALOG": "https://g/server/rest/services/MIKAGE/FeatureServer/0",
                    "TBL_OBSERVATION": "https://g/server/rest/services/MIKAGE/FeatureServer/7",
                    "TBL_TAXONOMY": "https://g/server/rest/services/MIKAGE/FeatureServer/9"}
    assert unknown == ["Other"]
    path = tmp_path / "config.json"
    publish_service.write_config(str(path), urls)
    config, problems = parse_config_text(path.read_text(encoding="utf-8"))
    assert problems == []
    assert config.observation_stores[0].url.endswith("/7")
    assert config.image_catalogs[0].fields["source"] == ["COG_URL", "FILE_PATH"]
