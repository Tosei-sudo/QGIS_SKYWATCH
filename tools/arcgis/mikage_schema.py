"""MIKAGE の Esri テーブル定義（ArcGIS 側で作るテーブル・フィーチャクラスの一覧）。

README 「Esri 側のテーブル定義」と同じ内容をコードにしたもの。`create_tables.py`（ArcPy）と
`publish_service.py`（ポータルへの公開）が読む。ArcGIS Pro の Python（3.9 以降）でも、
プラグインのテスト（`mikage/tests/test_arcgis_tools.py`）でも読めるよう、標準ライブラリだけを使う。

テーブル・属性を足すときは README の表とここの両方に書く（テストが食い違いを見つける）。
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class F(NamedTuple):
    """1 属性。`type` は ArcPy の AddField の型（TEXT・DATE・DOUBLE・LONG・SHORT）。"""
    name: str
    alias: str
    type: str
    length: Optional[int] = None
    #: 設定ファイルの `fields` での役割（README の表の「役割」）。
    role: str = ""


class Table(NamedTuple):
    name: str
    title: str
    #: "POLYGON"・"POINT"、形状なしのテーブルは None。
    geometry: Optional[str]
    #: プラグイン（か tools/satellite_editor.html）が書き込むテーブル（作成者・更新者の 4 属性を足す）。
    writable: bool
    #: README の「設定ファイルの項目」。
    setting: str
    fields: tuple = ()
    #: 属性インデックスを張る属性（ID で引く属性）。
    indexes: tuple = ()


#: 作成者・更新者の 4 属性（設定ファイルの `editTracking` の既定）。書き込みのテーブルの最後に足す。
EDIT_FIELDS = (
    F("CREATED_DATE", "作成日時", "DATE", None, "createdAt"),
    F("CREATED_USER", "作成者", "TEXT", 255, "createdBy"),
    F("UPDATED_DATE", "更新日時", "DATE", None, "updatedAt"),
    F("UPDATED_USER", "更新者", "TEXT", 255, "updatedBy"),
)

TABLES = (
    Table("TBL_IMAGE_CATALOG", "画像カタログ", "POLYGON", writable=False, setting="imageCatalogs[].url", fields=(
        F("IMAGE_ID", "画像ID", "TEXT", 255, "id"),
        F("ACQUIRED_AT", "撮像日時", "DATE", None, "acquired"),
        F("REGISTERED_AT", "登録日時", "DATE", None, "registered"),
        F("SYSTEM_REGISTERED_AT", "システム登録日時", "DATE", None, "systemRegistered"),
        F("SENSOR", "センサー", "TEXT", 255, "sensor"),
        F("OFF_NADIR", "オフナディア角", "DOUBLE", None, "angle"),
        F("SAT_AZIMUTH", "衛星方位角", "DOUBLE", None, "azimuth"),
        F("FILE_PATH", "ファイルパス", "TEXT", 1000, "source"),
        F("COG_URL", "画像URL", "TEXT", 1000, "source"),
    )),
    Table("TBL_SATELLITE", "衛星カタログ", None, writable=True, setting="satelliteCatalogs[].url", fields=(
        F("SAT_NAME", "衛星名", "TEXT", 255, "name"),
        F("NORAD_ID", "NORAD番号", "TEXT", 64, "id"),
        F("TLE_LINE1", "TLE1行目", "TEXT", 255, "tle1"),
        F("TLE_LINE2", "TLE2行目", "TEXT", 255, "tle2"),
        F("MAX_OFF_NADIR", "最大オフナディア角", "DOUBLE", None, "maxOffNadir"),
        F("MIN_OFF_NADIR", "最小オフナディア角", "DOUBLE", None, "minOffNadir"),
        F("SWATH", "観測幅", "DOUBLE", None, "swath"),
        F("SCENE_LENGTH", "シーン長", "DOUBLE", None, "length"),
        F("LOOK_SIDE", "観測方向", "TEXT", 16, "lookSide"),
        F("SENSOR_TYPE", "センサー種別", "TEXT", 64, "kind"),
        F("SCENES_PER_PASS", "1パスのシーン数", "LONG", None, "scenesPerPass"),
        F("MIN_INTERVAL_MIN", "最小撮像間隔", "DOUBLE", None, "minInterval"),
        F("MAX_PER_DAY", "1日の上限", "LONG", None, "maxPerDay"),
        F("SHOTS_PER_PASS", "1パスの撮像回数", "LONG", None, "shotsPerPass"),
        F("SERIES", "シリーズ", "TEXT", 255, "series"),
        F("LEAD_TIME_H", "発注リードタイム", "DOUBLE", None, "leadTime"),
        F("SENSOR", "センサー", "TEXT", 255, "sensor"),
        F("SENSOR_MODE", "センサーモード", "TEXT", 255, "mode"),
        F("RESOLUTION_M", "分解能", "DOUBLE", None, "resolution"),
        F("SAT_SYSTEM", "衛星システム", "TEXT", 255, "system"),
    )),
    Table("TBL_SENSOR_MODE", "センサーモード", None, writable=True, setting="sensorCatalogs[].url", fields=(
        F("SENSOR", "センサー", "TEXT", 255, "sensor"),
        F("SENSOR_MODE", "センサーモード", "TEXT", 255, "mode"),
        F("SENSOR_TYPE", "センサー種別", "TEXT", 64, "kind"),
        F("MAX_OFF_NADIR", "最大オフナディア角", "DOUBLE", None, "maxOffNadir"),
        F("MIN_OFF_NADIR", "最小オフナディア角", "DOUBLE", None, "minOffNadir"),
        F("SWATH", "観測幅", "DOUBLE", None, "swath"),
        F("SCENE_LENGTH", "シーン長", "DOUBLE", None, "length"),
        F("LOOK_SIDE", "観測方向", "TEXT", 16, "lookSide"),
        F("SCENES_PER_PASS", "1パスのシーン数", "LONG", None, "scenesPerPass"),
        F("MIN_INTERVAL_MIN", "最小撮像間隔", "DOUBLE", None, "minInterval"),
        F("MAX_PER_DAY", "1日の上限", "LONG", None, "maxPerDay"),
        F("SHOTS_PER_PASS", "1パスの撮像回数", "LONG", None, "shotsPerPass"),
        F("LEAD_TIME_H", "発注リードタイム", "DOUBLE", None, "leadTime"),
        F("RESOLUTION_M", "分解能", "DOUBLE", None, "resolution"),
        F("IS_DEFAULT", "既定モード", "SHORT", None, "primary"),
    )),
    Table("TBL_SATELLITE_ALIAS", "衛星名の対応表", None, writable=True, setting="satelliteAliases.url", fields=(
        F("SAT_NAME", "衛星名", "TEXT", 255, "satellite"),
        F("ALIAS", "別名", "TEXT", 1000, "alias"),
    )),
    Table("TBL_IMAGING_REQUEST", "撮像依頼台帳", "POLYGON", writable=True, setting="requestStores[].url", fields=(
        F("REQ_ID", "依頼ID", "TEXT", 64, "id"),
        F("REQ_NAME", "名称", "TEXT", 255, "name"),
        F("MANAGEMENT_NO", "管理番号", "TEXT", 255, "managementNo"),
        F("IDENTIFICATION_NO", "識別番号", "TEXT", 255, "identificationNo"),
        F("REQUEST_GROUP", "依頼グループ", "TEXT", 255, "group"),
        F("CATEGORY", "区分", "TEXT", 16, "category"),
        F("ACHIEVE", "達成条件", "TEXT", 16, "achieve"),
        F("START_AT", "開始", "DATE", None, "start"),
        F("END_AT", "終了", "DATE", None, "end"),
        F("REPEAT", "繰り返し", "TEXT", 64, "repeat"),
        F("STATUS", "状態", "TEXT", 16, "status"),
        F("PRIORITY", "優先度", "LONG", None, "priority"),
        F("BOOST", "繰り上げ量", "LONG", None, "boost"),
        F("KIND", "種別", "TEXT", 16, "kind"),
        F("SERIES", "衛星シリーズ", "TEXT", 1000, "series"),
        F("SATELLITES", "衛星", "TEXT", 1000, "satellites"),
        F("SENSOR_MODES", "センサーモード", "TEXT", 1000, "modes"),
        F("OFF_NADIR_MIN", "オフナディア角下限", "DOUBLE", None, "offNadirMin"),
        F("OFF_NADIR_MAX", "オフナディア角上限", "DOUBLE", None, "offNadirMax"),
        F("INCIDENCE_MIN", "入射角下限", "DOUBLE", None, "incidenceMin"),
        F("INCIDENCE_MAX", "入射角上限", "DOUBLE", None, "incidenceMax"),
        F("LOOK_SIDE", "観測方向", "TEXT", 16, "lookSide"),
        F("ORBIT_DIR", "軌道方向", "TEXT", 16, "orbitDir"),
        F("RESOLUTION_MAX", "分解能", "DOUBLE", None, "resolution"),
        F("TIME_OF_DAY", "時間帯", "TEXT", 16, "timeOfDay"),
        F("CLOUD_NO_GO", "実施しない雲量", "LONG", None, "cloudNoGo"),
        F("TIMES_MIN", "1日の回数下限", "LONG", None, "timesMin"),
        F("TIMES_MAX", "1日の回数上限", "LONG", None, "timesMax"),
        F("TIMES_GAP", "回の間隔", "DOUBLE", None, "timesGap"),
        F("SYSTEMS", "優先システム", "TEXT", 1000, "systems"),
        F("SHAPE_KIND", "範囲の種類", "TEXT", 16, "shapeKind"),
        F("REQUESTER", "依頼者", "TEXT", 255, "requester"),
        F("NOTE", "メモ", "TEXT", 4000, "note"),
        F("CREATED_AT", "登録日時", "DATE", None, "created"),
        F("UPDATED_AT", "更新日時", "DATE", None, "updated"),
    )),
    Table("TBL_REQUEST_HISTORY", "撮像依頼の出力履歴", "POLYGON", writable=True, setting="requestStores[].history", fields=(
        F("RUN_ID", "出力ID", "TEXT", 64, "runId"),
        F("RUN_DATE", "出力日時", "DATE", None, "runDate"),
        F("TARGET_DAY", "対象日", "TEXT", 16, "day"),
        F("TARGET_DATE", "対象日（日付）", "DATE", None, "targetDate"),
        F("REQ_ID", "依頼ID", "TEXT", 64, "requestId"),
        F("REQ_NAME", "名称", "TEXT", 255, "name"),
        F("MANAGEMENT_NO", "管理番号", "TEXT", 255, "managementNo"),
        F("REQUESTER", "依頼者", "TEXT", 255, "requester"),
        F("REQUEST_GROUP", "依頼グループ", "TEXT", 255, "group"),
        F("SYSTEM", "システム", "TEXT", 255, "system"),
        F("SATELLITE", "衛星", "TEXT", 255, "satellite"),
        F("PRIORITY_OUT", "出力優先度", "TEXT", 64, "priority"),
        F("CLOUD", "雲量予報", "DOUBLE", None, "cloud"),
        F("SCENES", "シーン数", "LONG", None, "scenes"),
        F("RESULT", "結果", "TEXT", 64, "result"),
        F("REASON", "理由", "TEXT", 1000, "reason"),
        F("OUTPUT_FILE", "出力ファイル", "TEXT", 1000, "file"),
        F("CAPTURED", "撮像結果", "TEXT", 16, "captured"),
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("BOOST_DONE", "繰り上げ済み", "LONG", None, "boostDone"),
        F("ADOPTION", "採否", "TEXT", 16, "adoption"),
        F("ADOPTION_REASON", "採否の理由", "TEXT", 1000, "adoptionReason"),
        F("PLANNED_AT", "撮像予定日時", "DATE", None, "plannedAt"),
        F("EXTERNAL_ID", "システム側の番号", "TEXT", 255, "externalId"),
        F("ADOPTION_SOURCE", "採否の入力元", "TEXT", 1000, "adoptionSource"),
        F("MERGED_INTO", "まとめ先の依頼ID", "TEXT", 255, "mergedInto"),
        F("SHOT_ID", "撮像ID", "TEXT", 64, "shotId"),
        F("OCCURRENCE", "その日の回", "LONG", None, "occurrence"),
        F("LINK_ID", "外部連携ID", "TEXT", 32, "linkId"),
        F("PART_NO", "分担の番号", "LONG", None, "part"),
    )),
    Table("TBL_OBSERVATION", "観測記録", "POINT", writable=True, setting="observationStores[].url", fields=(
        F("OBS_ID", "記録ID", "TEXT", 64, "id"),
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("IMAGE_NAME", "画像名", "TEXT", 255, "imageName"),
        F("IMAGE_TIME", "撮像日時", "DATE", None, "imageTime"),
        F("OBJECT_ID", "モノID", "TEXT", 64, "objectId"),
        F("OBJECT_NAME", "モノの名前", "TEXT", 255, "objectName"),
        F("KIND", "種別", "TEXT", 1000, "kind"),
        F("ACTIVITY", "活動", "TEXT", 255, "activity"),
        F("PRESENT", "在不在", "SHORT", None, "present"),
        F("NOTE", "備考", "TEXT", 4000, "note"),
        F("LENGTH_M", "長さ", "DOUBLE", None, "length"),
        F("HEADING", "向き", "DOUBLE", None, "heading"),
        F("WIDTH_M", "幅", "DOUBLE", None, "width"),
        F("CONFIDENCE", "確度", "TEXT", 64, "confidence"),
        F("OBSERVER", "記録者", "TEXT", 255, "recordedBy"),
        F("UPDATED_BY", "更新者名", "TEXT", 255, "updatedBy"),
        F("CREATED_AT", "記録日時", "DATE", None, "created"),
        F("UPDATED_AT", "修正日時", "DATE", None, "updated"),
        F("DELETED_AT", "削除日時", "DATE", None, "deleted"),
    )),
    Table("TBL_OBSERVATION_AREA", "確認範囲", "POLYGON", writable=True, setting="observationStores[].areas", fields=(
        F("AREA_ID", "範囲ID", "TEXT", 64, "id"),
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("IMAGE_NAME", "画像名", "TEXT", 255, "imageName"),
        F("IMAGE_TIME", "撮像日時", "DATE", None, "imageTime"),
        F("KIND", "対象の種別", "TEXT", 1000, "kind"),
        F("NOTE", "備考", "TEXT", 4000, "note"),
        F("OBSERVER", "記録者", "TEXT", 255, "recordedBy"),
        F("CREATED_AT", "記録日時", "DATE", None, "created"),
        F("UPDATED_AT", "修正日時", "DATE", None, "updated"),
        F("DELETED_AT", "削除日時", "DATE", None, "deleted"),
    )),
    Table("TBL_TAXONOMY", "分類体系", None, writable=True, setting="observationStores[].taxonomyTable", fields=(
        F("PATH", "分類", "TEXT", 1000, "path"),
        F("LEVELS", "階層名", "TEXT", 1000, "levels"),
        F("NAME_LABEL", "名前の見出し", "TEXT", 255, "nameLabel"),
        F("MEMBERS", "個体", "TEXT", 8000, "members"),
        F("SPECS", "諸元", "TEXT", 16000, "specs"),
        F("PHOTOS", "参考写真", "TEXT", 8000, "photos"),
        F("ACTIVITIES", "活動", "TEXT", 2000, "activities"),
        F("SORT_ORDER", "並び順", "LONG", None, "order"),
    )),
    Table("TBL_ALERT_RULE", "通報の条件", None, writable=False, setting="alertReports.rules", fields=(
        F("KIND", "分類", "TEXT", 1000, "kind"),
        F("ACTIVITIES", "活動", "TEXT", 2000, "activities"),
        F("MIN_CONFIDENCE", "確度の下限", "TEXT", 64, "minConfidence"),
        F("GEOFENCE_NAME", "ジオフェンス", "TEXT", 255, "area"),
        F("ACTIVE", "有効", "SHORT", None, "active"),
        F("NOTE", "備考", "TEXT", 1000, "note"),
        F("WEIGHT", "重み", "DOUBLE", None, "weight"),
    )),
    Table("TBL_ALERT_GEOFENCE", "ジオフェンス", "POLYGON", writable=False, setting="alertReports.geofences", fields=(
        F("NAME", "名前", "TEXT", 255, "name"),
        F("ACTIVE", "有効", "SHORT", None, "active"),
        F("WEIGHT", "重み", "DOUBLE", None, "weight"),
        F("NAME_EN", "英語名", "TEXT", 255, "nameEn"),
    )),
    Table("TBL_NAMED_AREA", "地名のポリゴン", "POLYGON", writable=False, setting="alertReports.namedAreas", fields=(
        F("NAME", "地名", "TEXT", 255, "name"),
        F("KIND", "種類", "TEXT", 64, "kind"),
        F("ACTIVE", "有効", "SHORT", None, "active"),
        F("NAME_EN", "英語名", "TEXT", 255, "nameEn"),
        F("KIND_EN", "種類（英語）", "TEXT", 64, "kindEn"),
    )),
    Table("TBL_ALERT_LOG", "通報記録", None, writable=True, setting="alertReports.table", fields=(
        F("OBS_ID", "記録ID", "TEXT", 64, "obsId"),
        F("REPORT_NAME", "フォーマット名", "TEXT", 255, "report"),
        F("TEXT", "通報文", "TEXT", 4000, "text"),
        F("REPORTED_AT", "通報日時", "DATE", None, "reportedAt"),
        F("REPORTED_BY", "通報者", "TEXT", 255, "reportedBy"),
        F("BATCH_ID", "通報ID", "TEXT", 64, "batch"),
        F("MANUAL", "手動", "SHORT", None, "manual"),
    )),
    Table("TBL_ASSIGNMENT", "分担", None, writable=True, setting="assignments", fields=(
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("IMAGE_NAME", "画像名", "TEXT", 255, "imageName"),
        F("IMAGE_TIME", "撮像日時", "DATE", None, "imageTime"),
        F("ASSIGNEE", "担当者", "TEXT", 255, "assignee"),
        F("STATUS", "状態", "TEXT", 16, "status"),
        F("NOTE", "メモ", "TEXT", 2000, "note"),
        F("TAKEN_AT", "担当開始", "DATE", None, "takenAt"),
        F("DONE_AT", "完了日時", "DATE", None, "doneAt"),
    )),
    Table("TBL_HANDOVER_MEMO", "引き継ぎメモ", None, writable=True, setting="handover.table", fields=(
        F("TEXT", "本文", "TEXT", 100000, "text"),
        F("MEMO", "申し送り", "TEXT", 4000, "memo"),
        F("PERIOD_START", "期間開始", "DATE", None, "start"),
        F("PERIOD_END", "期間終了", "DATE", None, "end"),
        F("WRITTEN_AT", "作成日時", "DATE", None, "writtenAt"),
        F("WRITTEN_BY", "作成者名", "TEXT", 255, "writtenBy"),
    )),
    Table("TBL_EVENT", "イベント", "POLYGON", writable=True, setting="events.layer", fields=(
        F("EVENT_ID", "イベントID", "TEXT", 64, "id"),
        F("EVENT_NAME", "イベント名", "TEXT", 255, "name"),
        F("EVENT_KIND", "種類", "TEXT", 64, "kind"),
        F("STATUS", "状態", "TEXT", 16, "status"),
        F("START_AT", "開始", "DATE", None, "start"),
        F("END_AT", "終了", "DATE", None, "end"),
        F("NOTE", "メモ", "TEXT", 4000, "note"),
    )),
    Table("TBL_EVENT_LINK", "イベントの紐付け", None, writable=True, setting="events.links", fields=(
        F("EVENT_ID", "イベントID", "TEXT", 64, "event"),
        F("ITEM_TYPE", "項目の種類", "TEXT", 16, "type"),
        F("ITEM_ID", "項目ID", "TEXT", 128, "item"),
        F("ITEM_LABEL", "項目名", "TEXT", 1000, "label"),
        F("ITEM_TIME", "項目の日時", "DATE", None, "time"),
        F("SOURCE", "出典", "TEXT", 1000, "source"),
        F("NOTE", "メモ", "TEXT", 4000, "note"),
    )),
    Table("TBL_AI_RESULT", "外部 AI の結果", "POLYGON", writable=False, setting="aiResultSources[].url", fields=(
        F("RESULT_ID", "結果ID", "TEXT", 128, "id"),
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("CLASS", "クラス", "TEXT", 255, "class"),
        F("SCORE", "確信度", "DOUBLE", None, "score"),
        F("MODEL", "モデル", "TEXT", 255, "model"),
        F("DETECTED_AT", "検出日時", "DATE", None, "detectedAt"),
        F("OBJECT_NAME", "モノの名前", "TEXT", 255, "name"),
        F("KIND", "種別", "TEXT", 1000, "kind"),
        F("ACTIVITY", "活動", "TEXT", 255, "activity"),
        F("LENGTH_M", "長さ", "DOUBLE", None, "length"),
        F("HEADING", "向き", "DOUBLE", None, "heading"),
        F("WIDTH_M", "幅", "DOUBLE", None, "width"),
        F("NOTE", "備考", "TEXT", 4000, "note"),
    )),
    Table("TBL_DETECTION", "検出結果の転記先", "POLYGON", writable=True, setting="detectionOutputs[].url", fields=(
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("FILE_NAME", "ファイル名", "TEXT", 255, "fileName"),
        F("IMAGE_TIME", "撮像日時", "DATE", None, "imageTime"),
        F("DETECTED_AT", "検出日時", "DATE", None, "detectedAt"),
        F("SENT_AT", "転記日時", "DATE", None, "sentAt"),
        F("CLASS", "クラス", "TEXT", 255, "class"),
        F("CLASS_ID", "クラス番号", "LONG", None, "classId"),
        F("SCORE", "確信度", "DOUBLE", None, "score"),
        F("MODEL", "モデル", "TEXT", 255, "model"),
        F("STATUS", "状態", "TEXT", 16, "status"),
        F("LON", "経度", "DOUBLE", None, "lon"),
        F("LAT", "緯度", "DOUBLE", None, "lat"),
        F("AREA_M2", "面積", "DOUBLE", None, "area"),
        F("LENGTH_M", "長辺", "DOUBLE", None, "length"),
        F("WIDTH_M", "短辺", "DOUBLE", None, "width"),
        F("HEADING", "向き", "DOUBLE", None, "angle"),
        F("NOTE", "備考", "TEXT", 4000, "note"),
    )),
    Table("TBL_PLACE_NAME", "地名検索", "POINT", writable=False, setting="geocoders[]", fields=(
        F("PLACE_NAME", "地名", "TEXT", 255, ""),
        F("PLACE_NAME_KANA", "読み", "TEXT", 255, ""),
        F("PLACE_ALIAS", "別名", "TEXT", 255, ""),
    )),
    Table("TBL_COUNTRY", "国", "POLYGON", writable=False, setting="alertReports.countries", fields=(
        F("COUNTRY_CODE", "国コード", "TEXT", 8, "code"),
        F("COUNTRY_NAME", "国名", "TEXT", 255, "name"),
        F("COUNTRY_NAME_EN", "国名（英語）", "TEXT", 255, "nameEn"),
        F("ACTIVE", "有効", "SHORT", None, "active"),
    )),
    # 一覧の最後に足した（既に公開したサービスのレイヤー番号を変えないため）。
    Table("TBL_REQUEST_GROUP", "依頼グループ", None, writable=False, setting="requestStores[].groups", fields=(
        F("GROUP_NAME", "グループ名", "TEXT", 255, "name"),
        F("MEMBERS", "メンバー", "TEXT", 4000, "users"),
        F("SORT_ORDER", "表示順", "LONG", None, "order"),
        F("ACTIVE", "有効", "SHORT", None, "active"),
        F("NOTE", "メモ", "TEXT", 1000, "note"),
    )),
    # 2026-11-11 に追加（一覧の最後: 既に公開したサービスのレイヤー番号を変えないため）。
    Table("TBL_REQUEST_SHOT", "撮像の範囲", "POLYGON", writable=True, setting="requestStores[].shots", fields=(
        F("SHOT_ID", "撮像ID", "TEXT", 64, "shotId"),
        F("RUN_ID", "出力ID", "TEXT", 64, "runId"),
        F("RUN_DATE", "出力日時", "DATE", None, "runDate"),
        F("TARGET_DAY", "対象日", "TEXT", 16, "day"),
        F("TARGET_DATE", "対象日（日付）", "DATE", None, "targetDate"),
        F("SYSTEM", "システム", "TEXT", 255, "system"),
        F("SATELLITE", "衛星", "TEXT", 255, "satellite"),
        F("PASS_TIME", "撮像時刻", "DATE", None, "passTime"),
        F("OFF_NADIR", "オフナディア角", "DOUBLE", None, "offNadir"),
        F("INCIDENCE", "入射角", "DOUBLE", None, "incidence"),
        F("ORBIT_DIR", "軌道方向", "TEXT", 16, "orbitDir"),
        F("LOOK_SIDE", "観測方向", "TEXT", 16, "lookSide"),
        F("SCENES", "シーン数", "LONG", None, "scenes"),
        F("SHOT_COUNT", "撮像回数", "LONG", None, "shots"),
        F("COVERAGE", "範囲の割合", "DOUBLE", None, "coverage"),
        F("REQ_ID", "依頼ID", "TEXT", 64, "requestId"),
        F("REQ_IDS", "まとめた依頼ID", "TEXT", 4000, "requestIds"),
        F("REQ_NAMES", "まとめた依頼の名称", "TEXT", 4000, "names"),
        F("REQ_COUNT", "依頼件数", "LONG", None, "count"),
        F("OUTPUT_FILE", "出力ファイル", "TEXT", 1000, "file"),
        F("SENSOR", "センサー", "TEXT", 255, "sensor"),
        F("SENSOR_MODE", "センサーモード", "TEXT", 255, "mode"),
        F("CAPTURED", "撮像結果", "TEXT", 16, "captured"),
        F("IMAGE_ID", "画像ID", "TEXT", 255, "imageId"),
        F("UNCOUNTED_SCENES", "数えない枚数", "LONG", None, "uncounted"),
        F("UNCOUNTED_REASON", "数えない理由", "TEXT", 1000, "uncountedReason"),
    )),
    Table("TBL_IMAGING_QUOTA", "年間撮像上限", None, writable=True, setting="requestStores[].quotas", fields=(
        F("SENSOR", "センサー", "TEXT", 255, "sensor"),
        F("SENSOR_MODE", "センサーモード", "TEXT", 255, "mode"),
        F("ANNUAL_LIMIT", "年間上限", "LONG", None, "limit"),
        F("YEAR_START_MONTH", "年の始まりの月", "SHORT", None, "startMonth"),
        F("INITIAL_USED", "当初の消費枚数", "LONG", None, "initialUsed"),
        F("COUNT_FROM", "数え始める日", "DATE", None, "countFrom"),
        F("NOTE", "メモ", "TEXT", 1000, "note"),
    )),
)

#: 属性インデックス（テーブル → 属性）。ID で引く・紐付ける属性。
INDEXES = {
    "TBL_IMAGE_CATALOG": ("IMAGE_ID", "REGISTERED_AT"),
    "TBL_SATELLITE": ("SAT_NAME",),
    "TBL_SATELLITE_ALIAS": ("SAT_NAME",),
    "TBL_IMAGING_REQUEST": ("REQ_ID",),
    "TBL_REQUEST_HISTORY": ("REQ_ID", "TARGET_DAY", "TARGET_DATE", "SHOT_ID"),
    "TBL_REQUEST_SHOT": ("SHOT_ID", "TARGET_DAY"),
    "TBL_OBSERVATION": ("OBS_ID", "OBJECT_ID", "IMAGE_ID"),
    "TBL_OBSERVATION_AREA": ("AREA_ID", "IMAGE_ID"),
    "TBL_ALERT_LOG": ("OBS_ID",),
    "TBL_ASSIGNMENT": ("IMAGE_ID",),
    "TBL_EVENT": ("EVENT_ID",),
    "TBL_EVENT_LINK": ("EVENT_ID", "ITEM_ID"),
    "TBL_AI_RESULT": ("IMAGE_ID",),
    "TBL_DETECTION": ("IMAGE_ID",),
    "TBL_PLACE_NAME": ("PLACE_NAME",),
}

TABLES = tuple(t._replace(indexes=INDEXES.get(t.name, ())) for t in TABLES)
BY_NAME = {t.name: t for t in TABLES}

#: 名前を変えた既定の属性: テーブル → {今の名前: 前の名前}。前の名前だけがあるテーブルに今の名前を足すときは値を写す
#: （プラグインは今の名前があればそちらを読むので、空のままだと前の値が使われなくなる）。
#: FILE は Oracle・SQL Server の予約語（2026-11-08）。AREA は ArcSDE（ST_Geometry）が形状の面積に使う名前（2026-11-10）。
LEGACY_FIELDS = {
    "TBL_REQUEST_HISTORY": {"OUTPUT_FILE": "FILE"},
    "TBL_ALERT_RULE": {"GEOFENCE_NAME": "AREA"},
}


def all_fields(table: Table, edit_tracking: bool = True) -> tuple:
    """そのテーブルに作る属性（書き込みのテーブルは作成者・更新者の 4 属性も）。"""
    return table.fields + (EDIT_FIELDS if edit_tracking and table.writable else ())


def index_name(table: str, field: str) -> str:
    """属性インデックスの名前（30 文字以内。Oracle 12.1 以前の上限）。"""
    return ("IX_" + table[len("TBL_"):] + "_" + field)[:30]


#: 既定の属性名がない項目で、同じ役割に属性が複数あるときの順（先に書いたものから使う）。
_ROLE_ORDER = {"source": ("COG_URL", "FILE_PATH")}


def _fields_of(table: str) -> dict:
    """既定の属性名がないテーブルの `fields`（役割 → 属性名。同じ役割が複数なら配列）。"""
    out: dict = {}
    for f in BY_NAME[table].fields:
        if f.role in _ROLE_ORDER:
            out[f.role] = list(_ROLE_ORDER[f.role])
        elif f.role:
            out[f.role] = f.name
    return out


def plugin_config(urls: dict) -> dict:
    """公開したサービスの URL（テーブル名 → `…/FeatureServer/n`）から、プラグインの設定ファイルの中身を作る。

    既定の属性名のある項目は URL だけ、既定のない項目は `fields` も書く（README 「上の表の名前で作ったときの設定」と同じ形）。
    URL のないテーブルの項目は書かない。
    """
    def has(*names):
        return all(n in urls for n in names)

    config: dict = {}
    if has("TBL_IMAGE_CATALOG"):
        config["imageCatalogs"] = [{"label": "衛星画像", "url": urls["TBL_IMAGE_CATALOG"], "fields": _fields_of("TBL_IMAGE_CATALOG")}]
    if has("TBL_SATELLITE"):
        config["satelliteCatalogs"] = [{"label": "衛星", "url": urls["TBL_SATELLITE"], "fields": _fields_of("TBL_SATELLITE")}]
    if has("TBL_SENSOR_MODE"):
        config["sensorCatalogs"] = [{"url": urls["TBL_SENSOR_MODE"], "fields": _fields_of("TBL_SENSOR_MODE")}]
    if has("TBL_SATELLITE_ALIAS"):
        config["satelliteAliases"] = {"url": urls["TBL_SATELLITE_ALIAS"], "fields": _fields_of("TBL_SATELLITE_ALIAS")}
    if has("TBL_IMAGING_REQUEST"):
        store = {"url": urls["TBL_IMAGING_REQUEST"]}
        if has("TBL_REQUEST_HISTORY"):
            store["history"] = urls["TBL_REQUEST_HISTORY"]
        if has("TBL_REQUEST_GROUP"):
            store["groups"] = urls["TBL_REQUEST_GROUP"]
        if has("TBL_REQUEST_SHOT"):
            store["shots"] = urls["TBL_REQUEST_SHOT"]
        if has("TBL_IMAGING_QUOTA"):
            store["quotas"] = urls["TBL_IMAGING_QUOTA"]
        config["requestStores"] = [store]
    if has("TBL_OBSERVATION"):
        store = {"url": urls["TBL_OBSERVATION"]}
        if has("TBL_OBSERVATION_AREA"):
            store["areas"] = urls["TBL_OBSERVATION_AREA"]
        if has("TBL_TAXONOMY"):
            store["taxonomyTable"] = urls["TBL_TAXONOMY"]
        config["observationStores"] = [store]
    alerts = {key: urls[t] for key, t in (("rules", "TBL_ALERT_RULE"), ("geofences", "TBL_ALERT_GEOFENCE"),
                                          ("namedAreas", "TBL_NAMED_AREA"), ("table", "TBL_ALERT_LOG"),
                                          ("countries", "TBL_COUNTRY")) if t in urls}
    if alerts:
        alerts["formats"] = [{"name": "既定"}]
        config["alertReports"] = alerts
    if has("TBL_ASSIGNMENT"):
        config["assignments"] = urls["TBL_ASSIGNMENT"]
    if has("TBL_HANDOVER_MEMO"):
        config["handover"] = {"table": urls["TBL_HANDOVER_MEMO"]}
    if has("TBL_EVENT"):
        events = {"layer": urls["TBL_EVENT"]}
        if has("TBL_EVENT_LINK"):
            events["links"] = urls["TBL_EVENT_LINK"]
        config["events"] = events
    if has("TBL_AI_RESULT"):
        config["aiResultSources"] = [{"url": urls["TBL_AI_RESULT"], "fields": _fields_of("TBL_AI_RESULT")}]
    if has("TBL_DETECTION"):
        config["detectionOutputs"] = [{"url": urls["TBL_DETECTION"], "fields": _fields_of("TBL_DETECTION")}]
    if has("TBL_PLACE_NAME"):
        config["geocoders"] = [{"type": "features", "label": "地名", "url": urls["TBL_PLACE_NAME"],
                                "field": [f.name for f in BY_NAME["TBL_PLACE_NAME"].fields]}]
    return config
