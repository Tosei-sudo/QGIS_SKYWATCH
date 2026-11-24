"""地名（TBL_NAMED_AREA）の管理番号 NAMED_AREA_ID を自動で付ける仕組みを作る（ArcGIS Pro の ArcPy）。

2026-11-24 tsudo「自動採番が欲しい」「全てに付番すること」。プラグインの組織の関心地域（docs/features/images.md）と
同じ形 `NA-000123` の番号を、行を足したとき（管理番号が空のとき）にジオデータベースが付ける。

使い方（ArcGIS Pro の Python で。データ所有者のユーザーで接続した .sde を渡す）:

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" named_area_ids.py ^
        --workspace C:\\gis\\connections\\mikage_owner.sde

すること（何度実行してもよい。2 回目からは足りないものだけ）:
1. 管理番号が空の行に番号を付ける（`OBJECTID` の順に、今ある `NA-` の番号のいちばん大きいものの次から）
2. Oracle のシーケンス `SEQ_NAMED_AREA_ID` を、いちばん大きい番号の次から始まるように作る
3. TBL_NAMED_AREA にグローバル ID を足し（属性ルールに要る）、属性ルールを 2 つ足す
   - 計算ルール `MIKAGE_NAMED_AREA_ID`: 挿入・更新で管理番号が空なら `NA-` + シーケンスの次の値（6 桁）
   - 制約ルール `MIKAGE_NAMED_AREA_ID_UNIQUE`: ほかの行と同じ管理番号（大文字・小文字は区別しない）は保存させない
4. 終わったらサービスを上書き公開する（`publish_service.py publish`。属性ルールは参照で公開したサービスで動く）

ルールがなくてもプラグインは番号を付ける（いちばん大きい番号の次を付け、保存の後にもう一度重なりを確かめる）。
ルールがあると ArcGIS Pro・Portal で足した行にも付き、2 台が同時に足しても重ならない。
"""
from __future__ import annotations

import argparse
import re
import sys

TABLE = "TBL_NAMED_AREA"
FIELD = "NAMED_AREA_ID"
SEQUENCE = "SEQ_NAMED_AREA_ID"
PREFIX = "NA-"
DIGITS = 6
CALC_RULE = "MIKAGE_NAMED_AREA_ID"
UNIQUE_RULE = "MIKAGE_NAMED_AREA_ID_UNIQUE"


def number_of(text, prefix: str = PREFIX):
    """`NA-000123` の 123（ほかの形は None）。"""
    t = (text or "").strip()
    if not t.upper().startswith(prefix.upper()):
        return None
    rest = t[len(prefix):]
    return int(rest) if rest.isdigit() else None


def plan_numbers(rows, prefix: str = PREFIX, digits: int = DIGITS):
    """(OBJECTID, 管理番号) の並びから、空の行に付ける番号 {OBJECTID: 番号} と、その後のいちばん大きい番号。"""
    largest = max((n for n in (number_of(i, prefix) for _o, i in rows) if n is not None), default=0)
    out = {}
    for oid, value in sorted(rows):
        if not (value or "").strip():
            largest += 1
            out[oid] = f"{prefix}{largest:0{digits}d}"
    return out, largest


def calculation_rule(owner: str, prefix: str = PREFIX, digits: int = DIGITS) -> str:
    """計算ルールの式（Arcade）。空のときだけ番号を付け、手で入れた番号はそのまま。"""
    sequence = f"{owner}.{SEQUENCE}" if owner else SEQUENCE
    return (f"if (!IsEmpty($feature.{FIELD}) && Trim($feature.{FIELD}) != '') {{\n"
            f"    return $feature.{FIELD};\n"
            f"}}\n"
            f"return '{prefix}' + Text(NextSequenceValue('{sequence}'), '{'0' * digits}');\n")


def unique_rule(owner: str) -> str:
    """制約ルールの式（Arcade）。ほかの行と同じ管理番号なら false。"""
    table = f"{owner}.{TABLE}" if owner else TABLE
    return (f"var id = $feature.{FIELD};\n"
            f"if (IsEmpty(id) || Trim(id) == '') {{\n"
            f"    return true;\n"
            f"}}\n"
            f"var oid = $feature.OBJECTID;\n"
            f"var others = Filter(FeatureSetByName($datastore, '{table}', ['OBJECTID'], false),\n"
            f"                    'UPPER({FIELD}) = UPPER(@id) AND OBJECTID <> @oid');\n"
            f"if (Count(others) > 0) {{\n"
            f"    return {{ 'errorMessage': '管理番号 ' + id + ' はほかの地名が使用中' }};\n"
            f"}}\n"
            f"return true;\n")


def _owner_of(arcpy, workspace: str) -> str:
    """接続しているユーザー（エンタープライズ ジオデータベースのデータ所有者。ファイル ジオデータベースは ""）。"""
    if workspace.lower().rstrip("\\/").endswith(".gdb"):
        return ""
    props = arcpy.Describe(workspace).connectionProperties
    return str(getattr(props, "user", "") or "").upper()


def fill_blanks(arcpy, path: str, prefix: str, digits: int, dry_run: bool) -> tuple:
    """空の管理番号に番号を付ける。付けた件数と、その後のいちばん大きい番号。"""
    with arcpy.da.SearchCursor(path, ["OBJECTID", FIELD]) as cursor:
        rows = [(int(o), i) for o, i in cursor]
    plan, largest = plan_numbers(rows, prefix, digits)
    if plan and not dry_run:
        with arcpy.da.UpdateCursor(path, ["OBJECTID", FIELD]) as cursor:
            for row in cursor:
                if int(row[0]) in plan:
                    cursor.updateRow([row[0], plan[int(row[0])]])
    dup = {}
    for _o, i in rows:
        if i and i.strip():
            dup[i.strip().upper()] = dup.get(i.strip().upper(), 0) + 1
    twice = sorted(k for k, n in dup.items() if n > 1)
    if twice:
        print("  ! 管理番号が重なっている（直してからもう一度）: " + "、".join(twice[:10]))
    return len(plan), largest


def ensure_sequence(arcpy, workspace: str, start: int, dry_run: bool) -> bool:
    """シーケンスがなければ作る。作ったとき True。"""
    try:
        existing = {s.name.upper().split(".")[-1] for s in arcpy.da.ListDatabaseSequences(workspace)}
    except (AttributeError, RuntimeError):
        existing = set()
    if SEQUENCE in existing:
        return False
    if not dry_run:
        arcpy.management.CreateDatabaseSequence(workspace, SEQUENCE, start, 1)
    return True


def ensure_rules(arcpy, path: str, owner: str, prefix: str, digits: int, dry_run: bool) -> list:
    """グローバル ID と 2 つの属性ルール（ないものだけ）。足したものの名前。"""
    done = []
    d = arcpy.Describe(path)
    if not getattr(d, "hasGlobalID", False):
        done.append("グローバル ID")
        if not dry_run:
            arcpy.management.AddGlobalIDs(path)
    have = {r.name for r in getattr(arcpy.Describe(path), "attributeRules", []) or []}
    if CALC_RULE not in have:
        done.append(CALC_RULE)
        if not dry_run:
            arcpy.management.AddAttributeRule(
                path, CALC_RULE, "CALCULATION", calculation_rule(owner, prefix, digits), "EDITABLE", "INSERT;UPDATE",
                description="空の管理番号に NA-000123 の形の番号を付ける（MIKAGE）", field=FIELD,
                exclude_from_client_evaluation="EXCLUDE")
    if UNIQUE_RULE not in have:
        done.append(UNIQUE_RULE)
        if not dry_run:
            arcpy.management.AddAttributeRule(
                path, UNIQUE_RULE, "CONSTRAINT", unique_rule(owner), triggering_events="INSERT;UPDATE",
                error_number=1001, error_message="管理番号がほかの地名と重なっている",
                description="管理番号の重なりを止める（MIKAGE）")
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="地名の管理番号を自動で付ける（シーケンスと属性ルール。何度実行してもよい）")
    parser.add_argument("--workspace", required=True, help="データ所有者で接続した .sde（試すならファイル ジオデータベース .gdb も可）")
    parser.add_argument("--table", default=TABLE, help=f"地名のフィーチャクラス（既定 {TABLE}）")
    parser.add_argument("--prefix", default=PREFIX, help=f"番号の頭（既定 {PREFIX}。設定ファイルの namedAreas.idPrefix と同じに）")
    parser.add_argument("--digits", type=int, default=DIGITS, help=f"桁数（既定 {DIGITS}。namedAreas.idDigits と同じに）")
    parser.add_argument("--fill-only", action="store_true", help="空の行に番号を付けるだけ（シーケンス・属性ルールを作らない）")
    parser.add_argument("--dry-run", action="store_true", help="何をするかだけ出す（変えない）")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{0,16}", args.prefix) or not 1 <= args.digits <= 12:
        parser.error("--prefix は英数字と - _ の 16 文字まで、--digits は 1〜12")

    import arcpy  # ArcGIS Pro の Python にだけある

    path = args.workspace.rstrip("\\/") + "\\" + args.table
    if not arcpy.Exists(path):
        raise SystemExit(f"地名のフィーチャクラスがない: {path}（create_tables.py で作る）")
    owner = _owner_of(arcpy, args.workspace)
    filled, largest = fill_blanks(arcpy, path, args.prefix, args.digits, args.dry_run)
    print(f"  空の管理番号に付番: {filled} 件（いちばん大きい番号 {args.prefix}{largest:0{args.digits}d}）")
    if not args.fill_only:
        if args.workspace.lower().rstrip("\\/").endswith(".gdb"):
            print("  ! ファイル ジオデータベースにはシーケンスの属性ルールを作れない（番号を付けるだけ）")
        else:
            if ensure_sequence(arcpy, args.workspace, largest + 1, args.dry_run):
                print(f"  シーケンス {SEQUENCE} を作成（{largest + 1} から）")
            for name in ensure_rules(arcpy, path, owner, args.prefix, args.digits, args.dry_run):
                print(f"  追加: {name}")
            print("  サービスを上書き公開する（publish_service.py publish）と、プラグインの保存でもルールが番号を付ける")
    if args.dry_run:
        print("（--dry-run: 何も変えていない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
