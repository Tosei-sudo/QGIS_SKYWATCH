"""SKYWATCH のテーブルをポータル（ArcGIS Enterprise）にフィーチャサービスとして公開し、プラグインの設定ファイルを書き出す。

ArcGIS Pro の Python で実行する（ArcPy で公開し、ArcGIS API for Python でサービスを確かめる）。
`create_tables.py` でテーブルを作ったあとに使う。

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" publish_service.py publish ^
        --workspace C:\\gis\\connections\\skywatch_publisher.sde ^
        --aprx C:\\gis\\skywatch_publish\\skywatch_publish.aprx ^
        --portal https://gis.example/portal --server https://gis.example/server ^
        --service SKYWATCH --groups SKYWATCH利用者 --config C:\\gis\\skywatch.config.json

    rem 公開済みのサービスから設定ファイルだけ作る
    propy.bat publish_service.py config --url https://gis.example/server/rest/services/SKYWATCH/FeatureServer ^
        --portal https://gis.example/portal --config C:\\gis\\skywatch.config.json

前提:
- ジオデータベースがフェデレーションしたサーバーにデータストアとして登録してある（参照で公開するため。データはコピーしない）
- `--aprx` は空のプロジェクト（ArcGIS Pro で「新しいプロジェクト」→ 保存したもの）。スクリプトは中に地図
  `SKYWATCH_SERVICE` を作り直して使う（ほかの地図には触れない）
- `--user` を書かなければ ArcGIS Pro でサインインしているポータル・アカウントを使う

何度実行してもよい: 同じ名前のサービスは上書きする。レイヤー番号はテーブルの一覧の順（0 = TBL_IMAGE_CATALOG …）に固定する。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from xml.dom import minidom

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skywatch_schema as schema  # noqa: E402

MAP_NAME = "SKYWATCH_SERVICE"
#: フィーチャ アクセスの機能（プラグインは追加・更新・削除をする）。
CAPABILITIES = "Query,Create,Update,Delete,Uploads,Editing"


# --- サービス定義ドラフト（.sddraft）の書き換え（ArcPy なしで動く。テストする） -------------------------

def _element_children(node, tag=None):
    return [c for c in node.childNodes if c.nodeType == c.ELEMENT_NODE and (tag is None or c.tagName == tag)]


def _text(node) -> str:
    return "".join(c.data for c in node.childNodes if c.nodeType == c.TEXT_NODE).strip()


def _set_text(doc, node, value: str):
    for c in list(node.childNodes):
        node.removeChild(c)
    node.appendChild(doc.createTextNode(value))


def _set_property(doc, info, key: str, value: str):
    """`<Info>`・`<Props>` の PropertyArray の Key の値を value に（なければ足す）。"""
    arrays = _element_children(info, "PropertyArray")
    if not arrays:
        return
    array = arrays[0]
    for prop in _element_children(array, "PropertySetProperty"):
        keys = _element_children(prop, "Key")
        if keys and _text(keys[0]) == key:
            values = _element_children(prop, "Value")
            if values:
                _set_text(doc, values[0], value)
            return
    prop = doc.createElement("PropertySetProperty")
    prop.setAttribute("xsi:type", "typens:PropertySetProperty")
    k = doc.createElement("Key")
    k.appendChild(doc.createTextNode(key))
    v = doc.createElement("Value")
    v.setAttribute("xsi:type", "xs:string")
    v.appendChild(doc.createTextNode(value))
    prop.appendChild(k)
    prop.appendChild(v)
    array.appendChild(prop)


def enable_feature_access(xml_text: str, capabilities: str = CAPABILITIES, max_records: int = 0) -> str:
    """マップ イメージのサービス定義ドラフトで、フィーチャ アクセス（FeatureServer）を有効にし、編集の機能を付ける。"""
    doc = minidom.parseString(xml_text.encode("utf-8"))
    found = False
    for type_name in doc.getElementsByTagName("TypeName"):
        if _text(type_name) != "FeatureServer":
            continue
        found = True
        extension = type_name.parentNode
        for child in _element_children(extension):
            if child.tagName == "Enabled":
                _set_text(doc, child, "true")
            elif child.tagName == "Info":
                _set_property(doc, child, "WebCapabilities", capabilities)
            elif child.tagName == "Props":
                _set_property(doc, child, "allowGeometryUpdates", "true")
                if max_records:
                    _set_property(doc, child, "maxRecordCount", str(max_records))
    if not found:
        raise ValueError("サービス定義ドラフトに FeatureServer の拡張がない")
    if max_records:
        # マップ サービス側の上限（フィーチャ サービスの既定はこちらに従う）
        for config in doc.getElementsByTagName("ConfigurationProperties"):
            _set_property(doc, config, "maxRecordCount", str(max_records))
    return doc.toxml()


# --- 公開したサービスの読み取り ---------------------------------------------------------------------

def layer_urls(service_url: str, service_json: dict) -> tuple[dict, list[str]]:
    """サービスの JSON（`…/FeatureServer?f=json`）から、テーブル名 → レイヤーの URL。名前の合わないレイヤーは別に返す。"""
    base = service_url.rstrip("/")
    urls, unknown = {}, []
    for item in list(service_json.get("layers") or []) + list(service_json.get("tables") or []):
        name = str(item.get("name") or "")
        key = name.upper().rsplit(".", 1)[-1]  # OWNER.TBL_X の形でも
        if key in schema.BY_NAME:
            urls[key] = f"{base}/{item['id']}"
        else:
            unknown.append(name)
    return urls, unknown


def write_config(path: str, urls: dict) -> dict:
    """プラグインの設定ファイル（JSON）を書く。"""
    config = schema.plugin_config(urls)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return config


def _gis(args):
    from arcgis.gis import GIS  # ArcGIS Pro の Python にある ArcGIS API for Python
    if args.user:
        password = args.password or os.environ.get("SKYWATCH_PORTAL_PASSWORD") or getpass.getpass(f"{args.user} のパスワード: ")
        return GIS(args.portal, args.user, password, verify_cert=not args.no_verify)
    return GIS("pro")


def service_json(gis, url: str) -> dict:
    from arcgis.features import FeatureLayerCollection
    flc = FeatureLayerCollection(url, gis)
    props = flc.properties
    return {
        "layers": [{"id": layer["id"], "name": layer["name"]} for layer in props.get("layers", [])],
        "tables": [{"id": table["id"], "name": table["name"]} for table in props.get("tables", [])],
    }


# --- 公開（ArcPy） ---------------------------------------------------------------------------------

def _build_map(arcpy, aprx, workspace: str, tables) -> object:
    for m in aprx.listMaps(MAP_NAME):
        aprx.deleteItem(m)
    m = aprx.createMap(MAP_NAME)
    for index, table in enumerate(tables):
        path = os.path.join(workspace, table.name)
        if not arcpy.Exists(path):
            raise SystemExit(f"{path} がない（先に create_tables.py を）")
        if arcpy.Describe(path).dataType == "FeatureClass":
            item = m.addDataFromPath(path)
        else:
            item = m.addTable(arcpy.mp.Table(path))
        item.name = table.name
    _fix_layer_ids(m, tables)
    return m


def _fix_layer_ids(m, tables):
    """レイヤー番号をテーブルの一覧の順に固定する（「一意の数値 ID の割り当てを許可」）。"""
    order = {t.name: i for i, t in enumerate(tables)}
    try:
        cim = m.getDefinition("V3")
        cim.useServiceLayerIDs = True
        m.setDefinition(cim)
        for item in list(m.listLayers()) + list(m.listTables()):
            if item.name not in order:
                continue
            d = item.getDefinition("V3")
            if hasattr(d, "serviceTableID"):  # 単独のテーブル
                d.serviceTableID = order[item.name]
            else:
                d.serviceLayerID = order[item.name]
            item.setDefinition(d)
    except Exception as e:  # noqa: BLE001 - 固定できなくても公開はでき、設定ファイルは実際の番号で書く
        print(f"  ! レイヤー番号を固定できない（設定ファイルは公開後の実際の番号で書く）: {e}")


def publish(args) -> int:
    import arcpy

    tables = [schema.BY_NAME[n] for n in args.tables] if args.tables else [t for t in schema.TABLES if t.name not in args.skip]
    if args.user:
        password = args.password or os.environ.get("SKYWATCH_PORTAL_PASSWORD") or getpass.getpass(f"{args.user} のパスワード: ")
        arcpy.SignInToPortal(args.portal, args.user, password)
        args.password = password
    aprx = arcpy.mp.ArcGISProject(args.aprx)
    print(f"地図 {MAP_NAME} に {len(tables)} テーブルを入れる")
    m = _build_map(arcpy, aprx, args.workspace, tables)
    aprx.save()

    work = tempfile.mkdtemp(prefix="skywatch_publish_")
    sddraft = os.path.join(work, args.service + ".sddraft")
    sd = os.path.join(work, args.service + ".sd")
    draft = m.getWebLayerSharingDraft("FEDERATED_SERVER", "MAP_IMAGE", args.service)
    draft.federatedServerUrl = args.server
    draft.copyDataToServer = False
    draft.overwriteExistingService = True
    if args.folder:
        draft.serverFolder = args.folder
        draft.portalFolder = args.folder
    draft.summary = "SKYWATCH（衛星画像の観測）のテーブル"
    draft.tags = "SKYWATCH"
    draft.exportToSDDraft(sddraft)
    with open(sddraft, encoding="utf-8") as f:
        xml_text = f.read()
    with open(sddraft, "w", encoding="utf-8") as f:
        f.write(enable_feature_access(xml_text, max_records=args.max_records))

    print("サービス定義を作る")
    try:
        arcpy.server.StageService(sddraft, sd)
    except Exception:
        print(arcpy.GetMessages())
        print("ヒント: 「データがサーバーに登録されていない」なら、ジオデータベースをフェデレーションしたサーバーのデータストアに登録する")
        raise
    print("公開する")
    arcpy.server.UploadServiceDefinition(
        sd, args.server, in_override="OVERRIDE_DEFINITION", in_my_contents="SHARE_ONLINE", in_public="PRIVATE",
        in_organization="SHARE_ORGANIZATION" if args.share_org else "NO_SHARE_ORGANIZATION",
        in_groups=args.groups or None)
    url = args.server.rstrip("/") + "/rest/services/" + (args.folder + "/" if args.folder else "") + args.service + "/FeatureServer"
    print(f"公開した: {url}")
    args.url = url
    return write_config_from_service(args)


def write_config_from_service(args) -> int:
    gis = _gis(args)
    urls, unknown = layer_urls(args.url, service_json(gis, args.url))
    for name in unknown:
        print(f"  ! テーブルの一覧にないレイヤー（設定には書かない）: {name}")
    missing = [t.name for t in schema.TABLES if t.name not in urls]
    if missing:
        print("  サービスにないテーブル（その項目は設定に書かない）: " + ", ".join(missing))
    if args.config:
        write_config(args.config, urls)
        print(f"設定ファイルを書いた: {args.config}")
    else:
        print(json.dumps(schema.plugin_config(urls), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SKYWATCH のフィーチャサービスを公開し、プラグインの設定ファイルを作る")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--portal", required=True, help="ポータルの URL（https://gis.example/portal）")
        p.add_argument("--user", default="", help="ポータルのユーザー（省略すると ArcGIS Pro のサインインを使う）")
        p.add_argument("--password", default="", help="パスワード（省略すると環境変数 SKYWATCH_PORTAL_PASSWORD か入力）")
        p.add_argument("--no-verify", action="store_true", help="証明書を確かめない（社内の自己署名証明書のとき）")
        p.add_argument("--config", default="", help="書き出すプラグインの設定ファイル（省略すると画面に出す）")

    p = sub.add_parser("publish", help="公開して設定ファイルを作る")
    common(p)
    p.add_argument("--workspace", required=True, help="公開に使う .sde（サーバーに登録したものと同じデータベース）")
    p.add_argument("--aprx", required=True, help="公開に使う ArcGIS Pro のプロジェクト（空のもの）")
    p.add_argument("--server", required=True, help="フェデレーションしたサーバーの URL（https://gis.example/server）")
    p.add_argument("--service", default="SKYWATCH", help="サービス名（既定 SKYWATCH）")
    p.add_argument("--folder", default="", help="サーバー・ポータルのフォルダー")
    p.add_argument("--tables", nargs="*", default=[], help="公開するテーブルだけ（既定はすべて）")
    p.add_argument("--skip", nargs="*", default=[], help="公開しないテーブル")
    p.add_argument("--groups", nargs="*", default=[], help="共有するポータルのグループ")
    p.add_argument("--share-org", action="store_true", help="組織に共有する")
    p.add_argument("--max-records", type=int, default=0, help="1 回の問い合わせで返す上限（既定はサーバーの既定 2000）")

    p = sub.add_parser("config", help="公開済みのサービスから設定ファイルだけ作る")
    common(p)
    p.add_argument("--url", required=True, help="サービスの URL（…/FeatureServer）")

    args = parser.parse_args(argv)
    for name in getattr(args, "tables", []) + getattr(args, "skip", []):
        if name not in schema.BY_NAME:
            parser.error(f"知らないテーブル: {name}")
    return publish(args) if args.command == "publish" else write_config_from_service(args)


if __name__ == "__main__":
    sys.exit(main())
