# MIKAGE（QGIS プラグイン）

**MIKAGE**（ミカゲ）は **MI**ssion **K**it for **A**pplied **GE**ospatial-intelligence の略。地理空間情報（GEOINT）を実際の任務に使うための道具一式、という意味。

衛星画像の判読業務を QGIS でまとめて行うプラグイン。撮像の計画と依頼から、届いた画像の検索・補正・解析、画像に写ったモノの観測記録・分担・通報・エクスポートまでを扱う。Web 画像ビューア（[browser-image-enhancement](https://github.com/Tosei-sudo/browser-image-enhancement) の `usecase/image-viewer`）の「撮像計画」「観測記録」「画像補正」を移したところから始まり、同じ設定ファイル（`config.json`）と同じ ArcGIS フィーチャーサービスを共用する。

- 撮像計画: 衛星カタログ（TLE と諸元）から撮像機会・組み合わせ・優先度、衛星パス、移動目標撮像計画、ステレオ・干渉ペア、再訪間隔マップ、撮像機会から臨時依頼の登録、雲量予報・雲量マップ（閉域では GFS のファイル）
- 撮像依頼: 定点・臨時の依頼台帳と日次の撮像計画ファイル出力、日ごとの撮像予定（システムの採否・要求外）
- 画像: 画像カタログ（検索・フットプリント・新着通知・類似画像・フォルダのレコード）、ピラミッド（.ovr）の自動作成、複素 SAR（SICD）の振幅表示、画像補正（色調補正）、スワイプ比較・位置ずらし・クイック幾何補正（地形（DEM）と目印の点で SAR 画像を正しい位置へ）、影から高さ・スペクトル
- 解析・処理: データ分析（Esri の表とプロジェクトのレイヤーを SQL で結合・集計）、パンシャープン・変化抽出・自動位置合わせ・簡易オルソ補正・土地被覆分類・測地線バッファ、サーバ処理、雲量を属性に付与（Processing にも同じものがある）
- 観測記録: モノの位置・種別・活動を画像ごとに記録して画像をまたいで追う（2〜4 点入力・確認範囲・一括編集）、分類体系の編集（表・Excel からの取り込みと表への書き出しも）、分担と引き継ぎメモ、イベント（地震対応など）、通報（条件・ジオフェンス・地名・通報記録）、観測記録のエクスポート（CSV・JSON）、作成者・更新者の記録、ダッシュボード
- AI: 物体検出・クリック抽出・教師データセット・アノテーション変換（点→矩形）・AI 学習（物体検出）、外部 AI とプラグイン内の検出結果の取り込み
- そのほか: 移動体（AIS・ADS-B）、座標・地名へ移動、背景地図・ArcGIS Web マップ、新規スクラッチレイヤー（テンプレート）、ドックのレイアウト、最初の設定（担当と個人設定）、社内のプラグインリポジトリでの更新配布

- 対応: QGIS 3.34 LTR 以上（Qt5 / Qt6）
- 追加の依存: `sgp4`（任意。なければ同梱の純 Python 版で動く。入れると約 15 倍速い）、`onnxruntime`（AI の推論だけに必要。AI 学習は追加なし）、`onnxruntime-genai`（文章の手伝いだけに必要）。numpy は QGIS 付属のもの。Windows 版の zip にはすべて同梱（`timeZone` の IANA 名に使う `tzdata` も）
- 閉域ネットワークで動作（外部の暦ファイル・地図タイルを使わない）
- 名前: 表示名は「MIKAGE」、プラグインのフォルダー名・ID は `mikage`（意味は冒頭のとおり）。2026-11-12 に「SKYWATCH」・`skywatch` から、その前は 2026-11-08 に「衛星撮像計画・観測記録」・`satellite_observation` から変更。古い版（`skywatch`・`satellite_observation`）を入れた PC は、先にそれをアンインストールしてから入れる。設定・ドックのレイアウトなどは引き継がれない

ビューアとの違いは [docs/differences.md](docs/differences.md)。

## 説明の一覧

機能ごとの説明は docs/ の下に分けてある（1 つの機能の改修で 1 つのファイルだけを直すため）。

| ファイル | 内容 |
| --- | --- |
| [画面・メニュー・担当](docs/features/screen.md) | メニュー、使い方、ツールバーのボタン、ドックの配置、最初の設定、担当と使える機能、練習モード、一覧のビュー、キーボードショートカット、入力の初期値の記憶 |
| [画像カタログ・通知](docs/features/images.md) | 画像カタログ、通知 |
| [撮像計画](docs/features/plan.md) | 撮像計画、移動目標撮像計画、衛星パス、雲量マップ・雲量を属性に付与 |
| [撮像依頼](docs/features/requests.md) | 撮像依頼と日次出力 |
| [観測記録](docs/features/observations.md) | 観測記録、イベント、分類体系の編集、ダッシュボード、移動体 |
| [画像補正と画像の道具](docs/features/enhance.md) | 画像補正、影から高さ、スペクトル、スワイプ比較、位置ずらし、クイック幾何補正 |
| [解析・処理](docs/features/analysis.md) | データ分析、QGIS の式の MIKAGE 関数、測地線バッファ・正距方位での重心、サーバ処理 |
| [AI](docs/features/ai.md) | AI、AI 学習、AI結果 |
| [地図・レイヤー](docs/features/map.md) | 背景地図、新規スクラッチレイヤー、ArcGIS Web マップ、座標・地名へ移動、外部から図形を受け取る、起動時のレイヤー |
| [運用（タイムゾーン・オフライン・記録・先読み）](docs/admin/operations.md) | 日時のタイムゾーン、サーバー障害時のオフライン作業、複数人で同じ保存先を使うとき、作成者・更新者の記録、監査ログ、先読み、裏の処理中の画面のなめらかさ |
| [導入と設定](docs/admin/install.md) | 導入、設定、接続と表の点検 |
| [設定ファイルの書き方](docs/admin/config-file.md) | 設定ファイルの書き方 |
| [Esri 側のテーブル定義](docs/admin/esri-tables.md) | Esri 側のテーブル定義 |
| [エラーコード一覧（管理者向け）](docs/admin/error-codes.md) | 画面のエラーコードごとの原因・対処と、管理者が確かめること（ログ・監査ログの `detail.code`） |
| [構成と依存のきまり](docs/architecture.md) | 改修する人向け: 層、機能の登録票、窓口と通知、機能の api、データ窓口（リポジトリ）、設定の一覧表、大きなドックの部分、機能ごとの小さな組み立てのテスト、構成テストの台帳 |

## Python API

Python コンソールから `plugin = qgis.utils.plugins["mikage"]` で取り、`plugin.api` を使う。メニューの「Python API リファレンス…」で、ここにあるものを日本語の説明・引数（コードから読む）・例つきで引ける（例はコピーしてコンソールに貼る。プロセシングのツール・式の関数の使い方も）。観測記録はビューアのスクリプトと同じ関数を snake_case で持つ（観測記録ドックで選んだ保存先が対象）。

```python
api = plugin.api
api.observation_images()                                   # 画像の一覧（撮像日時順）
api.observe_image("d2.tif")                                # 記録する画像
rec = api.add_observation(139.7, 35.6, object="あおば", kind="船舶 > 護衛艦", activity="停泊")
api.update_observation(rec["id"], note="直した")
api.observed_things(kind="船舶")                            # モノと流れ
api.observations(start="2026-10-01", end="2026-10-31")     # 記録（時刻は ISO 文字列）
api.observation_candidates(139.7, 35.6, kind="船舶")        # 同一候補
api.add_checked_area(bbox=[139.69, 35.59, 139.72, 35.62], kind="船舶")
api.observation_report(title="週報", save="/tmp/週報.html") # 報告書（地図つき）
```

ほかに `rename_observed_thing`・`delete_observations`・`clear_observations(deleted_only=False)`（ローカルのみ、完全に消去）・`confidences`・`checked_areas`・`delete_checked_areas`。ドックそのものは `api.observation_dock`（`save_csv(path)`・`to_layers()`・`set_observer(name)`）。画像カタログは `api.image_catalog`（`search()`・`read_more()`（続きを読む）・`records`・`choose()`・`open_records()`・`find_similar()`・`add_layer()`）、通知は `api.notices`（`check()`・`watch_point(lon, lat)`・`store()`）、座標・地名へ移動は `api.coordinates`（`jump(text)`・`search.search(text)`・`go_to_place(place)`・`set_notation("mgrs")`）、スペクトルは `api.spectral`（`add_point(point)`・`add_box(rect)`・`series`・`save_csv(path)`）、ダッシュボードは `api.dashboard`（`figures`・`categories`・`click_value(value)`・`click_bin(i)`・`click_step(i)`・`set_heatmap(True)`・`clear_filter()`・`set_trend("grid" | "group" | レイヤー ID, "day" | "week" | "month")`・`trends`・`surge_regions()`）、スワイプ比較は `api.swipe`（`set_active(True)`・`set_target(layer)`・`set_position(0.3)`・`set_orientation("horizontal")`）、位置ずらしは `api.shift`（`set_shift_metres(layer, 12, -3)`・`shift_metres(layer)`・`reset()`）、クイック幾何補正は `api.geo_fix`（`set_active(True)`・`take(layer)`・`set_look(270, 35)`・`add_point(start, end)`・`undo()`・`save()`・`take_off()`）、影から高さは `api.shadow_height`（`set_sun(45, 180)`・`set_time(ms)`・`measurements`・`add_layer()`）、イベントは `api.events`（`events`・`links`・`reload()`・`link_items(event_ids, items)`・`choose(id)`・`export(path)`）、AI結果は `api.ai_results`（`load(image_id)`・`rows`・`selected_rows()`・`import_rows(rows)`・`zoom_to(rows)`・`review_queue()`・`next_to_review()`・`accept_rows(rows)`・`reject_rows(rows)`・`open_training()`）、新規スクラッチレイヤーは `api.new_scratch_layer("目標・物体")`（組織のものは `api.new_scratch_layer("巡回記録", "config")`。`create()` で作成）。

## 構成

```
mikage/
  plugin.py      機能の登録票を並べて起動・終了する（plugin_api.py が plugin.api）
  features/      機能ごとの登録票（ドック・メニュー・右クリック・先読み・つなぎ）
  core/      純 Python（Qt / QGIS に依存しない）: 設定・Esri・GeoPackage・撮像計画・観測記録・AI の前後処理（NumPy）
    <機能>/api.py  機能（ai・catalog・enhance・obs・plan・request）の外向けの窓口。外からはここだけを import する
    request/repository.py・obs/repository.py  Esri の表の読み書きの窓口（リポジトリ）。sql.py が SQL の日時の書き方
  ext/       同梱の純 Python 版 sgp4（Windows 版の zip では win64/ に onnxruntime・onnxruntime-genai と C++ 版 sgp4 と tzdata も）
  qgis_io/   QGIS の通信（QgsNetworkAccessManager）・QgsTask・設定・画像補正のレンダラー・パンシャープンのファイル処理
    prefs/       機能ごとの個人設定（qsettings.py の pref / set_pref で読み書き）
  processing_provider/  Processing のアルゴリズム（パンシャープン・物体検出・AI 学習・教師データセットほか）
  ui/        ドック・ダイアログ
    ports.py     機能同士の窓口と「変わった」の通知（PluginEvents）
    obs_dock/・plan_dock/・catalog_parts/  観測記録・撮像計画・画像カタログのドックの担当ごとの部分
  tests/     pytest（architecture.py が依存のきまり、snapshots/screen.json が画面の写し、feature_harness.py が機能ごとの小さな組み立て）
tools/
  config_builder.html  設定ファイル作成ツール（ブラウザで開く）
  satellite_editor.html  衛星・センサー・別名のテーブルの編集ツール（ブラウザで開く。設定ファイルを読み込む）
  arcgis/              ArcGIS でテーブルを作り、ポータルに公開するスクリプト（docs/arcgis-setup.md）・ダッシュボード用のビュー（docs/arcgis-dashboards.md）
  audit_check.py       監査ログの送り先のフォルダの欠け・書き換えの点検（python tools/audit_check.py <フォルダ>）
scripts/
  build_zip.py         配布用の zip を作る（導入）
  make_plugin_repo.py  社内のプラグインリポジトリ（plugins.xml）を作る
  mock_esri/           Esri なしで試すモックサーバー
  help_shots/          ヘルプの「画面つきの手順書」の写真を撮り直す・画面の重なりを点検する（QGIS を仮想画面で動かす）
  compare_viewer/      ビューアとの数値比較
  ai_bench/            AI 学習のネットワークの精度比較（合成の場面）
docs/                  ビューアとの違い・撮像依頼の設計書・雲量ファイルの仕様・ArcGIS 側の準備・ArcGIS ダッシュボード・構成と依存のきまり（architecture.md）
```

## テスト

```
python3 -m pip install -r requirements-test.txt       # pytest・pytest-xdist・pytest-timeout・sgp4・onnxruntime（NumPy は 2 未満のまま）

QT_QPA_PLATFORM=offscreen python3 -m pytest -n auto         # 高速版（約 80 秒・4 コア）: 学習が「当たるまで」のテストを除く
QT_QPA_PLATFORM=offscreen python3 -m pytest -n auto --full  # 完全版（約 3 分）: AI 学習が四角を見つけ直すまで学ぶテストも
python3 -m pytest                                           # core だけ（QGIS なしで動く）
MIKAGE_SGP4=bundled python3 -m pytest        # 同梱の純 Python 版 SGP4 で
```

- 遅いテストには `@pytest.mark.slow`。高速版では飛ばし、`--full`（または環境変数 `MIKAGE_FULL_TESTS=1`）で実行。AI 学習のテストは高速版でも 1 エポックで読み込み・学習・書き出し・検出まで通す（精度だけ完全版）
- マージ前は高速版。AI（`core/ai`・`processing_provider` の学習・検出・データセット）を変えたときは完全版も
- `-n auto` は pytest-xdist の並列実行。NumPy の BLAS は 1 プロセス 1 スレッド（`tests/conftest.py`）。並列でなくても動く（高速版で約 1 分 45 秒）
- pytest-timeout があれば、1 件 5 分を超えたテストは全スレッドのスタックを出して止める（固まったまま待たない）
- NumPy 2 では QGIS / GDAL の `gdal_array` が読めず約 200 件が落ちるので、テストは始まる前に止まる。`pip uninstall numpy` でシステムの 1.x に戻す

### 画面の重なりの点検（本物の QGIS で）

```
python3.12 scripts/help_shots/check_layout.py --out /tmp/layout            # 約 10 分
python3.12 scripts/help_shots/check_layout.py --out /tmp/layout --font 12  # 文字を大きくして
```

ヘルプの写真と同じしくみ（Xvfb・モックサーバー）で本物の QGIS を起動し、担当をすべて選んだ状態で、すべてのドック（タブごと・幅 440 / 300 ピクセル）、メニューから開く画面（開いたときと一番小さくしたとき）、撮像依頼・イベントのフォーム、新着画像（大きな画面）、データを入れた状態（カタログの検索結果・観測記録のフォーム・計画の計算結果など）を撮って `--out` に PNG で書き、`mikage/tests/layout_probe.py` で文字や部品の重なり・切れを調べて `problems.json` に画面ごとに書く（問題がなければ `"problems": []`）。`--parts docks,dialogs,forms,feed,filled` で一部だけ、`--only <名前の一部>` で絞る、`--widths 440,300` でドックの幅を変える。画面を足したり並びを変えたりしたら流して、問題の出た写真を目で見る。テスト（`tests/test_qgis_layout_overlap.py`）は、すべてのドックの重なり、`ui/` のすべてのダイアログが `fit_text`（`ui/fit.py`: 折り返す文字のある画面が行の要る高さより小さくならない）を使っていることなどを確かめる

### Esri なしで試す（モックサーバー）

```
python3 scripts/mock_esri/mock_esri.py      # http://127.0.0.1:8765 に ArcGIS フィーチャーサービスのモック（サンプルデータ入り）
```

モックの中身はプラグインの練習モードのサーバー（`mikage/core/training/`）と同じ。プラグインの中で試すだけなら、メニュー「練習モード ▸ 練習モードを始める」でも同じデータで動く（[練習モード](docs/features/screen.md#練習モードtraining)）。書き出される `scripts/mock_esri/data/config.mock.jsonc` を「設定…」で選ぶと、画像カタログ・衛星とセンサーモード・観測記録（分類体系・確認範囲）・外部AI結果・転記先・地名・撮像依頼台帳と出力履歴・通報（条件・ジオフェンス・地名・国・通報記録）・分担と引き継ぎメモ・イベントがすべてモックにつながる。詳しくは [scripts/mock_esri/README.md](scripts/mock_esri/README.md)。

## ビューアとの数値比較

```
cd scripts/compare_viewer && npm install
python3 compare.py /path/to/browser-image-enhancement/usecase/image-viewer/src
```

画像補正は、ビューアのライブラリのコミット済みの `dist/` を Node で動かして突き合わせる（`npm install` は不要）。

```
python3 scripts/compare_viewer/compare_enhance.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist
python3 scripts/compare_viewer/compare_color.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist   # 着色（カラースケール・区分）
python3 scripts/compare_viewer/compare_pansharpen.py /path/to/browser-image-enhancement/packages/browser-image-enhancement/dist   # パンシャープン
```

ビューアの `imaging-plan.ts`（移動目標・衛星パスも）を esbuild で束ねて Node で動かし、同じ衛星・対象・条件でプラグインの計算と突き合わせる（時刻 ±1 秒、角度 ±0.05°、被覆 ±1%、組み合わせは同じパス）。ネットワークは `npm install` のときだけ使う。

座標の表記は `python3 coords.py /path/to/browser-image-enhancement/packages/coordinate-notation/src/index.ts` で、ビューアの `coordinate-notation`（mgrs も）と読み取り・書き出しを突き合わせる。
