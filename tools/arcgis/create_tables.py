"""SKYWATCH が使う Esri のテーブル・フィーチャクラスをジオデータベースに作る（ArcGIS Pro の ArcPy）。

使い方（ArcGIS Pro の Python で。データ所有者のユーザーで接続した .sde を渡す）:

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" create_tables.py ^
        --workspace C:\\gis\\connections\\skywatch_owner.sde --sr 6668 --grant-edit SKYWATCH_EDITOR

何度実行してもよい（冪等）:
- ないテーブル・フィーチャクラスを作り、ない属性を足し、エイリアスを表のとおりにし、ない属性インデックスを張る
- 既にある属性の型・長さが表と違っても変えない（データを壊さないため。違いは最後に一覧で出す）
- 表にない属性は消さない

テーブル・属性の定義は `skywatch_schema.py`（README 「Esri 側のテーブル定義」と同じ）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skywatch_schema as schema  # noqa: E402

#: ArcPy の ListFields の型（Field.type）と AddField の型の対応。
FIELD_TYPES = {
    "TEXT": ("String",),
    "DATE": ("Date",),
    "DOUBLE": ("Double",),
    "LONG": ("Integer",),
    "SHORT": ("SmallInteger",),
}
#: Describe の shapeType と表の形状。
SHAPE_TYPES = {"POLYGON": "Polygon", "POINT": "Point"}
#: 形状を変えられるテーブル（README で「ポリゴンかポイント」「なしも可」のもの）と選べる形状。
GEOMETRY_CHOICES = {
    "TBL_EVENT": ("POLYGON", "NONE"),
    "TBL_REQUEST_HISTORY": ("POLYGON", "NONE"),
    "TBL_AI_RESULT": ("POLYGON", "POINT"),
    "TBL_DETECTION": ("POLYGON", "POINT"),
    "TBL_PLACE_NAME": ("POINT", "POLYGON"),
}


class Report:
    """実行した内容と、表との違い（変えなかったもの）。"""

    def __init__(self):
        self.done: list[str] = []
        self.warnings: list[str] = []

    def did(self, text: str):
        self.done.append(text)
        print("  " + text)

    def warn(self, text: str):
        self.warnings.append(text)
        print("  ! " + text)


def plan_tables(only=(), skip=(), geometry=None) -> list:
    """作るテーブル（`--tables`・`--skip`・`--geometry` を反映したもの）。"""
    geometry = dict(geometry or {})
    unknown = [n for n in list(only) + list(skip) + list(geometry) if n not in schema.BY_NAME]
    if unknown:
        raise SystemExit("知らないテーブル: " + ", ".join(unknown) + "（使えるもの: " + ", ".join(schema.BY_NAME) + "）")
    out = []
    for table in schema.TABLES:
        if (only and table.name not in only) or table.name in skip:
            continue
        if table.name in geometry:
            choice = geometry[table.name].upper()
            if choice not in GEOMETRY_CHOICES.get(table.name, ()):
                raise SystemExit(f"{table.name} の形状は {', '.join(GEOMETRY_CHOICES.get(table.name, ()) or ('変えられない',))}")
            table = table._replace(geometry=None if choice == "NONE" else choice)
        out.append(table)
    return out


def _field_matches(existing, wanted) -> bool:
    if existing.type not in FIELD_TYPES[wanted.type]:
        return False
    return wanted.type != "TEXT" or not wanted.length or int(existing.length or 0) >= wanted.length


def _copy_legacy(arcpy, path: str, table, old: str, new: str, report: Report, dry_run: bool) -> None:
    """前の名前の属性の値を、足したばかりの今の名前の属性に写す（前の属性は消さない）。"""
    if dry_run:
        report.did(f"{old} の値を {new} に写す")
        return
    try:
        arcpy.management.CalculateField(path, new, f"!{old}!", "PYTHON3")
    except Exception as e:  # noqa: BLE001 - 写せなくてもテーブルは使える
        report.warn(f"{table.name}.{old} の値を {new} に写せない: {e}。ArcGIS Pro で写す（プラグインは {new} を読む）")
        return
    report.did(f"{old} の値を {new} に写した")
    report.warn(f"{table.name}.{old} は前の名前（値は {new} に写した）。古いプラグインが使っていなければ消してよい")


def ensure_table(arcpy, workspace: str, table, spatial_reference, report: Report, edit_tracking: bool = True,
                 dry_run: bool = False) -> str:
    """1 テーブルを表のとおりにする（なければ作る・属性を足す・エイリアス・インデックス）。そのパスを返す。"""
    path = os.path.join(workspace, table.name)
    print(f"{table.name}（{table.title}）")
    if not arcpy.Exists(path):
        if dry_run:
            report.did(f"作る（{table.geometry or '形状なし'}）")
            for f in schema.all_fields(table, edit_tracking):
                report.did(f"属性を足す {f.name}")
            return path
        if table.geometry:
            arcpy.management.CreateFeatureclass(workspace, table.name, table.geometry, spatial_reference=spatial_reference)
        else:
            arcpy.management.CreateTable(workspace, table.name)
        report.did(f"作った（{table.geometry or '形状なし'}）")
    else:
        desc = arcpy.Describe(path)
        shape = getattr(desc, "shapeType", None) if getattr(desc, "dataType", "") == "FeatureClass" else None
        want = SHAPE_TYPES.get(table.geometry) if table.geometry else None
        if shape != want:
            report.warn(f"{table.name} の形状が {shape or 'なし'}（表は {want or 'なし'}）。作り直さない")
    if not dry_run:
        try:
            arcpy.AlterAliasName(path, table.title)
        except Exception as e:  # noqa: BLE001 - エイリアスは付かなくても使える
            report.warn(f"{table.name} のエイリアスを付けられない: {e}")

    existing = {f.name.upper(): f for f in arcpy.ListFields(path)} if arcpy.Exists(path) else {}
    legacy = schema.LEGACY_FIELDS.get(table.name, {})
    for wanted in schema.all_fields(table, edit_tracking):
        have = existing.get(wanted.name.upper())
        if have is None:
            if not dry_run:
                arcpy.management.AddField(path, wanted.name, wanted.type, field_length=wanted.length,
                                          field_alias=wanted.alias, field_is_nullable="NULLABLE")
            report.did(f"属性を足した {wanted.name}（{wanted.type}{f'({wanted.length})' if wanted.length else ''}）")
            old = existing.get(legacy.get(wanted.name, "").upper())
            if old is not None:
                _copy_legacy(arcpy, path, table, old.name, wanted.name, report, dry_run)
            continue
        if not _field_matches(have, wanted):
            report.warn(f"{table.name}.{have.name} は {have.type}({have.length})（表は {wanted.type}"
                        f"{f'({wanted.length})' if wanted.length else ''}）。変えない")
        if (have.aliasName or "") != wanted.alias and not dry_run:
            try:
                arcpy.management.AlterField(path, have.name, new_field_alias=wanted.alias)
                report.did(f"エイリアス {have.name} → {wanted.alias}")
            except Exception as e:  # noqa: BLE001
                report.warn(f"{table.name}.{have.name} のエイリアスを変えられない: {e}")

    if arcpy.Exists(path):
        indexed = set()
        for index in arcpy.ListIndexes(path):
            names = [f.name.upper() for f in index.fields]
            if len(names) == 1:
                indexed.add(names[0])
        for field in table.indexes:
            if field.upper() in indexed:
                continue
            if not dry_run:
                arcpy.management.AddIndex(path, [field], schema.index_name(table.name, field), "NON_UNIQUE", "ASCENDING")
            report.did(f"インデックスを張った {field}")
    elif dry_run:
        for field in table.indexes:
            report.did(f"インデックスを張る {field}")
    return path


def grant(arcpy, path: str, table, view_users=(), edit_users=(), report: Report = None, dry_run: bool = False):
    """データベースのユーザー・ロールに権限を付ける（エンタープライズ ジオデータベース）。

    編集のユーザーには、プラグインが書き込むテーブルだけ編集の権限（読みのテーブルは閲覧だけ）。
    """
    for user in list(view_users) + list(edit_users):
        edit = user in edit_users and table.writable
        if not dry_run:
            arcpy.management.ChangePrivileges(path, user, "GRANT", "GRANT" if edit else "AS_IS")
        if report:
            report.did(f"権限 {user}: 閲覧{'・編集' if edit else ''}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SKYWATCH の Esri テーブルを作る（何度実行してもよい）")
    parser.add_argument("--workspace", required=True, help="データ所有者で接続した .sde（試すならファイル ジオデータベース .gdb も可）")
    parser.add_argument("--sr", type=int, default=4326, help="フィーチャクラスの座標系の EPSG（既定 4326。JGD2011 なら 6668）")
    parser.add_argument("--tables", nargs="*", default=[], help="作るテーブルだけ（既定はすべて）")
    parser.add_argument("--skip", nargs="*", default=[], help="作らないテーブル（ほかのシステムが作る画像カタログなど）")
    parser.add_argument("--geometry", nargs="*", default=[], metavar="TABLE=TYPE",
                        help="形状を変える（TBL_AI_RESULT=POINT、TBL_DETECTION=POINT、TBL_EVENT=NONE、TBL_PLACE_NAME=POLYGON）")
    parser.add_argument("--no-edit-tracking", action="store_true", help="作成者・更新者の 4 属性を足さない")
    parser.add_argument("--grant-view", nargs="*", default=[], help="閲覧の権限を付けるユーザー・ロール")
    parser.add_argument("--grant-edit", nargs="*", default=[], help="書き込みのテーブルに編集の権限を付けるユーザー・ロール（公開に使うユーザーなど）")
    parser.add_argument("--dry-run", action="store_true", help="何をするかだけ出す（作らない）")
    args = parser.parse_args(argv)

    geometry = {}
    for item in args.geometry:
        if "=" not in item:
            parser.error(f"--geometry は TABLE=TYPE: {item}")
        name, kind = item.split("=", 1)
        geometry[name.strip()] = kind.strip()
    tables = plan_tables(args.tables, args.skip, geometry)

    import arcpy  # ArcGIS Pro の Python にだけある

    if not arcpy.Exists(args.workspace):
        raise SystemExit(f"ワークスペースがない: {args.workspace}")
    sr = arcpy.SpatialReference(args.sr)
    report = Report()
    granting = bool(args.grant_view or args.grant_edit)
    if granting and args.workspace.lower().rstrip("\\/").endswith(".gdb"):
        report.warn("ファイル ジオデータベースには権限を付けられない（--grant-* は .sde のとき）")
        granting = False
    for table in tables:
        path = ensure_table(arcpy, args.workspace, table, sr, report, not args.no_edit_tracking, args.dry_run)
        if granting:
            grant(arcpy, path, table, args.grant_view, args.grant_edit, report, args.dry_run)
    print()
    print(f"{len(tables)} テーブル、{len(report.done)} 件の変更{'（予定）' if args.dry_run else ''}")
    if report.warnings:
        print("表と違うところ（変えていない）:")
        for w in report.warnings:
            print("  " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
