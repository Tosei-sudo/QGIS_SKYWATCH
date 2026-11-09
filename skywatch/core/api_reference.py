"""
「Python API リファレンス」 (tsudo, 2026-11-08): what `plugin.api` has, in
Japanese, with an example each, for the Python console and Processing
scripts. The functions' arguments are read from the code itself when the
page is shown (ui/api_reference_dialog.py), so they cannot go out of date;
the texts here are checked against PluginApi by the tests (every name of it
has an entry, every member named exists).
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: How a script gets the API.
HOW_TO = ('import qgis.utils\n'
          'api = qgis.utils.plugins["skywatch"].api')


@dataclass
class Entry:
    #: The name in PluginApi (`observations`, `image_catalog` …), or a name of its own for the extra pages ("processing:…").
    name: str
    group: str
    title: str
    text: str
    example: str = ""
    #: The members of a dock / tool shown with their arguments (method or attribute names).
    members: list[str] = field(default_factory=list)


GROUPS = ["はじめに", "観測記録の関数", "ドックと道具", "設定", "プロセシング", "式の関数"]

ENTRIES: list[Entry] = [
    Entry("intro", "はじめに", "API の取り方", "Python コンソール（プラグイン ▸ Python コンソール）やプロセシングのスクリプトから使う。"
          "観測記録の関数は観測記録ドックで選んだ保存先が対象。日時は ISO 8601 の文字列（例 2026-11-08T09:00:00+09:00）。",
          HOW_TO + "\napi.observation_images()"),

    # ---- 観測記録の関数 ----
    Entry("observations", "観測記録の関数", "記録の一覧", "記録を辞書の一覧で返す。画像・モノ・種別・期間で絞れる。",
          'api.observations(kind="船舶", start="2026-10-01", end="2026-10-31")'),
    Entry("observed_things", "観測記録の関数", "モノと流れ", "モノ（同じ対象）ごとに、確認した画像と移動の流れ。", 'api.observed_things(kind="船舶")'),
    Entry("observation_images", "観測記録の関数", "記録する画像の一覧", "開いている画像と一覧に加えた画像（撮像日時順）。", "api.observation_images()"),
    Entry("observe_image", "観測記録の関数", "記録する画像を選ぶ", "以後の add_observation の対象の画像を画像 ID で選ぶ。", 'api.observe_image("d2.tif")'),
    Entry("add_observation", "観測記録の関数", "記録を追加", "経度・緯度に記録を 1 件追加。省略した種別・活動・確度は前回の値。",
          'rec = api.add_observation(139.7, 35.6, object="あおば", kind="船舶 > 護衛艦", activity="停泊")'),
    Entry("update_observation", "観測記録の関数", "記録を変更", "記録 ID の記録の種別・活動・確度・備考・位置・長さなどを変える（名前は rename_observed_thing）。",
          'api.update_observation(rec["id"], note="直した", confidence="確定")'),
    Entry("rename_observed_thing", "観測記録の関数", "モノの名前を変える", "モノ ID のモノの名前を、すべての画像の記録で変える。",
          'api.rename_observed_thing(rec["objectId"], "あおば（DDH）")'),
    Entry("delete_observations", "観測記録の関数", "記録を削除", "記録 ID の一覧を削除（保存先が対応していれば削除済みとして残る）。", 'api.delete_observations([rec["id"]])'),
    Entry("clear_observations", "観測記録の関数", "ローカルの記録を消去", "ローカルの保存先の記録を完全に消す（deleted_only=True で削除済みだけ）。",
          "api.clear_observations(deleted_only=True)"),
    Entry("observation_candidates", "観測記録の関数", "同一候補", "その位置・種別・長さで、前の画像の同じモノの候補。", 'api.observation_candidates(139.7, 35.6, kind="船舶")'),
    Entry("confidences", "観測記録の関数", "確度の段階", "設定ファイルの確度の段階（名前・色・説明）。", "api.confidences()"),
    Entry("checked_areas", "観測記録の関数", "確認範囲の一覧", "画像ごとの確認した範囲。", 'api.checked_areas(image="d2.tif")'),
    Entry("add_checked_area", "観測記録の関数", "確認範囲を追加", "矩形（bbox）か多角形で確認範囲を追加。", 'api.add_checked_area(bbox=[139.69, 35.59, 139.72, 35.62], kind="船舶")'),
    Entry("delete_checked_areas", "観測記録の関数", "確認範囲を削除", "確認範囲 ID の一覧を削除。", 'api.delete_checked_areas(["a1"])'),
    Entry("observation_report", "観測記録の関数", "報告書", "期間・種別の記録を報告書（地図つき HTML）にする。", 'api.observation_report(title="週報", save="/tmp/週報.html")'),

    # ---- ドックと道具 ----
    Entry("observation_dock", "ドックと道具", "観測記録ドック", "ドックそのもの。記録の本体は book（記録・画像・分類体系）。一括取込みは open_import()。",
          "dock = api.observation_dock\nlen(dock.book.records)",
          ["book", "save_csv", "to_layers", "set_observer", "open_export", "open_import", "bulk_edit", "views"]),
    Entry("image_catalog", "ドックと道具", "画像カタログ", "画像の検索・一覧・開く。", "api.image_catalog.search()\n[r.id for r in api.image_catalog.records]",
          ["search", "read_more", "records", "choose", "open_records", "find_similar", "add_layer", "views"]),
    Entry("notices", "ドックと道具", "通知（新着画像）", "見張りの条件と新着画像。", "api.notices.watch_point(139.7, 35.6)", ["store", "check", "watch_point"]),
    Entry("imaging_plan", "ドックと道具", "撮像計画", "地点を撮像できる衛星と時刻の計算。", "api.imaging_plan.set_point(139.7, 35.6)\napi.imaging_plan.run()",
          ["plan", "shown", "run", "set_point", "csv_text", "add_layer", "to_requests"]),
    Entry("moving_targets", "ドックと道具", "移動目標撮像計画", "点のレイヤー（なければ選択中のレイヤー）の移動目標の撮像計画を開く。", "dialog = api.moving_targets()"),
    Entry("satellite_pass", "ドックと道具", "衛星パス", "衛星の軌跡と時刻。", "api.satellite_pass.draw()", ["tracks", "choose", "set_time", "draw", "add_layer"]),
    Entry("requests", "ドックと道具", "撮像依頼", "依頼台帳の一覧・編集・レイヤーとの出し入れ。", "[r.name for r in api.requests.requests]",
          ["requests", "reload", "edit", "import_layer", "export_layer", "from_plan", "views"]),
    Entry("plan_files", "ドックと道具", "撮像計画ファイル出力", "依頼から撮像計画ファイルを作る。", "api.plan_files.run()", ["run", "plan", "write", "paths"]),
    Entry("schedule", "ドックと道具", "撮像予定", "日ごとの撮像計画（出力履歴）: システムの採否・要求外の計画。カレンダーで日を選び、地図に出す。",
          "from datetime import date\napi.schedule.set_day(date.today())\n[(r.name, r.adoption) for r in api.schedule.rows]",
          ["rows", "set_day", "reload", "answer", "import_file", "to_layer"]),
    Entry("image_enhance", "ドックと道具", "画像補正", "レイヤーの色調補正と着色。", 'api.image_enhance.enhance(layer, "satellite", bands=[4, 3, 2])',
          ["enhance", "pipeline", "set_pipeline", "apply", "colorize"]),
    Entry("shadow_height", "ドックと道具", "影から高さ", "影の長さと太陽の高さから高さを測る。", "api.shadow_height.set_sun(45, 180)",
          ["measurements", "measure", "set_sun", "set_time", "clear", "add_layer"]),
    Entry("coordinates", "ドックと道具", "座標・地名へ移動", "座標や地名を入れて地図を移動。", 'api.coordinates.jump("35.6, 139.7")', ["jump", "search", "go_to_place", "set_notation"]),
    Entry("spectral", "ドックと道具", "スペクトル", "点・範囲のバンドの値。", "api.spectral.save_csv('/tmp/spectral.csv')",
          ["series", "labels", "add_point", "add_box", "csv_text", "save_csv", "clear"]),
    Entry("dashboard", "ドックと道具", "ダッシュボード", "レイヤーの集計と絞り込み。", "api.dashboard.click_value('船舶')\napi.dashboard.clear_filter()",
          ["figures", "categories", "click_value", "click_bin", "click_step", "set_heatmap", "clear_filter", "set_trend", "trends", "surge_regions"]),
    Entry("moving", "ドックと道具", "移動体（AIS・ADS-B）", "移動体のフィードと履歴。", "api.moving.go_live()", ["connect_feed", "open_history", "layers", "align_to_image", "go_live"]),
    Entry("events", "ドックと道具", "イベント", "イベントと画像・記録・依頼の紐付け。", "api.events.reload()", ["events", "links", "reload", "link_items", "choose", "export"]),
    Entry("analysis", "ドックと道具", "データ分析", "Esri の表とプロジェクトのレイヤーを SQL で集計。保存した分析はプロセシングのツールにもなる（algorithm_id）。",
          'a = api.analysis\na.load("モデル別")\na.set_value("model", "yolo")\na.run(lambda result: print(result.rows[:5]))',
          ["tables", "set_sql", "set_value", "run", "result", "save", "load", "add_layer", "csv_text", "algorithm_id", "open_processing"]),
    Entry("ai_results", "ドックと道具", "AI結果", "画像の AI の結果の一覧と観測記録への取り込み。", 'api.ai_results.load("IMG0")',
          ["load", "rows", "selected_rows", "import_rows", "zoom_to", "review_queue", "next_to_review", "accept_rows", "reject_rows", "open_training"]),
    Entry("swipe", "ドックと道具", "スワイプ比較", "上のレイヤーをスワイプで比べる。", "api.swipe.set_active(True)\napi.swipe.set_position(0.3)",
          ["set_active", "set_target", "set_position", "set_orientation"]),
    Entry("shift", "ドックと道具", "位置ずらし", "レイヤーの表示位置をメートルでずらす。", "api.shift.set_shift_metres(layer, 12, -3)", ["set_active", "set_shift_metres", "shift_metres", "reset"]),
    Entry("segment", "ドックと道具", "クリック抽出（AI）", "クリックした物の輪郭を AI で抽出。", "api.segment.set_active(True)",
          ["set_active", "add_model_files", "click", "undo", "keep", "outlines"]),
    Entry("new_scratch_layer", "ドックと道具", "新規スクラッチレイヤー", "テンプレートから一時レイヤーを作るダイアログ（create() で作成）。",
          'api.new_scratch_layer("目標・物体").create()'),

    # ---- 設定 ----
    Entry("config", "設定", "設定ファイル", "読み込んだ設定ファイル（PluginConfig）。", "[c.label for c in api.config.image_catalogs]"),
    Entry("problems", "設定", "設定ファイルの問題", "設定ファイルを読んだときの問題（文字列の一覧）。", "api.problems"),
    Entry("reload_config", "設定", "設定ファイルを読み直す", "設定ファイルを読み直し、各ドックに反映。", "api.reload_config()"),
    Entry("startup_times", "設定", "起動と先読みの所要時間", "起動の各段階（steps）と合計（total）、先読みした表ごと（reads）の秒数。"
          "同じ内容はログメッセージの「SKYWATCH」タブにも出る。", 'api.startup_times["reads"]'),
    Entry("preload", "設定", "先読みし直す", "設定ファイルの preload に書いた表を、いま裏で読み直す（衛星カタログ・撮像依頼など）。", "api.preload()"),

    # ---- プロセシング（plugin.api の外） ----
    Entry("processing:analysis", "プロセシング", "保存した分析のツール", "プロジェクトに保存した分析は「SKYWATCH ▸ 保存した分析」のツール。"
          "ID は api.analysis.algorithm_id(名前)。:名前 の値は同じ名前の入力。Esri の表を読む分析は Python コンソールではなく処理ツールボックス・"
          "モデラー・バッチ処理から実行する（通信はバックグラウンドのみ）。",
          'import processing\nprocessing.run(api.analysis.algorithm_id("港ごとの AI 件数"),\n               {"model": "yolo", "OUTPUT": "/tmp/ports.gpkg"})'),
    Entry("processing:tools", "プロセシング", "そのほかのツール", "パンシャープン・変化抽出・位置合わせ・分類・簡易オルソ・測地線・雲量・物体検出・学習なども"
          "「SKYWATCH」のツール（ID は skywatch:…、処理ツールボックスで確認）。",
          'import processing\nprocessing.algorithmHelp("skywatch:cloud_cover")'),

    # ---- 式の関数 ----
    Entry("expression:records", "式の関数", "記録の件数・モノ", "フィールド計算機・ラベル・スタイル・式による選択で使える（グループ「SKYWATCH」）。"
          "観測記録ドックが読んだ記録が対象。",
          "skywatch_record_count(\"IMG_ID\", '船舶')\narray_to_string(skywatch_object_names(\"IMG_ID\"), '、')\n"
          "skywatch_last_seen(\"OBJ_ID\")\nskywatch_last_activity(\"OBJ_ID\")"),
    Entry("expression:kinds", "式の関数", "種別の階層", "種別の 1 つの階層（1 が大分類、-1 が最も下）。", "skywatch_kind_level(\"KIND\", 1)"),
    Entry("expression:places", "式の関数", "地名", "図形を含む地名（設定ファイルの namedAreas。起動時と 10 分ごとに読む）。",
          "skywatch_place($geometry)\narray_to_string(skywatch_places($geometry), '・')"),
]


def entry(name: str):
    return next((e for e in ENTRIES if e.name == name), None)
