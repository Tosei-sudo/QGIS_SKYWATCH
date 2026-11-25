# Esri 側のテーブル定義

ArcGIS（SDE / Oracle）に作る表と属性、推奨のインデックス。（[README](../../README.md) から機能ごとに分けた説明。）

## Esri 側のテーブル定義

プラグインが読み書きする Esri のレイヤー・テーブルの一覧（RDBMS（エンタープライズ ジオデータベース）に作る前提）。属性名と役割の対応は各モジュールの既定（`DEFAULT_*_FIELDS`）と各項目の読み込み処理が元。プラグインは**属性名**（エイリアスではなく）で読み書きするので、エイリアスは表のとおりでなくてもよい。

この表のとおりにテーブルを作り、ポータルに公開して設定ファイルまで作るスクリプトが `tools/arcgis/` にある（ArcGIS Pro の Python で実行。手順は [docs/arcgis-setup.md](../arcgis-setup.md)）。何度実行してもよく、プラグインを新しくして属性が増えたら足りない属性だけ足す。

ArcGIS Enterprise のダッシュボード（撮像計画・観測結果・地名）の構成案と、そのための読み取り専用のビュー（Oracle、`tools/arcgis/dashboard_views_oracle.sql`）は [docs/arcgis-dashboards.md](../arcgis-dashboards.md)。

### 命名の決まり

- テーブル・フィーチャクラスの名前は `TBL_` で始め、すべて大文字、語は `_` で区切る（`TBL_OBSERVATION`、`TBL_EVENT_LINK`）
- 属性名もすべて大文字・`_` 区切り（`IMAGE_ID`・`CREATED_AT`）。30 文字以内
- DBMS の予約語は属性名に使わない。ArcSDE（エンタープライズ ジオデータベース）が形状（ST_Geometry）の中身に使う名前 `FID`・`AREA`・`LEN`・`POINTS`・`NUMOFPTS`・`ENTITY`・`EMINX`・`EMINY`・`EMAXX`・`EMAXY`・`EMINZ`・`EMAXZ`・`MIN_MEASURE`・`MAX_MEASURE` も使わない。プラグインの既定の属性名もこれに従う（2026-11-08 に `MODE`（Oracle の予約語）を `SENSOR_MODE` に、`FILE`（Oracle・SQL Server の予約語）を `OUTPUT_FILE` に、2026-11-10 に通報の条件の `AREA`（ArcSDE の名前）を `GEOFENCE_NAME` に変えた。それより前に作ったレイヤーで新しい名前がなく古い名前だけがあれば、設定を変えなくても古い名前に読み書きする。`tools/arcgis/create_tables.py` は新しい名前の属性を足すとき古い属性の値を写す）
- 既定の属性名がない役割（画像カタログ・衛星カタログなど、設定ファイルの `fields` で必ず指定するもの）は、この決まりに従った推奨の名前を書いた。既にあるテーブルを使うなら、その属性名を `fields` に書けばよい
- テーブルの名前はプラグインからは見えない（設定ファイルにはサービスの URL `…/FeatureServer/n` を書く）。下の名前は推奨

### 型と共通の属性

| 型（表の書き方） | Esri の型 | ArcGIS Pro での名前 | 中身 |
| --- | --- | --- | --- |
| `String(n)` | `esriFieldTypeString`（長さ n） | テキスト | 文字列。長さを書いていないものは 255 |
| `Date` | `esriFieldTypeDate` | 日付 | 日時（UTC で保存。表示のタイムゾーンは[日時のタイムゾーン](operations.md#日時のタイムゾーンtimezonetimezones)） |
| `Double` | `esriFieldTypeDouble` | Double | 実数 |
| `Integer` | `esriFieldTypeInteger` | Long | 整数 |
| `SmallInteger` | `esriFieldTypeSmallInteger` | Short | 0・1 の印など（`ACTIVE`・`PRESENT`・`MANUAL`） |

- `OBJECTID`（`esriFieldTypeOID`）と形状（`SHAPE`）は ArcGIS が作る。座標系は何でもよい（プラグインは WGS 84（4326）で送受信し、サーバーが変換する）
- 4000 を超える長さの文字列（分類体系の `SPECS`、引き継ぎメモの `TEXT` など）は、DBMS によっては CLOB などの大きな文字列の型になる（ArcGIS が作る）。日本語を入れるので、データベースの文字コードは Unicode（Oracle なら AL32UTF8 か各国語文字セットの NVARCHAR2）に
- **作成者・更新者の 4 属性**: プラグイン（と衛星・センサー編集ツール）が書き込むテーブル（下の表で「書き込み」のもの）にはすべて、次の 4 つを足しておくと、追加・更新のたびに入る（[作成者・更新者の記録](operations.md#作成者更新者の記録edittracking)、設定ファイルの `editTracking`。ない属性は書かない）。ArcGIS 自身の編集者トラッキング（`created_user` など）はサインインのアカウントで上書きするので、この 4 属性には使わない

| 属性名 | エイリアス | 型 | 中身 | 役割（`editTracking.fields`） |
| --- | --- | --- | --- | --- |
| `CREATED_DATE` | 作成日時 | `Date` | 追加した日時 | `createdAt` |
| `CREATED_USER` | 作成者 | `String(255)` | 追加した人（環境変数 `USERNAME`） | `createdBy` |
| `UPDATED_DATE` | 更新日時 | `Date` | 最後に更新した日時 | `updatedAt` |
| `UPDATED_USER` | 更新者 | `String(255)` | 最後に更新した人 | `updatedBy` |

### テーブルの一覧

| 推奨の名前 | 形状 | 読み書き | 中身 | 設定ファイルの項目 |
| --- | --- | --- | --- | --- |
| [`TBL_IMAGE_CATALOG`](#tbl_image_catalog画像カタログ) | ポリゴン | 読み | 画像カタログ（フットプリント） | `imageCatalogs[].url` |
| [`TBL_SATELLITE`](#tbl_satellite衛星カタログ) | なし（テーブル） | 書き込み（[衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)。プラグインは読むだけ） | 衛星カタログ（TLE・諸元） | `satelliteCatalogs[].url` |
| [`TBL_SENSOR_MODE`](#tbl_sensor_modeセンサーモード) | なし | 書き込み（[衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)） | センサーの撮像モード | `sensorCatalogs[].url` |
| [`TBL_SATELLITE_ALIAS`](#tbl_satellite_alias衛星名の対応表) | なし | 書き込み（[衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)） | 衛星名の対応表 | `satelliteAliases.url` |
| [`TBL_IMAGING_REQUEST`](#tbl_imaging_request撮像依頼台帳) | ポリゴン | 書き込み | 撮像依頼台帳 | `requestStores[].url` |
| [`TBL_REQUEST_HISTORY`](#tbl_request_history撮像依頼の出力履歴) | ポリゴン（なしでも可） | 書き込み | 撮像依頼の日次出力の履歴・日ごとの撮像予定（採否・要求外） | `requestStores[].history` |
| [`TBL_OBSERVATION`](#tbl_observation観測記録) | ポイント | 書き込み | 観測記録 | `observationStores[].url` |
| [`TBL_OBSERVATION_AREA`](#tbl_observation_area確認範囲) | ポリゴン | 書き込み | 確認範囲 | `observationStores[].areas` |
| [`TBL_TAXONOMY`](#tbl_taxonomy分類体系) | なし | 書き込み | 共有の分類体系 | `observationStores[].taxonomyTable` |
| [`TBL_ALERT_RULE`](#tbl_alert_rule通報の条件) | なし | 読み | 通報の条件 | `alertReports.rules` |
| [`TBL_ALERT_GEOFENCE`](#tbl_alert_geofenceジオフェンス) | ポリゴン | 読み | ジオフェンス | `alertReports.geofences` |
| [`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン) | ポリゴン | 書き込み（「関心地域・地点」の「組織」。2026-11-24 から。それまでは読みだけ） | 地名・施設名の範囲と組織の関心地域 | `namedAreas`・`alertReports.namedAreas` |
| [`TBL_ALERT_LOG`](#tbl_alert_log通報記録) | なし | 書き込み | 通報した記録 | `alertReports.table` |
| [`TBL_ASSIGNMENT`](#tbl_assignment分担) | なし | 書き込み | 画像ごとの分担 | `assignments` |
| [`TBL_HANDOVER_MEMO`](#tbl_handover_memo引き継ぎメモ) | なし | 書き込み | 引き継ぎメモ | `handover.table` |
| [`TBL_EVENT`](#tbl_eventイベント) | ポリゴン（なしも可） | 書き込み | イベント（地震対応など） | `events.layer` |
| [`TBL_EVENT_LINK`](#tbl_event_linkイベントの紐付け) | なし | 書き込み | イベントと画像・記録・依頼の紐付け | `events.links` |
| [`TBL_AI_RESULT`](#tbl_ai_result外部-ai-の結果) | ポリゴンかポイント | 読み | 外部 AI の結果 | `aiResultSources[].url` |
| [`TBL_DETECTION`](#tbl_detection検出結果の転記先) | ポリゴンかポイント | 書き込み | プラグインの物体検出の転記先（「AI結果」でも読む） | `detectionOutputs[].url` |
| [`TBL_PLACE_NAME`](#tbl_place_name地名検索) | ポイント（何でも可） | 読み | 地名検索（閉域） | `geocoders[]`（`type: "features"`） |
| [`TBL_COUNTRY`](#tbl_country国) | ポリゴン | 読み | 国（通報の国コード・国名） | `alertReports.countries` |
| [`TBL_REQUEST_GROUP`](#tbl_request_group依頼グループ) | なし | 書き込み（「依頼グループ…」。2026-11-23 から。それまでは読みだけ） | 撮像依頼の依頼グループとメンバー（2026-11-11 に追加。レイヤー番号を変えないよう一覧の最後） | `requestStores[].groups` |
| [`TBL_REQUEST_SHOT`](#tbl_request_shot撮像の範囲) | ポリゴン | 書き込み | 書き出したファイルの 1 行ごとの撮像の範囲（まとめた依頼は書き出す四角。2026-11-11 に追加） | `requestStores[].shots` |
| [`TBL_IMAGING_QUOTA`](#tbl_imaging_quota年間撮像上限) | なし | 書き込み（[衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)。プラグインは読むだけ） | センサー・モードごとの年間撮像上限（2026-11-11 に追加） | `requestStores[].quotas` |
| [`TBL_INTEREST_POINT`](#tbl_interest_point関心地点) | ポイント | 書き込み | 個人の関心地点（2026-11-14 に追加） | `interests.points` |
| [`TBL_INTEREST_AREA`](#tbl_interest_area関心地域) | ポリゴン | 書き込み | 個人の関心地域（2026-11-14 に追加） | `interests.areas` |
| [`TBL_USER_ROLE`](#tbl_user_role担当) | なし | 書き込み | 一人ひとりの担当（2026-11-15 に追加） | `userRoles` |
| [`TBL_SATELLITE_OUTAGE`](#tbl_satellite_outage衛星の障害情報) | なし | 書き込み | 衛星・システムの障害情報（理由・期間・影響。2026-11-16 に追加、システムの受付停止・臨時の上限は 2026-11-24） | `satelliteOutages` |
| [`TBL_GLOSSARY`](#tbl_glossary備考の単語集) | なし | 読み | 観測記録の備考の単語集（候補と表記ゆれ。2026-11-17 に追加） | `glossary` |
| [`TBL_TRAINING_EXERCISE`](#tbl_training_exercise判読の練習の問題) | ポリゴン | 書き込み（判読の練習の指導者が「判読の練習の問題を作る…」で書く。ほかの人は読むだけ） | 判読の練習の組織の問題（範囲。2026-11-22 に追加） | `training.exercises.exercises` |
| [`TBL_TRAINING_ANSWER`](#tbl_training_answer判読の練習の正解) | ポイント | 書き込み（指導者が「判読の練習の問題を作る…」で書く。ほかの人は読むだけ） | 判読の練習の正解（1 つのモノで 1 点） | `training.exercises.answers` |
| [`TBL_TRAINING_RESULT`](#tbl_training_result判読の練習の結果) | なし | 書き込み | 判読の練習の結果（提出 1 回で 1 行） | `training.exercises.results` |
| [`TBL_IMAGE_CORRECTION`](#tbl_image_correction画像の幾何補正) | なし | 書き込み | 画像ごとのクイック幾何補正（目印の点・地形の補正。2026-11-23 に追加） | `imageCorrections` |
| [`TBL_AREA_CATEGORY`](#tbl_area_category地域のカテゴリー) | なし | 書き込み（「関心地域・地点」の「組織」の「カテゴリー…」） | 組織の関心地域のカテゴリー（木の形。2026-11-24 に追加） | `namedAreas.categories` |
| [`TBL_REQUEST_AREA_CHANGE`](#tbl_request_area_change依頼の範囲の変更記録) | ポリゴン | 書き込み（行を足すだけ） | 地域の形が変わって依頼の範囲を合わせた記録と、合わせる前の範囲（2026-11-24 に追加） | `requestStores[].areaChanges` |
| [`TBL_IMAGE_REVIEW`](#tbl_image_review画像の評価) | ポリゴン | 書き込み | 画像の評価（画像 × 地名ごとに 1 行。判読に使えるか・雲量・理由。2026-11-24 に追加） | `imageReviews` |
| [`TBL_REFERENCE_INFO`](#tbl_reference_info参考情報) | ポイント | 書き込み（削除は作成者か管理者） | 出典つきの参考情報（発表・報道・通報など。場所と日時と出典・確からしさ。2026-11-24 に追加） | `referenceInfo.layer` |
| [`TBL_REFERENCE_LINK`](#tbl_reference_link参考情報の紐付け) | なし | 書き込み | 参考情報とイベント・モノ・観測記録の紐付け（同じ組は 1 行） | `referenceInfo.links` |

使う機能のテーブルだけあればよい（例えば観測記録だけなら `TBL_OBSERVATION`）。「読み」のテーブルはほかのシステムが作るものでもよく、属性名が違えば `fields` で合わせる。表の「必須」は、その属性がないとプラグインがそのテーブルを使わない（理由を出す）もの。ほかの属性は、なければその値を読み書きしない。

### 推奨のインデックス

プラグインが条件を付けて読む属性の属性インデックス。`tools/arcgis/create_tables.py` が下の表のとおりに張る（名前は `IX_<TBL_ を除いたテーブル名>_<属性>`、30 文字で切る）。既にあるテーブルにも、もう一度実行すれば足りないものだけ張る。手で作るなら ArcGIS Pro の「属性インデックスの追加」で。

利用者が多いほど効く。200 人規模では、画像を替えるたびの「ほかの人の変更」（`UPDATED_AT`）と、30 秒ごとの通報の新着（`REPORTED_AT`）がインデックスなしだとテーブル全体を読むことになる。2026-11-18 の点検で日時の 5 つ（下の表で「追加」）を、観測記録を全件読まないようにした作り直しで 4 つ（「作り直し」）を足した。観測記録は画像のまわり・期間・分類・名前で問い合わせるので、これがないと 1 回ごとにテーブル全体を読む。

| テーブル | 属性 | 使う問い合わせ |
| --- | --- | --- |
| `TBL_IMAGE_CATALOG` | `IMAGE_ID` | 画像 ID で画像を引く（記録・依頼から開く） |
| `TBL_IMAGE_CATALOG` | `REGISTERED_AT` | 新着画像、登録日での検索 |
| `TBL_IMAGE_CATALOG` | `ACQUIRED_AT` | 撮像日での検索（追加） |
| `TBL_SATELLITE` | `SAT_NAME` | 衛星名で引く |
| `TBL_SATELLITE_ALIAS` | `SAT_NAME` | 衛星名で引く |
| `TBL_IMAGING_REQUEST` | `REQ_ID` | 依頼 ID で引く・更新する |
| `TBL_IMAGING_REQUEST` | `NAMED_AREA_ID` | 地域から作った依頼（地域の形が直されたときに合わせる依頼） |
| `TBL_REQUEST_HISTORY` | `REQ_ID` | 依頼ごとの出力履歴 |
| `TBL_REQUEST_HISTORY` | `TARGET_DAY` | 対象日の範囲（出力履歴、年間上限の集計） |
| `TBL_REQUEST_HISTORY` | `TARGET_DATE` | 対象日での絞り込み（ダッシュボード） |
| `TBL_REQUEST_HISTORY` | `SHOT_ID` | 撮像の範囲との紐付け |
| `TBL_REQUEST_HISTORY` | `NAMED_AREA_ID` | 地域ごとの出力履歴（ダッシュボード） |
| `TBL_REQUEST_SHOT` | `SHOT_ID` | 撮像の範囲を ID で引く |
| `TBL_REQUEST_SHOT` | `TARGET_DAY` | 対象日の範囲 |
| `TBL_OBSERVATION` | `OBS_ID` | 保存前の確認、通報との照合 |
| `TBL_OBSERVATION` | `OBJECT_ID` | モノごとの記録（継続・候補、ダッシュボード） |
| `TBL_OBSERVATION` | `IMAGE_ID` | 画像ごとの記録 |
| `TBL_OBSERVATION` | `NAMED_AREA_OBJECTID` | 地名ごとの集計（ダッシュボード） |
| `TBL_OBSERVATION` | `UPDATED_AT` | ほかの人の変更（画像を替えるたび。追加） |
| `TBL_OBSERVATION` | `CREATED_AT` | 自分の記録（記録日時の範囲。追加） |
| `TBL_OBSERVATION` | `IMAGE_TIME` | 画像のまわりの記録（撮像日時の前後）、期間での報告書・エクスポート・通報（作り直し） |
| `TBL_OBSERVATION` | `OBJECT_NAME` | 個体（艦名・機番）の名前で引く（作り直し） |
| `TBL_OBSERVATION` | `KIND` | 分類ごとの記録（分類の名前の変更・削除、報告書。作り直し） |
| `TBL_OBSERVATION_AREA` | `AREA_ID` | 保存前の確認 |
| `TBL_OBSERVATION_AREA` | `IMAGE_ID` | 画像ごとの確認範囲 |
| `TBL_OBSERVATION_AREA` | `UPDATED_AT` | ほかの人の変更（追加） |
| `TBL_OBSERVATION_AREA` | `IMAGE_TIME` | 期間での確認範囲（報告書。作り直し） |
| `TBL_NAMED_AREA` | `NAMED_AREA_ID` | 地名 ID で引く |
| `TBL_NAMED_AREA` | `CATEGORY_ID` | カテゴリーごとの地域（カテゴリーの ID を変えたとき） |
| `TBL_ALERT_LOG` | `OBS_ID` | 記録ごとの通報済み |
| `TBL_ALERT_LOG` | `REPORTED_AT` | 通報の新着（30 秒ごと）、期間での通報記録（追加） |
| `TBL_ASSIGNMENT` | `IMAGE_ID` | 画像ごとの分担（画像を開くたび） |
| `TBL_EVENT` | `EVENT_ID` | イベント ID で引く |
| `TBL_EVENT_LINK` | `EVENT_ID` | イベントの紐付け |
| `TBL_EVENT_LINK` | `ITEM_ID` | 記録・画像から見たイベント |
| `TBL_AI_RESULT` | `IMAGE_ID` | 画像ごとの結果 |
| `TBL_DETECTION` | `IMAGE_ID` | 画像ごとの検出結果 |
| `TBL_PLACE_NAME` | `PLACE_NAME` | 地名で引く（部分一致の検索には効かない） |
| `TBL_INTEREST_POINT` | `OWNER_USER` | 持ち主ごとの関心地点 |
| `TBL_INTEREST_AREA` | `OWNER_USER` | 持ち主ごとの関心地域 |
| `TBL_USER_ROLE` | `USER_NAME` | 本人の担当 |
| `TBL_SATELLITE_OUTAGE` | `OUTAGE_ID` | 障害 ID で引く・更新する |
| `TBL_SATELLITE_OUTAGE` | `SAT_NAME` | 衛星ごとの障害 |
| `TBL_SATELLITE_OUTAGE` | `SYSTEM_NAME` | システムごとの障害 |
| `TBL_GLOSSARY` | `TERM` | 語で引く |
| `TBL_TRAINING_EXERCISE` | `EXERCISE_ID` | 問題 ID で引く |
| `TBL_TRAINING_ANSWER` | `EXERCISE_ID` | 問題ごとの正解 |
| `TBL_TRAINING_RESULT` | `USER_NAME` | 本人の結果（判読の練習を開くたび。試験は 1 回だけ） |
| `TBL_TRAINING_RESULT` | `EXERCISE_ID` | 問題ごとの結果（指導者の集計） |
| `TBL_IMAGE_CORRECTION` | `IMAGE_ID` | 画像ごとの幾何補正（画像を開くたび） |
| `TBL_AREA_CATEGORY` | `CATEGORY_ID` | カテゴリーを ID で引く・更新する |
| `TBL_REQUEST_AREA_CHANGE` | `REQ_ID` | 依頼ごとの範囲の変更記録 |
| `TBL_REQUEST_AREA_CHANGE` | `NAMED_AREA_ID` | 地域ごとの範囲の変更記録 |
| `TBL_IMAGE_REVIEW` | `REVIEW_ID` | 評価を ID で引く・書き直す |
| `TBL_IMAGE_REVIEW` | `IMAGE_ID` | 画像の評価（画像を開くたび、カタログの一覧、依頼の照合） |
| `TBL_IMAGE_REVIEW` | `NAMED_AREA_ID` | 地名ごとの評価（ダッシュボード） |
| `TBL_REFERENCE_INFO` | `INFO_ID` | 参考情報を ID で引く・更新する |
| `TBL_REFERENCE_INFO` | `INFO_TIME` | 期間の参考情報（ドックの一覧、画像の前後の日数） |
| `TBL_REFERENCE_INFO` | `NAMED_AREA_ID` | 地名ごとの参考情報（画像に写る地名の参考情報） |
| `TBL_REFERENCE_LINK` | `INFO_ID` | 参考情報の紐付け |
| `TBL_REFERENCE_LINK` | `ITEM_ID` | イベント・モノ・記録の参考情報 |

ほかに:

- **空間インデックス**: 形状のあるテーブル（画像カタログ、観測記録、確認範囲、地名のポリゴンなど）には ArcGIS がフィーチャクラスを作るときに張る。範囲での問い合わせ（カタログの範囲検索など）はこれを使う
- **記録者の大文字・小文字を区別しない検索**（任意）: 「自分の記録」は `UPPER(OBSERVER)` で比べる。記録日時を付けずに全期間を見ることが多いなら、Oracle で関数インデックスを作る（ArcGIS の「属性インデックスの追加」では作れない）

  ```sql
  CREATE INDEX IX_OBSERVATION_UPPER_OBSERVER ON TBL_OBSERVATION (UPPER(OBSERVER));
  ```

- **統計情報**: まとめて取り込んだ後や、件数が大きく増えた後は統計情報を更新する（ArcGIS Pro の「データセットの分析」、Oracle なら `DBMS_STATS.GATHER_TABLE_STATS`）。古いままだと、インデックスがあっても使われないことがある
- **ダッシュボードのビュー**は問い合わせのたびに全件を集計する。閲覧する人が多いならマテリアライズド ビューに（[docs/arcgis-dashboards.md](../arcgis-dashboards.md)）

### `TBL_IMAGE_CATALOG`（画像カタログ）

フットプリントのポリゴン（1 画像 = 1 フィーチャ）。`imageCatalogs[].fields` で役割ごとに属性名を指定する（既定なし）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID。観測記録・撮像依頼の撮像済みの確認・AI結果が使う | `id` |
| `ACQUIRED_AT` | 撮像日時 | `Date` | 撮像日時 | `acquired` |
| `REGISTERED_AT` | 登録日時 | `Date` | カタログに登録した日時（新着の通知） | `registered` |
| `SYSTEM_REGISTERED_AT` | システム登録日時 | `Date` | 元のシステムに登録した日時 | `systemRegistered` |
| `SENSOR` | センサー | `String(255)` | 衛星・センサー名（衛星カタログの名前か[対応表](../features/plan.md#衛星名の対応表satellitealiases)の別名と照合） | `sensor` |
| `OFF_NADIR` | オフナディア角 | `Double` | 撮像の角度（度）。種類は `angleKind`（`offNadir`・`incidence`・`elevation`） | `angle` |
| `SAT_AZIMUTH` | 衛星方位角 | `Double` | 地上から見た衛星の方位角（度） | `azimuth` |
| `FILE_PATH` | ファイルパス | `String(1000)` | 画像（かフォルダ）のパス。**必須**（`source` のどれか 1 つ以上） | `source` |
| `COG_URL` | 画像URL | `String(1000)` | 画像の URL（COG など）。`source` は配列で複数書け、先に書いたものから使う | `source` |

### `TBL_SATELLITE`（衛星カタログ）

1 衛星 1 行（モードごとに行を分けてもよい）。`satelliteCatalogs[].fields` で指定（既定なし）。数の属性が空なら `defaults`、それもなければプラグインの既定。センサーモード・別名といっしょに [衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)で直せる。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `SAT_NAME` | 衛星名 | `String(255)` | 衛星名（なければ TLE の名前） | `name` |
| `NORAD_ID` | NORAD番号 | `String(64)` | 衛星の ID（なければ衛星名） | `id` |
| `TLE_LINE1` | TLE1行目 | `String(255)` | TLE の 1 行目。**必須**（`TLE_LINE1`＋`TLE_LINE2` か `tle` のどちらか） | `tle1` |
| `TLE_LINE2` | TLE2行目 | `String(255)` | TLE の 2 行目 | `tle2` |
| （`TLE`） | TLE | `String(1000)` | 2 行（か名前つき 3 行）をまとめて 1 属性に持つとき | `tle` |
| `MAX_OFF_NADIR` | 最大オフナディア角 | `Double` | 向けられる角度の上限（度、既定 30） | `maxOffNadir` |
| `MIN_OFF_NADIR` | 最小オフナディア角 | `Double` | 角度の下限（度、既定 0。SAR の近距離側など） | `minOffNadir` |
| `SWATH` | 観測幅 | `Double` | 観測幅（`swathUnit` の単位、既定 km。既定 10） | `swath` |
| `SCENE_LENGTH` | シーン長 | `Double` | 1 シーンの長さ（`swath` と同じ単位。空なら観測幅と同じ） | `length` |
| `LOOK_SIDE` | 観測方向 | `String(16)` | `右`・`左`・`両方`（`right`・`R` なども可） | `lookSide` |
| `SENSOR_TYPE` | センサー種別 | `String(64)` | `光学`・`SAR` など（`sarPattern` に合えば SAR） | `kind` |
| `SCENES_PER_PASS` | 1パスのシーン数 | `Integer` | 1 回のパスで撮れるシーン数（既定 1） | `scenesPerPass` |
| `MIN_INTERVAL_MIN` | 最小撮像間隔 | `Double` | 撮像と撮像の間隔の下限（分、既定 0） | `minInterval` |
| `MAX_PER_DAY` | 1日の上限 | `Integer` | 1 日の撮像数の上限（0 で制限なし） | `maxPerDay` |
| `SHOTS_PER_PASS` | 1パスの撮像回数 | `Integer` | 1 回のパス（同じ軌道方向の半周）で撮れる回数の上限（空・0 で制限なし。撮像間隔だけで離す） | `shotsPerPass` |
| `SERIES` | シリーズ | `String(255)` | 衛星シリーズ（空なら名前の末尾の番号を除いたもの） | `series` |
| `LEAD_TIME_H` | 発注リードタイム | `Double` | 撮像の何時間前までに発注するか（時間、既定 0） | `leadTime` |
| `SENSOR` | センサー | `String(255)` | 使うセンサー（`TBL_SENSOR_MODE` の `SENSOR`） | `sensor` |
| `SENSOR_MODE` | センサーモード | `String(255)` | この行のモード（モードごとに行を分けるとき） | `mode` |
| `RESOLUTION_M` | 分解能 | `Double` | 分解能（m） | `resolution` |
| `SAT_SYSTEM` | 衛星システム | `String(255)` | 発注するシステム（`requestExports` の `system`） | `system` |

### `TBL_SENSOR_MODE`（センサーモード）

1 センサー・モード 1 行。空の値は衛星の値を使う。`sensorCatalogs[].fields` で指定（既定なし）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `SENSOR` | センサー | `String(255)` | センサー名。**必須** | `sensor` |
| `SENSOR_MODE` | センサーモード | `String(255)` | モード名（ストリップマップ・スポットライトなど） | `mode` |
| `SENSOR_TYPE` | センサー種別 | `String(64)` | `光学`・`SAR` など | `kind` |
| `MAX_OFF_NADIR` | 最大オフナディア角 | `Double` | 角度の上限（度） | `maxOffNadir` |
| `MIN_OFF_NADIR` | 最小オフナディア角 | `Double` | 角度の下限（度） | `minOffNadir` |
| `SWATH` | 観測幅 | `Double` | 観測幅（`swathUnit`） | `swath` |
| `SCENE_LENGTH` | シーン長 | `Double` | 1 シーンの長さ | `length` |
| `LOOK_SIDE` | 観測方向 | `String(16)` | `右`・`左`・`両方` | `lookSide` |
| `SCENES_PER_PASS` | 1パスのシーン数 | `Integer` | 1 パスのシーン数 | `scenesPerPass` |
| `MIN_INTERVAL_MIN` | 最小撮像間隔 | `Double` | 撮像間隔の下限（分） | `minInterval` |
| `MAX_PER_DAY` | 1日の上限 | `Integer` | 1 日の上限 | `maxPerDay` |
| `SHOTS_PER_PASS` | 1パスの撮像回数 | `Integer` | 1 パスの撮像回数の上限 | `shotsPerPass` |
| `LEAD_TIME_H` | 発注リードタイム | `Double` | 発注の期限（時間前） | `leadTime` |
| `RESOLUTION_M` | 分解能 | `Double` | 分解能（m） | `resolution` |
| `IS_DEFAULT` | 既定モード | `SmallInteger` | そのセンサーのふだんのモードなら 1（`はい`・`true` などの文字も可） | `primary` |

### `TBL_SATELLITE_ALIAS`（衛星名の対応表）

1 行に 1 つ（かカンマ区切りで複数）の別名。`satelliteAliases.fields` で指定（既定なし。2 つとも必須）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `SAT_NAME` | 衛星名 | `String(255)` | 衛星カタログの名前か ID。**必須** | `satellite` |
| `ALIAS` | 別名 | `String(1000)` | 画像カタログでの名前（`WV03, WV3`）。**必須** | `alias` |

### `TBL_IMAGING_REQUEST`（撮像依頼台帳）

依頼の範囲のポリゴン（1 依頼 = 1 フィーチャ。点の依頼も正方形にして `SHAPE_KIND` に `点`）。`requestStores[].fields`（既定は下の名前）。値の詳しい意味は [docs/imaging-requests.md](../imaging-requests.md) の「4. 依頼台帳」。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `REQ_ID` | 依頼ID | `String(64)` | 依頼の ID（UUID）。**必須** | `id` |
| `REQ_NAME` | 名称 | `String(255)` | 依頼の名前 | `name` |
| `MANAGEMENT_NO` | 管理番号 | `String(255)` | 依頼の管理番号（文字列。システム向けファイルの項目 `managementNo`） | `managementNo` |
| `IDENTIFICATION_NO` | 識別番号 | `String(255)` | 依頼の識別番号（文字列。項目 `identificationNo`） | `identificationNo` |
| `REQUEST_GROUP` | 依頼グループ | `String(255)` | 依頼グループの名前（[`TBL_REQUEST_GROUP`](#tbl_request_group依頼グループ) の `GROUP_NAME`。空はグループなし。項目 `group`） | `group` |
| `CATEGORY` | 区分 | `String(16)` | `定点`・`臨時` | `category` |
| `ACHIEVE` | 達成条件 | `String(16)` | `毎回`・`期間内1回` | `achieve` |
| `START_AT` | 開始 | `Date` | 期間の始まり | `start` |
| `END_AT` | 終了 | `Date` | 期間の終わり | `end` |
| `REPEAT` | 繰り返し | `String(64)` | `毎日`・`曜日:月水金`・`間隔:3` | `repeat` |
| `STATUS` | 状態 | `String(16)` | `有効`・`停止`・`完了` | `status` |
| `PRIORITY` | 優先度 | `Integer` | 1〜1000（小さいほど高い） | `priority` |
| `BOOST` | 繰り上げ量 | `Integer` | 未撮像のときの繰り上げ（100 単位） | `boost` |
| `KIND` | 種別 | `String(16)` | `任意`・`光学`・`SAR` | `kind` |
| `SERIES` | 衛星シリーズ | `String(1000)` | シリーズ（カンマ区切り。空なら問わない） | `series` |
| `SATELLITES` | 衛星 | `String(1000)` | 個別に指定する衛星（カンマ区切り） | `satellites` |
| `SENSOR_MODES` | センサーモード | `String(1000)` | センサー・モード（カンマ区切り） | `modes` |
| `OFF_NADIR_MIN` | オフナディア角下限 | `Double` | 度 | `offNadirMin` |
| `OFF_NADIR_MAX` | オフナディア角上限 | `Double` | 度 | `offNadirMax` |
| `INCIDENCE_MIN` | 入射角下限 | `Double` | 度 | `incidenceMin` |
| `INCIDENCE_MAX` | 入射角上限 | `Double` | 度 | `incidenceMax` |
| `LOOK_SIDE` | 観測方向 | `String(16)` | `任意`・`右`・`左` | `lookSide` |
| `ORBIT_DIR` | 軌道方向 | `String(16)` | `任意`・`昇交`・`降交` | `orbitDir` |
| `RESOLUTION_MAX` | 分解能 | `Double` | これより細かい分解能（m） | `resolution` |
| `TIME_OF_DAY` | 時間帯 | `String(16)` | `任意`・`昼`・`夜` | `timeOfDay` |
| `CLOUD_NO_GO` | 実施しない雲量 | `Integer` | この雲量（%）以上なら実施しない（光学） | `cloudNoGo` |
| `TIMES_MIN` | 1日の回数下限 | `Integer` | 1 日にこの回数撮れたらその日は達成（別々のパスで。空なら 1）。無くても動く | `timesMin` |
| `TIMES_MAX` | 1日の回数上限 | `Integer` | 空いた枠があればこの回数まで出す（空なら下限と同じ）。無くても動く | `timesMax` |
| `TIMES_GAP` | 回の間隔 | `Double` | 同じ日の回と回の最短の間隔（時間。空なら 1）。無くても動く | `timesGap` |
| `SYSTEMS` | 優先システム | `String(1000)` | 優先するシステムの順（カンマ区切り） | `systems` |
| `SHAPE_KIND` | 範囲の種類 | `String(16)` | `範囲`・`点` | `shapeKind` |
| `NAMED_AREA_ID` | 地域の管理番号 | `String(255)` | 依頼を作った組織の地域（[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン) の `NAMED_AREA_ID`）。地域から作らなかった依頼は空。無くても動く（地域とのつながりが残らない） | `namedAreaId` |
| `AREA_FOLLOW` | 地域の形に合わせる | `SmallInteger` | 1 なら地域の形が直されたとき依頼の範囲も合わせる（0・空は合わせない。範囲を手で変えると 0 になる）。無くても動く | `areaFollow` |
| `REQUESTER` | 依頼者 | `String(255)` | 依頼者 | `requester` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |
| `CREATED_AT` | 登録日時 | `Date` | 登録した日時 | `created` |
| `UPDATED_AT` | 更新日時 | `Date` | 更新した日時 | `updated` |

### `TBL_REQUEST_HISTORY`（撮像依頼の出力履歴）

日次出力の 1 依頼・1 日 1 行（1 日に何回も撮る依頼は 1 回 1 行、広い範囲を衛星で分担した依頼はショットごとに 1 行で範囲はそのショットの部分）と、システムが依頼なしで計画したもの（要求外）の 1 計画 1 行。その日の撮像予定（「撮像予定」ドック・ダッシュボード）はこの表の対象日の行。ポリゴン（依頼の範囲、要求外は結果ファイルか手で描いた範囲）。形状なしのテーブルでもよい（範囲は残らず、ドックは台帳の範囲で描く。`create_tables.py` は既にある表の形状を変えないので、ポリゴンにするなら作り直す）。`requestStores[].historyFields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `RUN_ID` | 出力ID | `String(64)` | 出力 1 回の ID | `runId` |
| `RUN_DATE` | 出力日時 | `Date` | 出力した日時 | `runDate` |
| `TARGET_DAY` | 対象日 | `String(16)` | 対象の日（`2026-11-08`）。**必須** | `day` |
| `TARGET_DATE` | 対象日（日付） | `Date` | 対象の日の日本時間 0 時（ダッシュボードの日付の絞り込み・グラフ用） | `targetDate` |
| `REQ_ID` | 依頼ID | `String(64)` | 台帳の `REQ_ID`（要求外は空のことがある）。**必須** | `requestId` |
| `REQ_NAME` | 名称 | `String(255)` | 出力したときの依頼の名称（要求外は計画の名称） | `name` |
| `MANAGEMENT_NO` | 管理番号 | `String(255)` | 出力したときの管理番号 | `managementNo` |
| `REQUESTER` | 依頼者 | `String(255)` | 出力したときの依頼者 | `requester` |
| `REQUEST_GROUP` | 依頼グループ | `String(255)` | 出力したときの依頼グループ（ダッシュボード・撮像予定ドックの絞り込み用。要求外は名前の依頼のグループ） | `group` |
| `SYSTEM` | システム | `String(255)` | 出力した衛星システム（見送りは空。システムごとに書き出したときは、そのシステム） | `system` |
| `SATELLITE` | 衛星 | `String(255)` | 割り当てた衛星 | `satellite` |
| `PRIORITY_OUT` | 出力優先度 | `String(64)` | システムに渡した優先度（`High` など） | `priority` |
| `CLOUD` | 雲量予報 | `Double` | 出力時の雲量予報（%） | `cloud` |
| `SCENES` | シーン数 | `Integer` | 見積もったシーン数（まとめた撮像は、その行になった依頼に全部。同乗した依頼は 0） | `scenes` |
| `RESULT` | 結果 | `String(64)` | `採用`（出力した）・`見送り`・`要求外`（システムが依頼なしで計画したもの） | `result` |
| `REASON` | 理由 | `String(1000)` | 見送り・警告の理由 | `reason` |
| `OUTPUT_FILE` | 出力ファイル | `String(1000)` | 書き出したファイル（古い既定の `FILE` だけがあるテーブルはそれを使う） | `file` |
| `CAPTURED` | 撮像結果 | `String(16)` | `未判定`・`撮像済み`・`未撮像` | `captured` |
| `IMAGE_ID` | 画像ID | `String(255)` | 撮像済みにした画像の ID | `imageId` |
| `BOOST_DONE` | 繰り上げ済み | `Integer` | 繰り上げを済ませた量 | `boostDone` |
| `ADOPTION` | 採否 | `String(16)` | システムの採否: `採用`・`不採用`・`不明`（出力した依頼。分からないまま、画像が入れば `採用`）。要求外は `採用`、見送りは空 | `adoption` |
| `ADOPTION_REASON` | 採否の理由 | `String(1000)` | 不採用の理由・要求外のメモ（結果ファイルか手入力） | `adoptionReason` |
| `PLANNED_AT` | 撮像予定日時 | `Date` | システムの撮像予定の日時（結果ファイルか手入力） | `plannedAt` |
| `EXTERNAL_ID` | システム側の番号 | `String(255)` | システムが付けた番号 | `externalId` |
| `ADOPTION_SOURCE` | 採否の入力元 | `String(1000)` | 結果ファイルの名前・`手入力`・`画像の入着` | `adoptionSource` |
| `MERGED_INTO` | まとめ先の依頼ID | `String(255)` | ほかの依頼と 1 回の撮像にまとめて出力したとき、ファイルでその行になった依頼の `REQ_ID`（自分の行で出したものは空）。システムの採否はまとめた依頼すべてに付く。無くても動く | `mergedInto` |
| `SHOT_ID` | 撮像ID | `String(64)` | その依頼を撮る撮像（[`TBL_REQUEST_SHOT`](#tbl_request_shot撮像の範囲) の `SHOT_ID`。まとめた依頼は同じ値）。軌道を使わずに詰めた依頼・見送りは空。無くても動く | `shotId` |
| `OCCURRENCE` | その日の回 | `Integer` | 1 日に何回も撮る依頼（`TIMES_MIN`・`TIMES_MAX`）の何回目か（1, 2 …。1 回の依頼は 1）。無くても動く（何回も撮る依頼は回の区別が残らない） | `occurrence` |
| `LINK_ID` | 外部連携ID | `String(32)` | 出力した行の短い ID（既定 12 文字の英数字。システムごとに `linkIdLength`・`linkIdChars`）。ファイルの項目 `linkId` で渡し、結果ファイルの `linkId` の列でこの行と照合する。同じ依頼・同じ日・同じシステム・同じ回なら書き出し直しても同じ値。見送りは空。無くても動く（ID では照合できない） | `linkId` |
| `PART_NO` | 分担の番号 | `Integer` | 1 ショットに収まらない広い範囲をその日の衛星で分けて撮るとき（分担）の何番目のショットか（1, 2 …）。行の範囲はそのショットが撮る部分。範囲をまるごと出した行は空。無くても動く（分担の行の区別が残らず、照合は依頼の範囲で見る） | `part` |
| `NAMED_AREA_ID` | 地域の管理番号 | `String(255)` | 出力したときの依頼の地域（台帳の `NAMED_AREA_ID`。ダッシュボードで地域ごとに見る。要求外は空）。無くても動く | `namedAreaId` |

### `TBL_OBSERVATION`（観測記録）

記録の位置のポイント（1 記録 = 1 フィーチャ）。`observationStores[].fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `OBS_ID` | 記録ID | `String(64)` | 記録の ID（UUID）。**必須** | `id` |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID（カタログの画像 ID か、ファイル名） | `imageId` |
| `IMAGE_NAME` | 画像名 | `String(255)` | 画像の名前（カタログの画像は画像 ID、ほかはファイル名。ファイルなしで手入力した画像は入れた名前） | `imageName` |
| `IMAGE_TIME` | 撮像日時 | `Date` | 画像の撮像日時 | `imageTime` |
| `OBJECT_ID` | モノID | `String(64)` | モノ（同じ船・機体など）の ID（UUID）。**必須** | `objectId` |
| `OBJECT_NAME` | モノの名前 | `String(255)` | 船名・機番など | `objectName` |
| `KIND` | 種別 | `String(1000)` | 分類のパス（`船舶 > 貨物船 > コンテナ船`） | `kind` |
| `ACTIVITY` | 活動 | `String(255)` | 停泊・航行など | `activity` |
| `PRESENT` | 在不在 | `SmallInteger` | 在 1・消失 0（不可の記録は空） | `present` |
| `NOTE` | 備考 | `String(4000)` | 備考（AI の結果から取り込んだ印 `[AI:…]` も） | `note` |
| `LENGTH_M` | 長さ | `Double` | 長さ（m、2 点以上で測ったとき） | `length` |
| `HEADING` | 向き | `Double` | 向き（度、北から時計回り） | `heading` |
| `WIDTH_M` | 幅 | `Double` | 幅（m、3・4 点で測ったとき） | `width` |
| `PIXEL_X` | 画素X | `Double` | 記録の点の、画像ファイルの左上からの画素位置（右へ、全解像度、小数あり）。記録したとき・位置を直したときに画像が開いていれば入る。位置ずらしした画像でも元のファイルの画素。画像の外ならマイナスや幅より大きい値。AI 学習で枠をこの画素に置く。無くても動く | `pixelX` |
| `PIXEL_Y` | 画素Y | `Double` | 同じく下へ数えた画素位置 | `pixelY` |
| `IMAGE_FILE` | 画像ファイル | `String(255)` | その画素位置のファイル（フォルダのレコードの画像はフォルダ内のパス、ほかはファイル名） | `imageFile` |
| `SATELLITE` | 衛星名 | `String(255)` | 画像を撮った衛星（センサー）。カタログの画像はカタログの「センサー」、カタログ外の画像はメタデータ・ファイル名から、分からなければ手入力（[衛星名](../features/observations.md#観測記録)）。書き方は画像カタログにそろえる。以前の記録は空。無くても動く（2026-11-20 に追加） | `satellite` |
| `QUANTITY` | 数量 | `Integer` | その記録 1 件が表すモノの数（駐車場の車両 30 台を 1 点で記録したときの 30。[数量](../features/observations.md#数量まとめて-1-件で記録)）。2 以上のときだけ入り、空は 1（1 つずつの記録と以前の記録）。消失・不可の記録は空。台数などの集計は件数でなく数量の合計（空は 1）で。無くても動く（入力欄の「数量」が出ない。2026-11-24 に追加） | `quantity` |
| `CONFIDENCE` | 確度 | `String(64)` | 確定・推定・不明など | `confidence` |
| `OBSERVER` | 記録者 | `String(255)` | 記録した人 | `recordedBy` |
| `UPDATED_BY` | 更新者名 | `String(255)` | 最後に直した人（記録者と同じ名前の欄） | `updatedBy` |
| `CREATED_AT` | 記録日時 | `Date` | 記録した日時 | `created` |
| `UPDATED_AT` | 修正日時 | `Date` | 最後に直した日時（ほかの人の変更との食い違いの確認にも使う） | `updated` |
| `DELETED_AT` | 削除日時 | `Date` | 削除した日時（`softDelete`。空なら有効。この属性がなければ完全に消す） | `deleted` |
| `UNSEEN_REASON` | 不可の理由 | `String(40)` | 雲・霞などで見えなかったときの理由（「不可」の記録。このとき `PRESENT` は空）。見えた記録は空。無ければ「不可」は使えない（2026-11-19 に追加） | `unseen` |
| `NAMED_AREA_OBJECTID` | 地名のOBJECTID | `Integer` | 記録の点を含む地名（[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン)）の `OBJECTID`。エンタープライズ ジオデータベースの**属性ルール**で入れる（例は [docs/arcgis-setup.md](../arcgis-setup.md#属性ルール記録の地名の-objectid)）。プラグインは書かない（記録を直して保存しても、送るのはほかの属性だけなので値はそのまま）。データ分析で観測記録と地名を結ぶキー。無くても動く | `namedAreaObjectId` |

### `TBL_OBSERVATION_AREA`（確認範囲）

確認した範囲のポリゴン。雲などで見えなかった範囲（不可範囲、`UNSEEN_REASON` に理由）も同じレイヤーに置く。`observationStores[].areas.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `AREA_ID` | 範囲ID | `String(64)` | 範囲の ID（UUID）。**必須** | `id` |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID | `imageId` |
| `IMAGE_NAME` | 画像名 | `String(255)` | 画像の名前 | `imageName` |
| `IMAGE_TIME` | 撮像日時 | `Date` | 画像の撮像日時 | `imageTime` |
| `KIND` | 対象の種別 | `String(1000)` | 確かめた種別（分類のパス。空ならすべて） | `kind` |
| `NOTE` | 備考 | `String(4000)` | 備考 | `note` |
| `OBSERVER` | 記録者 | `String(255)` | 記録した人 | `recordedBy` |
| `CREATED_AT` | 記録日時 | `Date` | 記録した日時 | `created` |
| `UPDATED_AT` | 修正日時 | `Date` | 直した日時 | `updated` |
| `DELETED_AT` | 削除日時 | `Date` | 削除した日時 | `deleted` |
| `UNSEEN_REASON` | 不可の理由 | `String(40)` | 不可範囲の理由（雲・霞など）。空なら確認範囲。無ければ不可範囲は使えない（2026-11-19 に追加） | `unseen` |
| `SATELLITE` | 衛星名 | `String(255)` | その画像の衛星名（観測記録と同じ）。以前の範囲は空。無くても動く（2026-11-20 に追加） | `satellite` |

### `TBL_TAXONOMY`（分類体系）

1 分類 1 行（形状なし）。`PATH` が `#諸元項目` の行は分類ではなく諸元の項目の定義。`observationStores[].taxonomyTable.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `PATH` | 分類 | `String(1000)` | 分類のパス（`船舶 > 貨物船`）。**必須** | `path` |
| `LEVELS` | 階層名 | `String(1000)` | 階層の名前（`大分類,種別,級`。大分類の行） | `levels` |
| `NAME_LABEL` | 名前の見出し | `String(255)` | モノの名前の見出し（船名・機番） | `nameLabel` |
| `MEMBERS` | 個体 | `String(8000)` | その分類のモノの名前（カンマ区切り） | `members` |
| `SPECS` | 諸元 | `String(16000)` | 諸元（JSON） | `specs` |
| `PHOTOS` | 参考写真 | `String(8000)` | 参考写真（JSON） | `photos` |
| `ACTIVITIES` | 活動 | `String(2000)` | その大分類の活動（カンマ区切り） | `activities` |
| `SORT_ORDER` | 並び順 | `Integer` | 並び順 | `order` |

### `TBL_ALERT_RULE`（通報の条件）

1 行 1 条件（形状なし）。`alertReports.rules.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `KIND` | 分類 | `String(1000)` | 分類（その分類以下。空欄ですべて） | `kind` |
| `ACTIVITIES` | 活動 | `String(2000)` | 活動（カンマ区切り。空欄か `*` ですべて） | `activities` |
| `MIN_CONFIDENCE` | 確度の下限 | `String(64)` | 確度の下限 | `minConfidence` |
| `GEOFENCE_NAME` | ジオフェンス | `String(255)` | ジオフェンスの `NAME`（`*` でどれかの中、空欄で問わない） | `area` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 で停止 | `active` |
| `NOTE` | 備考 | `String(1000)` | 条件の説明 | `note` |
| `WEIGHT` | 重み | `Double` | 重要度スコアの条件の重み（0〜1 か 0〜100、空欄は 1） | `weight` |

### `TBL_ALERT_GEOFENCE`（ジオフェンス）

ポリゴン。`alertReports.geofences.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `NAME` | 名前 | `String(255)` | ジオフェンスの名前（条件の `GEOFENCE_NAME`） | `name` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 で使わない | `active` |
| `WEIGHT` | 重み | `Double` | 重要度スコアの場所の重み（空欄は 1） | `weight` |
| `NAME_EN` | 英語名 | `String(255)` | 英語の名前（通報の `area.en`。なくてもよい） | `nameEn` |

### `TBL_NAMED_AREA`（地名のポリゴン）

地名・施設名の範囲のポリゴン（記録を含むいちばん小さいものが通報の `place`）。2026-11-24 から**組織の関心地域**も同じ表に置く（[組織の関心地域](../features/images.md#組織の関心地域namedareas)。新しい表は作らず、属性を横に足した）。どの行を何に使うかは `AREA_USE`（用途）で決める: 空・`地名` は今までどおりの地名だけ、`関心地域` は組織の関心地域だけ（新着画像のカード・「関心地域・地点」の「組織」に出て、通報・観測記録の場所の名前には**使わない**）、`両方` はどちらにも。行の追加・変更は「関心地域・地点」の「組織」で全員ができる（ArcGIS Pro で直してもよい。プラグインが書くので作成者・更新者の 4 属性も置き、サービスで編集を許可する: `tools/arcgis/create_tables.py --grant-edit` をもう一度）。設定ファイルは `namedAreas`（なければ `alertReports.namedAreas`）、属性名が違えば `namedAreas.fields`（既定は下の名前）。

`NAMED_AREA_ID`（管理番号）はすべての行に付け、重ならないようにする（依頼・記録との紐付けに使う。`OBJECTID` は地名のデータを入れ直すと変わる）。空の行は保存したときに `NA-000123` の形で付く。付けるのはジオデータベースの属性ルール（Oracle のシーケンス `SEQ_NAMED_AREA_ID`。作り方は [docs/arcgis-setup.md](../arcgis-setup.md#属性ルール地名の管理番号の自動付番)）で、ルールがなければプラグインが「いちばん大きい番号の次」を付け、保存の後にもう一度重なりを確かめる（2 台が同時に付けたら、`OBJECTID` の大きい行が次の番号に）。前からある空の行は `tools/arcgis/named_area_ids.py` か、「組織」の「管理番号のない n 件に付番…」でまとめて付ける。番号を付けると、通報フォーマットの `place.id` に今まで空だった地名の番号も出るようになる。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `NAME` | 地名 | `String(255)` | 地名・施設名・関心地域の名前 | `name` |
| `KIND` | 種類 | `String(64)` | 階層の管理（都道府県・市町村・港・施設など。通報の `placeKinds` にも使う） | `kind` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 で使わない（地名にも関心地域にも） | `active` |
| `NAME_EN` | 英語名 | `String(255)` | 英語の地名（通報の `place.en`・`places.en`。なくてもよい） | `nameEn` |
| `KIND_EN` | 種類（英語） | `String(64)` | 英語の種類（通報の `placeKind.en`。なくてもよい） | `kindEn` |
| `NAMED_AREA_ID` | 管理番号 | `String(255)` | 組織内の管理番号（全体で重ならない。空なら保存時に付番。通報の項目 `place.id`・`places.id`。観測記録の `NAMED_AREA_OBJECTID` はこれではなく `OBJECTID`） | `id` |
| `AREA_USE` | 用途 | `String(16)` | `地名`・`関心地域`・`両方`（空は `地名`）。`関心地域` だけの行は通報・観測記録の場所の名前に使わない | `use` |
| `NAME_KANA` | よみがな | `String(255)` | ひらがなの読み（一覧の並べ替えと検索） | `kana` |
| `NAME_ALIAS` | 別名 | `String(1000)` | 略称・旧名・通称（カンマか読点で区切っていくつでも。検索に使う） | `alias` |
| `COUNTRY_CODE` | 国 | `String(8)` | 国コード（[`TBL_COUNTRY`](#tbl_country国) の `COUNTRY_CODE`。1 地域に 1 つ。「形から」で国のレイヤーから入る） | `country` |
| `CATEGORY_ID` | カテゴリー | `String(64)` | カテゴリーの ID（[`TBL_AREA_CATEGORY`](#tbl_area_category地域のカテゴリー) の `CATEGORY_ID`。名前ではなく ID でつなぐ） | `category` |
| `PARENT_ID` | 上位の管理番号 | `String(255)` | この地域を含む地域の `NAMED_AREA_ID`（「組織」の一覧の「上位の地域別」の木） | `parent` |
| `OWNER_GROUP` | 担当グループ | `String(255)` | 受け持つ依頼グループ（[`TBL_REQUEST_GROUP`](#tbl_request_group依頼グループ) の `GROUP_NAME`） | `ownerGroup` |
| `PRIORITY` | 重要度 | `Integer` | 1（最も高い）〜1000。空は依頼の既定と同じ 500 | `priority` |
| `COLOR` | 色 | `String(32)` | 地図とカードの色（`#rrggbb`）。空はカテゴリーの色 | `color` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_ALERT_LOG`（通報記録）

通報した記録の 1 記録 1 行（形状なし）。`alertReports.table.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `OBS_ID` | 記録ID | `String(64)` | 観測記録の `OBS_ID`。**必須** | `obsId` |
| `REPORT_NAME` | フォーマット名 | `String(255)` | 使ったフォーマットの名前 | `report` |
| `TEXT` | 通報文 | `String(4000)` | 通報した行 | `text` |
| `REPORTED_AT` | 通報日時 | `Date` | 通報した日時 | `reportedAt` |
| `REPORTED_BY` | 通報者 | `String(255)` | 通報した人 | `reportedBy` |
| `BATCH_ID` | 通報ID | `String(64)` | 1 回の通報の ID | `batch` |
| `MANUAL` | 手動 | `SmallInteger` | 手動で追加した記録は 1 | `manual` |

### `TBL_ASSIGNMENT`（分担）

画像ごとの担当（形状なし）。`assignments.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID。**必須** | `imageId` |
| `IMAGE_NAME` | 画像名 | `String(255)` | 画像の名前 | `imageName` |
| `IMAGE_TIME` | 撮像日時 | `Date` | 画像の撮像日時 | `imageTime` |
| `ASSIGNEE` | 担当者 | `String(255)` | 担当者（`editTracking` と同じユーザー名）。**必須** | `assignee` |
| `STATUS` | 状態 | `String(16)` | `作業中`・`保留`・`完了` | `status` |
| `NOTE` | メモ | `String(2000)` | メモ | `note` |
| `TAKEN_AT` | 担当開始 | `Date` | 担当にした日時 | `takenAt` |
| `DONE_AT` | 完了日時 | `Date` | 完了にした日時 | `doneAt` |

### `TBL_HANDOVER_MEMO`（引き継ぎメモ）

1 メモ 1 行（形状なし）。`handover.table.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `TEXT` | 本文 | `String(100000)` | メモの全文。**必須** | `text` |
| `MEMO` | 申し送り | `String(4000)` | 手で書いた申し送り | `memo` |
| `PERIOD_START` | 期間開始 | `Date` | メモの期間の始まり | `start` |
| `PERIOD_END` | 期間終了 | `Date` | 期間の終わり（次のメモの始まり） | `end` |
| `WRITTEN_AT` | 作成日時 | `Date` | 書いた日時 | `writtenAt` |
| `WRITTEN_BY` | 作成者名 | `String(255)` | 書いた人 | `writtenBy` |

### `TBL_EVENT`（イベント）

イベントの範囲のポリゴン（範囲を持たないなら形状なしのテーブルでもよい）。`events.layer.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `EVENT_ID` | イベントID | `String(64)` | イベントの ID（UUID）。**必須** | `id` |
| `EVENT_NAME` | イベント名 | `String(255)` | 名前。**必須** | `name` |
| `EVENT_KIND` | 種類 | `String(64)` | 地震・津波・事故など | `kind` |
| `STATUS` | 状態 | `String(16)` | `対応中`・`終了` | `status` |
| `START_AT` | 開始 | `Date` | 期間の始まり | `start` |
| `END_AT` | 終了 | `Date` | 期間の終わり | `end` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_EVENT_LINK`（イベントの紐付け）

イベントと項目の 1 組 1 行（形状なし。多対多）。`events.links.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `EVENT_ID` | イベントID | `String(64)` | `TBL_EVENT` の `EVENT_ID`。**必須** | `event` |
| `ITEM_TYPE` | 項目の種類 | `String(16)` | `image`・`observation`・`request`。**必須** | `type` |
| `ITEM_ID` | 項目ID | `String(128)` | 画像 ID・記録 ID・依頼 ID。**必須** | `item` |
| `ITEM_LABEL` | 項目名 | `String(1000)` | 一覧に出す名前 | `label` |
| `ITEM_TIME` | 項目の日時 | `Date` | 撮像日時など | `time` |
| `SOURCE` | 出典 | `String(1000)` | カタログ・保存先・台帳の URL | `source` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_AI_RESULT`（外部 AI の結果）

外部の AI が書く検出結果（ポリゴンかポイント）。プラグインは読むだけ。`aiResultSources[].fields` で指定（既定なし。`imageId` は必須）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `RESULT_ID` | 結果ID | `String(128)` | 結果の ID | `id` |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像カタログの画像 ID。**必須** | `imageId` |
| `CLASS` | クラス | `String(255)` | AI のクラス（`kinds` で種別に読み替え） | `class` |
| `SCORE` | 確信度 | `Double` | 0〜1 | `score` |
| `MODEL` | モデル | `String(255)` | モデルの名前 | `model` |
| `DETECTED_AT` | 検出日時 | `Date` | 検出した日時 | `detectedAt` |
| `OBJECT_NAME` | モノの名前 | `String(255)` | モノの名前 | `name` |
| `KIND` | 種別 | `String(1000)` | 種別（分類のパス） | `kind` |
| `ACTIVITY` | 活動 | `String(255)` | 活動 | `activity` |
| `LENGTH_M` | 長さ | `Double` | 長さ（m） | `length` |
| `HEADING` | 向き | `Double` | 向き（度） | `heading` |
| `WIDTH_M` | 幅 | `Double` | 幅（m） | `width` |
| `NOTE` | 備考 | `String(4000)` | 備考 | `note` |

### `TBL_DETECTION`（検出結果の転記先）

プラグインの物体検出の結果を転記するレイヤー（ポリゴンか、中心点を書くポイント）。`detectionOutputs[].fields` で指定（既定なし。書いた役割だけ書き込む）。`constants` で決まった値を書く属性（`SOURCE` など）も足せる。ドック「AI結果」がこのレイヤーも画像 ID で読み、ほかの人の検出を確かめられる（`IMAGE_ID` が要る。読まないなら `"aiResults": false`。[AI結果](../features/ai.md#ai結果外部aiプラグイン内の物体検出)）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID | `imageId` |
| `FILE_NAME` | ファイル名 | `String(255)` | 画像のファイル名 | `fileName` |
| `IMAGE_TIME` | 撮像日時 | `Date` | 撮像日時 | `imageTime` |
| `DETECTED_AT` | 検出日時 | `Date` | 検出した日時 | `detectedAt` |
| `SENT_AT` | 転記日時 | `Date` | 転記した日時 | `sentAt` |
| `CLASS` | クラス | `String(255)` | クラス名 | `class` |
| `CLASS_ID` | クラス番号 | `Integer` | クラスの番号 | `classId` |
| `SCORE` | 確信度 | `Double` | 0〜1 | `score` |
| `MODEL` | モデル | `String(255)` | モデルの名前 | `model` |
| `STATUS` | 状態 | `String(16)` | `AI`・`修正`・`手動`（`statusValues` で変更可） | `status` |
| `LON` | 経度 | `Double` | 中心の経度 | `lon` |
| `LAT` | 緯度 | `Double` | 中心の緯度 | `lat` |
| `AREA_M2` | 面積 | `Double` | 面積（m²） | `area` |
| `LENGTH_M` | 長辺 | `Double` | 長辺（m） | `length` |
| `WIDTH_M` | 短辺 | `Double` | 短辺（m） | `width` |
| `HEADING` | 向き | `Double` | 向き（度） | `angle` |
| `NOTE` | 備考 | `String(4000)` | 備考 | `note` |

### `TBL_PLACE_NAME`（地名検索）

閉域で「座標・地名へ移動」の地名を引くレイヤー（ふつうはポイント）。`geocoders[]` に `{ "type": "features", "url": …, "field": ["PLACE_NAME", "PLACE_NAME_KANA", "PLACE_ALIAS"] }`（地名だけなら `"field": "PLACE_NAME"`）。読み・別名のエイリアスは、読み・別名で見つかったときに候補の横に出る名前になる。

| 属性名 | エイリアス | 型 | 中身 | 設定 |
| --- | --- | --- | --- | --- |
| `PLACE_NAME` | 地名 | `String(255)` | 地名（部分一致で検索。候補に出る名前） | `field`（先頭） |
| `PLACE_NAME_KANA` | 読み | `String(255)` | 読み（ひらがなかカタカナ。なくてもよい） | `field`（2 番目以降） |
| `PLACE_ALIAS` | 別名 | `String(255)` | 別名・略称（複数なら「,」区切りで 1 つの値に。なくてもよい） | `field`（2 番目以降） |

### `TBL_COUNTRY`（国）

国のポリゴン（記録を含むものが通報の `country`・`countryName`。重なるときはいちばん小さいもの）。領海・EEZ を含めた範囲にしておけば海上の記録にも国が付く。ほかのシステムの国境レイヤーでもよく、属性名が違えば `alertReports.countries.fields` で合わせる（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `COUNTRY_CODE` | 国コード | `String(8)` | 国コード（ISO 3166 の `JP`・`JPN` など。通報の `country`） | `code` |
| `COUNTRY_NAME` | 国名 | `String(255)` | 国名（通報の `countryName`） | `name` |
| `COUNTRY_NAME_EN` | 国名（英語） | `String(255)` | 英語の国名（通報の `countryName.en`。なくてもよい） | `nameEn` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 で使わない | `active` |

### `TBL_REQUEST_GROUP`（依頼グループ）

撮像依頼の依頼グループ（だれの依頼か）の一覧。1 行 1 グループ。グループは人や業務とともに変わるので、設定ファイルではなくこのテーブルに置く（設定ファイルにはテーブルの URL `requestStores[].groups` だけ。属性名が違えば `requestStores[].groupFields` で合わせる）。行の追加・変更・削除はプラグインの「依頼グループ…」（撮像を依頼する人・撮像計画の担当。2026-11-23 から）で行う。ArcGIS Pro や Portal で直してもよい。プラグインが書くので、作成者・更新者の 4 属性（`CREATED_DATE` ほか。[型と共通の属性](#型と共通の属性)）も置く。2026-11-23 より前に作った環境では `tools/arcgis/create_tables.py --grant-edit` をもう一度実行し、サービスでこのテーブルの編集（追加・更新・削除）を許可する。マップサービス（`…/MapServer/21`）の URL のままなら一覧を見るだけになる。テーブルを読めないときは、依頼に付いているグループで絞り込める（入力は自由入力になる）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `GROUP_NAME` | グループ名 | `String(255)` | グループの名前。台帳・出力履歴の `REQUEST_GROUP` にこの名前が入る（大文字・小文字と前後の空白は区別しない）。**必須** | `name` |
| `MEMBERS` | メンバー | `String(4000)` | メンバーのユーザー名（環境変数 `USERNAME` と同じ。`editTracking.user` を使っていればその名前）。カンマ・読点・空白・改行で区切る。「自分のグループ」の絞り込みと、新しい依頼の最初のグループに使う | `users` |
| `SORT_ORDER` | 表示順 | `Integer` | 一覧に出す順（小さいほど先。空は後ろで名前順） | `order` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 なら新しい依頼には選べない（そのグループの依頼は絞り込みで「（使用停止）」として残る）。空・1 は使う | `active` |
| `NOTE` | メモ | `String(1000)` | グループの説明（絞り込みの吹き出し） | `note` |

「依頼グループ…」でグループの名前を変えると、台帳の `REQUEST_GROUP` もその名前に直す（一括編集と同じ保存。ほかの人が先に変えた依頼は上書きせずに伝える）。ArcGIS Pro などで名前を変えたときは、台帳も同じ名前に直す（「撮像依頼」ドックで古いグループで絞り込み、全件を選んで「一括編集…」）。出力履歴の過去の行は書き出したときの名前のまま。依頼が 1 件でも入っているグループは「依頼グループ…」で削除できない（使わなくなったら `ACTIVE` を 0 にする）。

### `TBL_REQUEST_SHOT`（撮像の範囲）

書き出したシステム向けファイルの 1 行ごとの撮像（2026-11-11 に追加。tsudo「マージした結果、どの範囲になったかも見たい。他方、要求にどう呼応したかも知りたい」）。ポリゴンは撮像するシーンの外形（まとめた行はファイルに書いた四角＝まとめた依頼を囲むパスに平行な四角、1 回の撮像に収まらない大きな範囲は撮像ごとのポリゴン）。依頼ごとの記録は [`TBL_REQUEST_HISTORY`](#tbl_request_history撮像依頼の出力履歴)（依頼の範囲）のまま、その `SHOT_ID` でこの表の行をたどる。軌道を使わずに詰めた行（`orbit: false`・TLE なし）も、依頼の範囲・撮像時刻なしで記録する（2026-11-11 から。1 枚の画像について管理することはこの表に置く: tsudo「個の画像に関して管理する情報はショットのほうがいい」）。`CAPTURED`・`IMAGE_ID` は照合で依頼の行が変わるたびに、その撮像の依頼の行から入れ直す（どれかが撮像済みなら撮像済み、全部が未撮像なら未撮像、ほかは未判定。照合はプラグインの「照合…」と、QGIS なしの定期照合のどちらでも同じ処理）。`UNCOUNTED_SCENES`・`UNCOUNTED_REASON` は「年間撮像上限」の画面で入れる、システムが年間の枚数に数えない分（[`TBL_IMAGING_QUOTA`](#tbl_imaging_quota年間撮像上限)）。`USE_START`・`USE_END`・`PASS_ID` はその撮像で衛星を使う時間とパス（2026-11-14 に追加。軌道の使用時間の上限で、前後の日の撮像と同じパスを数えるのに読む。ない表でも動く）。「置き換えて書き出し」では、その日（システムごとに書き出したときはそのシステム）の前回の行を消してから入れ直す（採否ごと残した出力履歴の行と、照合済みの行の撮像は残す）。テーブルがなくても動く（撮像予定ドックは `MERGED_INTO` でまとめを表示し、範囲は出さない）。`requestStores[].shotFields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `SHOT_ID` | 撮像ID | `String(64)` | 撮像の ID（出力 ID と番号）。**必須** | `shotId` |
| `RUN_ID` | 出力ID | `String(64)` | 出力 1 回の ID（出力履歴の `RUN_ID` と同じ） | `runId` |
| `RUN_DATE` | 出力日時 | `Date` | 出力した日時 | `runDate` |
| `TARGET_DAY` | 対象日 | `String(16)` | 対象の日（`2026-11-11`）。**必須** | `day` |
| `TARGET_DATE` | 対象日（日付） | `Date` | 対象の日の日本時間 0 時（ダッシュボードの日付の絞り込み用） | `targetDate` |
| `SYSTEM` | システム | `String(255)` | 書き出した衛星システム | `system` |
| `SATELLITE` | 衛星 | `String(255)` | 撮る衛星 | `satellite` |
| `PASS_TIME` | 撮像時刻 | `Date` | 撮像の真ん中の時刻（軌道から。大きな範囲は最初の撮像） | `passTime` |
| `OFF_NADIR` | オフナディア角 | `Double` | 撮像のオフナディア角（°） | `offNadir` |
| `INCIDENCE` | 入射角 | `Double` | 撮像の入射角（°） | `incidence` |
| `ORBIT_DIR` | 軌道方向 | `String(16)` | `昇交`・`降交` | `orbitDir` |
| `LOOK_SIDE` | 観測方向 | `String(16)` | `右`・`左` | `lookSide` |
| `SCENES` | シーン数 | `Integer` | 連続シーン数（大きな範囲は撮像すべての合計） | `scenes` |
| `SHOT_COUNT` | 撮像回数 | `Integer` | 撮像の回数（1 回に収まらない大きな範囲だけ 2 以上） | `shots` |
| `COVERAGE` | 範囲の割合 | `Double` | 大きな範囲で撮れる割合（%。1 回に収まる行は空。衛星で分担した行はそのショットが撮る分） | `coverage` |
| `REQ_ID` | 依頼ID | `String(64)` | ファイルでその行になった依頼の `REQ_ID` | `requestId` |
| `REQ_IDS` | まとめた依頼ID | `String(4000)` | この撮像で撮る依頼の `REQ_ID`（カンマ区切り、行の依頼が先頭） | `requestIds` |
| `REQ_NAMES` | まとめた依頼の名称 | `String(4000)` | 同じ依頼の名称（読点 `、` 区切り） | `names` |
| `REQ_COUNT` | 依頼件数 | `Integer` | この撮像で撮る依頼の件数（まとめていなければ 1） | `count` |
| `OUTPUT_FILE` | 出力ファイル | `String(1000)` | 書き出したファイル | `file` |
| `SENSOR` | センサー | `String(255)` | 撮るセンサー（センサーモードの `SENSOR`。衛星の「センサー」がなければ空）。年間撮像上限の数え先 | `sensor` |
| `SENSOR_MODE` | センサーモード | `String(255)` | 撮るモード（モードのない衛星は空） | `mode` |
| `CAPTURED` | 撮像結果 | `String(16)` | `未判定`・`撮像済み`・`未撮像`（その撮像の依頼の出力履歴の行から。書き出したときは未判定） | `captured` |
| `IMAGE_ID` | 画像ID | `String(255)` | 撮像済みにした画像の ID（ファイルの行になった依頼の行のもの） | `imageId` |
| `UNCOUNTED_SCENES` | 数えない枚数 | `Integer` | システムが年間の枚数に数えない枚数（ノイズ・ジャミングなど。空は全部数える） | `uncounted` |
| `UNCOUNTED_REASON` | 数えない理由 | `String(1000)` | 数えない理由 | `uncountedReason` |
| `USE_START` | 使用開始 | `Date` | 衛星を使い始める時刻（撮像の始まり − システムの準備秒 `useTime.setupSeconds`。軌道を使わずに詰めた行は空）。[軌道の使用時間の上限](../imaging-requests.md) | `useStart` |
| `USE_END` | 使用終了 | `Date` | 衛星を使い終わる時刻（撮像の終わり） | `useEnd` |
| `PASS_ID` | パス番号 | `String(20)` | 撮像のパス（半周）の番号: TLE の周回番号から数えた周回番号と向き（`45123昇`・`45123降`） | `passId` |

### `TBL_IMAGING_QUOTA`（年間撮像上限）

センサー・モードごとの 1 年の撮像枚数の上限（2026-11-11 に追加。tsudo「センサー、モードごとに、年間撮像枚数の上限があるので、それを管理したい」「年の途中から運用しているので、当初年は現在の消費枚数を設定したい」「9月から1年とかあるので、ウィンドウは変えられるように」）。1 行 1 上限。上限のないセンサー・モードは行を作らない（「全部あるわけではない」）。契約で変わる値なので設定ファイルではなくこのテーブルに置き、[衛星・センサー編集ツール](config-file.md#衛星センサー編集ツールtoolssatellite_editorhtml)（衛星のページの「年間撮像上限」）か ArcGIS Pro・Portal で編集する。プラグインは読むだけ。設定ファイルには `requestStores[].quotas`（属性名が違えば `requestStores[].quotaFields`）。消費は撮像ごとに数えるので、[`TBL_REQUEST_SHOT`](#tbl_request_shot撮像の範囲)（`requestStores[].shots`）も要る。

- **消費** = 当初の消費枚数（`COUNT_FROM` の日が入る年だけ）＋ その年の撮像のうち撮像済みの枚数（`SCENES`）− 数えない枚数（`UNCOUNTED_SCENES`）。`COUNT_FROM` が入る年は、その日より前の撮像は数えない（当初の消費枚数に入っている）
- **見込み** = 出力して結果がまだの撮像の枚数（システムが不採用と答えたもの・未撮像になったものは数えない）。残り = 上限 − 消費 − 見込み
- 撮像がどの上限にかかるかは、撮像の `SENSOR`（なければ衛星名 `SATELLITE`）と `SENSOR_MODE` で決める。`SENSOR_MODE` が空の行は、そのセンサーの全モードの合計にかかる。1 つの撮像が複数の行（モードの行と全モードの行）にかかれば、どれも守る
- 1 年は `YEAR_START_MONTH` の月の 1 日から（日本時間の対象日で区切る）

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `SENSOR` | センサー | `String(255)` | センサー名（[`TBL_SENSOR_MODE`](#tbl_sensor_modeセンサーモード) の `SENSOR`）か、モードのない衛星なら衛星名。大文字・小文字・記号は区別しない。**必須** | `sensor` |
| `SENSOR_MODE` | センサーモード | `String(255)` | モード名（`TBL_SENSOR_MODE` の `SENSOR_MODE`）。空はそのセンサーの全モード | `mode` |
| `ANNUAL_LIMIT` | 年間上限 | `Integer` | 1 年の枚数（シーン数）。空は上限なし（消費だけ数える） | `limit` |
| `YEAR_START_MONTH` | 年の始まりの月 | `SmallInteger` | 1 年が始まる月（1〜12。9 なら 9 月 1 日から翌年 8 月 31 日）。空は 1（1 月から） | `startMonth` |
| `INITIAL_USED` | 当初の消費枚数 | `Integer` | `COUNT_FROM` より前にその年に使った枚数（プラグインで数え始める前の分） | `initialUsed` |
| `COUNT_FROM` | 数え始める日 | `Date` | 撮像の範囲で数え始める日（日本時間）。`INITIAL_USED` を書くときは必須 | `countFrom` |
| `NOTE` | メモ | `String(1000)` | 契約の番号など | `note` |

### `TBL_INTEREST_POINT`（関心地点）

一人ひとりが自分で決める関心地点（2026-11-14 に追加。[個人の関心地域・地点](../features/images.md#個人の関心地域地点interests)）。1 行 1 地点。持ち主（`OWNER_USER`）が自分の行だけを読み書きする（大文字・小文字は区別しない）。`interests.points.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `INTEREST_ID` | 関心ID | `String(64)` | ID（UUID）。**必須** | `id` |
| `INTEREST_NAME` | 名前 | `String(255)` | 名前（カードの「写っている地物」に出る）。**必須** | `name` |
| `OWNER_USER` | 持ち主 | `String(255)` | 持ち主のユーザー名（[`editTracking`](operations.md#作成者更新者の記録edittracking) と同じ。既定は環境変数 `USERNAME`）。**必須** | `owner` |
| `COLOR` | 色 | `String(32)` | 色（`#rrggbb`）。カードの行の背景と地図の色 | `color` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_INTEREST_AREA`（関心地域）

一人ひとりが自分で決める関心地域のポリゴン（2026-11-14 に追加）。属性は [`TBL_INTEREST_POINT`](#tbl_interest_point関心地点) と同じ。`interests.areas.fields`。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `INTEREST_ID` | 関心ID | `String(64)` | ID（UUID）。**必須** | `id` |
| `INTEREST_NAME` | 名前 | `String(255)` | 名前。**必須** | `name` |
| `OWNER_USER` | 持ち主 | `String(255)` | 持ち主のユーザー名。**必須** | `owner` |
| `COLOR` | 色 | `String(32)` | 色（`#rrggbb`） | `color` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_USER_ROLE`（担当）

一人ひとりの担当（2026-11-15 に追加。[担当と使える機能](../features/screen.md#担当と使える機能userroles)）。1 人 × 1 担当で 1 行。各自が「担当と個人設定…」でチェックすると、自分の行を書き・消す。管理者がこの表を直接直してもよい（次の起動からその担当）。`userRoles.fields`。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `USER_NAME` | 利用者名 | `String(255)` | ユーザー名（[`editTracking`](operations.md#作成者更新者の記録edittracking) と同じ。既定は環境変数 `USERNAME`。大文字・小文字は区別しない）。**必須** | `user` |
| `ROLE_NAME` | 担当 | `String(32)` | `PLANNER`（撮像計画の担当）・`REQUESTER`（撮像を依頼する人）・`OBSERVER`（観測記録の担当）・`INTERPRETER`（画像判読）・`TRAINER`（AI 学習の担当）・`INSTRUCTOR`（判読練習の指導者）・`ADMIN`（管理者）。**必須** | `role` |

### `TBL_SATELLITE_OUTAGE`（衛星の障害情報）

衛星かシステム（発注先）の障害 1 件で 1 行（2026-11-16 に追加。システムの行は 2026-11-24。[衛星の障害情報](../features/plan.md#衛星の障害情報satelliteoutages)）。プラグインの「衛星・システムの障害情報…」で書く（ArcGIS Pro・Portal で直してもよい）。`SAT_NAME` か `SYSTEM_NAME` のどちらかを入れる（両方あれば衛星の行。`SYSTEM_NAME` の列がない表には衛星の行だけ書ける）。システムの行の影響はそのシステムの衛星すべてにかかり（`STOP_IMAGING` は受付停止）、1 日の件数・枚数の上限はシステムの行だけ。センサー・モードはシステムの行では見ない。復旧しても行は消さない（履歴）。影響のチェックの属性は 1（`はい`・`true` などの文字も可）でその影響あり、空か 0 でなし。上限の属性は空でその影響なし。`satelliteOutages.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `OUTAGE_ID` | 障害ID | `String(64)` | ID（`OUT-20261116-181530-1a2b`）。**必須** | `id` |
| `SAT_NAME` | 衛星名 | `String(255)` | 衛星カタログの名前・ID・別名か、シリーズ名（そのシリーズの全機）。**これか `SYSTEM_NAME` が必須** | `satellite` |
| `SYSTEM_NAME` | システム名 | `String(255)` | システム（設定ファイルの `requestExports` の `system`）。そのシステムの衛星すべてにかかる。2026-11-24 に追加 | `system` |
| `SENSOR` | センサー | `String(255)` | このセンサーだけ（空は全センサー） | `sensor` |
| `SENSOR_MODE` | センサーモード | `String(255)` | このモードだけ（空は全モード） | `mode` |
| `OUTAGE_REASON` | 理由 | `String(1000)` | 障害の理由 | `reason` |
| `START_TIME` | 発生日時 | `Date` | 発生日時（計画停止なら始まる日時）。**必須** | `start` |
| `EXPECTED_END` | 復旧見込み | `Date` | 復旧の見込み。空は未定 | `expected` |
| `RECOVERED_AT` | 復旧日時 | `Date` | 復旧した日時。空はまだ | `recovered` |
| `STOP_IMAGING` | 撮像停止 | `SmallInteger` | 1 で撮像停止（システムの行は受付停止: 期間中の撮像機会をそのシステムに出さない） | `stop` |
| `NO_ASC_RIGHT` | 昇交右不可 | `SmallInteger` | 1 で昇交・右向きを使わない | `noAscRight` |
| `NO_ASC_LEFT` | 昇交左不可 | `SmallInteger` | 1 で昇交・左向きを使わない | `noAscLeft` |
| `NO_DESC_RIGHT` | 降交右不可 | `SmallInteger` | 1 で降交・右向きを使わない | `noDescRight` |
| `NO_DESC_LEFT` | 降交左不可 | `SmallInteger` | 1 で降交・左向きを使わない | `noDescLeft` |
| `LIMIT_OFF_NADIR` | オフナディア角の上限 | `Double` | 障害中のオフナディア角の上限（度） | `maxOffNadir` |
| `LIMIT_PER_DAY` | 1日の撮像回数の上限 | `Integer` | 障害中の 1 日の撮像回数の上限 | `maxPerDay` |
| `LIMIT_SCENES_PER_PASS` | 1パスのシーン数の上限 | `Integer` | 障害中の 1 パスのシーン数の上限 | `scenesPerPass` |
| `LIMIT_INTERVAL_MIN` | 撮像間隔の下限 | `Double` | 障害中の撮像間隔の下限（分） | `minInterval` |
| `NO_NIGHT` | 夜間不可 | `SmallInteger` | 1 で夜間（撮像の場所で太陽が地平線より下）を使わない | `noNight` |
| `LIMIT_ORDERS_PER_DAY` | 1日の件数の上限 | `Integer` | システムの行だけ: 期間にかかる日の、システムへ出す件数の上限（設定ファイルの `limit.count` との小さいほう）。2026-11-24 に追加 | `ordersPerDay` |
| `LIMIT_SCENES_PER_DAY` | 1日の枚数の上限 | `Integer` | システムの行だけ: 期間にかかる日の、システムへ出す枚数の上限（`limit.scenes` との小さいほう）。2026-11-24 に追加 | `scenesPerDay` |
| `OTHER_IMPACT` | その他の影響 | `String(1000)` | 計算に使わない影響（注意として出す） | `other` |
| `NOTE` | メモ | `String(4000)` | メモ | `note` |

### `TBL_GLOSSARY`（備考の単語集）

備考に使う語 1 つで 1 行（2026-11-17 に追加。[備考の単語集](../features/observations.md#備考の単語集glossary)）。プラグインは読むだけで、ArcGIS Pro・Portal で直す。同じ語が 2 行にあれば 1 語にまとめる（表記ゆれ・よみ・大分類は両方のもの）。`glossary.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `TERM` | 語 | `String(255)` | 備考に使う語（`入港`・`停泊中`）。**必須** | `term` |
| `VARIANTS` | 表記ゆれ | `String(1000)` | この語に直す別の書き方（`入航,入湊`。カンマ・読点区切り） | `variants` |
| `READING` | よみ | `String(255)` | よみ（ひらがな、カンマ区切りで複数）。よみを打っても候補に出す | `reading` |
| `ROOT_KIND` | 大分類 | `String(1000)` | この語を出す大分類（`船舶`、カンマ区切りで複数）。空はすべて | `kinds` |
| `DESCRIPTION` | 説明 | `String(1000)` | 候補の横に出す説明 | `description` |
| `SORT_ORDER` | 並び順 | `Integer` | 候補の並び順（小さい順。空は後ろ） | `order` |

### `TBL_TRAINING_EXERCISE`（判読の練習の問題）

[判読の練習](../features/screen.md#練習モードtraining)の組織の問題 1 つで 1 行（2026-11-22 に追加）。形状は問題の範囲（解く人の地図に橙の点線で出し、地図をそこへ動かす。空は画像の全体）。判読の練習の指導者が本番の MIKAGE の「判読の練習の問題を作る…」で書き（ArcGIS Pro・Portal で入れてもよい）、練習モードでは「判読の練習…」を開くたびに読む（読むだけ）。この表と `TBL_TRAINING_ANSWER` の書き込みの権限は指導者だけに、ほかの人は読むだけにするとよい。正解は `TBL_TRAINING_ANSWER` に `EXERCISE_ID` で結ぶ。正解のない問題・使えない行は出さず、練習モードの間に理由を出す。属性名は下の名前で決まり（大文字・小文字は問わない。`fields` はない）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `EXERCISE_ID` | 問題ID | `String(64)` | 問題の ID（`HARBOR-01`）。正解・結果がこれで結ぶ。**必須** |  |
| `TITLE` | 題名 | `String(255)` | 一覧に出す題名（空は ID） |  |
| `INSTRUCTION` | 指示文 | `String(4000)` | 解く人への指示（何を・どこまで記録するか） |  |
| `EXERCISE_TYPE` | 種類 | `String(32)` | `見つける`・`見分ける`・`測る`・`変化を見つける`（`find`・`identify`・`measure`・`change` も可）。**必須** |  |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像カタログの画像 ID。練習用のカタログになければ本番の画像カタログ（`imageCatalogs`、`fields.id` のあるもの）で引いて開く（読むだけ） |  |
| `IMAGE_SOURCE` | 画像のファイル | `String(1000)` | 画像 ID がないときの画像のファイルか URL（その PC から開けるもの）。`IMAGE_ID` かこれが**必須** |  |
| `BEFORE_IMAGE_ID` | 前の画像ID | `String(255)` | 変化を見つける問題の前の画像の ID |  |
| `BEFORE_IMAGE_SOURCE` | 前の画像のファイル | `String(1000)` | 前の画像のファイルか URL。変化を見つける問題は前の画像が**必須** |  |
| `TARGET_KINDS` | 対象の種別 | `String(1000)` | 記録する種別（`船舶`。`;` 区切りで複数。その下の種別も含む）。空はすべて |  |
| `DIFFICULTY` | 難しさ | `String(32)` | 一覧に出す難しさ（`易`・`中`・`難` など） |  |
| `TIME_LIMIT_MIN` | 制限時間 | `Double` | 制限時間（分）。空は制限なし。試験は時間が来たら自動で提出 |  |
| `EXERCISE_MODE` | 形式 | `String(16)` | `練習` か `試験`（空は練習）。試験は 1 回だけ（この表の結果とこの PC の結果で数える）・ヒントなし |  |
| `SHOW_ANSWERS` | 答えを見せる | `SmallInteger` | 0 で答え合わせに正解の場所と解説を出さない（点と合否だけ）。空は出す |  |
| `PASS_SCORE` | 合格点 | `Double` | 合格点（0〜100）。空は採点の決まりの合格点 |  |
| `SCORING` | 採点の決まり | `String(4000)` | この問題だけの採点の決まり（JSON。設定ファイルの `training.scoring` と同じ書き方で、変える所だけ） |  |
| `COURSE_NAME` | コース | `String(255)` | コース（一覧はコースごと） |  |
| `COURSE_ORDER` | コースの中の順番 | `Integer` | コースの中の順番（「次の問題へ」の順） |  |
| `ROLES` | 対象の担当 | `String(1000)` | この問題を出す担当の符号（`OBSERVER,ANALYST`）。空はだれにでも |  |
| `ACTIVE` | 出す | `SmallInteger` | 0 で出さない（作りかけ）。空は出す |  |

### `TBL_TRAINING_ANSWER`（判読の練習の正解）

判読の練習の正解のモノ 1 つで 1 点（2026-11-22 に追加）。経験者が確かめた答えを入れる。形状はモノの場所（中心）。変化を見つける問題で消えたモノ（`消失`）は前の画像での場所。属性名は下の名前で決まり。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `EXERCISE_ID` | 問題ID | `String(64)` | 問題の ID（`TBL_TRAINING_EXERCISE`）。**必須** |  |
| `ANSWER_NO` | 番号 | `Integer` | 地図と答え合わせの番号（空は表の順に空いている番号） |  |
| `KIND` | 種別 | `String(1000)` | 分類体系の種別（`船舶 > 貨物船 > コンテナ船`）。近い種別には部分点 |  |
| `ACTIVITY` | 活動 | `String(255)` | 活動（`停泊`・`航行` など。空は採点しない） |  |
| `LENGTH_M` | 長さ | `Double` | 長さ（m）。測る問題で採点する |  |
| `WIDTH_M` | 幅 | `Double` | 幅（m） |  |
| `HEADING` | 向き | `Double` | 向き（度。北から時計回り、後ろから前） |  |
| `CHANGE_KIND` | 変化 | `String(16)` | 変化を見つける問題の `来た`・`継続`・`消失` |  |
| `ANSWER_POINTS` | 配点 | `Double` | この答えの配点（空は 10） |  |
| `TOLERANCE_M` | 位置の許容 | `Double` | この答えの位置の許容（m。空は採点の決まり） |  |
| `REQUIRED` | 必ず見つける | `SmallInteger` | 0 で加点の答え（見つければ加点、見落としても減らない）。空は 1 |  |
| `EXPLANATION` | 解説 | `String(4000)` | 答え合わせに出す解説（見分け方の手がかり） |  |
| `REFERENCE_IMAGE` | 参考写真 | `String(1000)` | 参考写真のファイルか URL |  |

### `TBL_TRAINING_RESULT`（判読の練習の結果）

判読の練習の提出 1 回で 1 行（2026-11-22 に追加）。練習モードの中で本番に書き込むのはこの表だけ。表がなければ（`training.exercises.results` を書かなければ）結果はその PC だけに残す。送れなかった結果は、次に「判読の練習…」を開くときに送り直す。ない属性は書かない。判読練習の指導者の「判読の練習の成績…」は、この表のすべての行（`DETAIL` も）を読む。判読する人には書き込み（足すだけ）、指導者には読む権限を与える。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `EXERCISE_ID` | 問題ID | `String(64)` | 問題の ID（組織の問題。見本の問題は `見本:sample-find` のように頭に `見本:`） |  |
| `TITLE` | 題名 | `String(255)` | 問題の題名（そのときの） |  |
| `COURSE_NAME` | コース | `String(255)` | コース |  |
| `USER_NAME` | 解いた人 | `String(255)` | 解いた人（作成者の 4 属性と同じ利用者名。`editTracking`） |  |
| `EXERCISE_MODE` | 形式 | `String(16)` | `練習` か `試験` |  |
| `ATTEMPT` | 何回目 | `Integer` | その問題の何回目か |  |
| `STARTED` | 始めた日時 | `Date` | 始めた日時 |  |
| `FINISHED` | 提出した日時 | `Date` | 提出した日時 |  |
| `SECONDS` | かかった秒数 | `Integer` | かかった秒数 |  |
| `SCORE` | 点 | `Double` | 点（0〜100） |  |
| `PASSED` | 合格 | `SmallInteger` | 1 で合格 |  |
| `FOUND` | 見つけた数 | `Integer` | 見つけた答えの数 |  |
| `MISSED` | 見落とした数 | `Integer` | 見落とした答えの数 |  |
| `EXTRA` | 誤りの数 | `Integer` | 答えにない記録の数 |  |
| `KIND_SCORE` | 種別の正しさ | `Double` | 見つけた答えの種別の正しさ（%） |  |
| `HINTS` | ヒントを見た回数 | `Integer` | ヒントを見た回数 |  |
| `DETAIL` | 答えごとの結果 | `String(30000)` | 答えごとの結果と項目ごとの点（JSON。取り違えた種別の集計に使う） |  |

### `TBL_IMAGE_CORRECTION`（画像の幾何補正）

[クイック幾何補正](../features/enhance.md#クイック幾何補正)で直した画像のファイル 1 つで 1 行（2026-11-23 に追加）。プラグインの「クイック幾何補正」の「保存」で書き（同じ画像のファイルの行があれば上書き）、画像を開くたびに画像 ID で読んで、だれが開いても補正した位置で表示する。「保存した補正を削除」で行を消すと、補正なしに戻る。補正した画像はファイルを変えない（この表の値から、開いた PC で作り直す）。`imageCorrections.fields`（既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像カタログの画像 ID（カタログ外の画像はファイル名。観測記録の `IMAGE_ID` と同じ）。**必須** | `imageId` |
| `IMAGE_FILE` | 画像のファイル | `String(1000)` | 画像のファイル（フォルダのレコードはフォルダからのパス、ほかはファイル名。観測記録の `IMAGE_FILE` と同じ）。空はその画像のどのファイルにも | `imageFile` |
| `TIE_POINTS` | 目印の点 | `String(4000)` | 目印の点（JSON: `[[画素X, 画素Y, 経度, 緯度], …]`。画素は元のファイルの左上から右・下、経緯度は本当の位置（WGS 84）。40 点まで）。**必須**（目印なしは `[]`） | `points` |
| `USE_DEM` | 地形の補正 | `SmallInteger` | 1 で地形（DEM）でも補正する | `terrain` |
| `LOOK_AZIMUTH` | 衛星の方角 | `Double` | 入力した衛星の方角（度。画像の場所から見た衛星の方位、北から時計回り）。空はファイルから読む | `toward` |
| `INCIDENCE_ANGLE` | 入射角 | `Double` | 入力した入射角（度）。空はファイルから読む | `incidence` |
| `RESIDUAL_M` | 残差 | `Double` | 保存したときの目印の点の残差（m、二乗平均。3 点以上のとき） | `residual` |
| `CORRECTED_BY` | 補正した人 | `String(255)` | 保存した人（作成者の 4 属性と同じ利用者名） | `by` |
| `CORRECTED_AT` | 補正した日時 | `Date` | 保存した日時 | `at` |

### `TBL_AREA_CATEGORY`（地域のカテゴリー）

組織の関心地域のカテゴリー（2026-11-24 に追加。tsudo「新しいテーブルとして、カテゴリーを整理しよう」）。1 行 1 カテゴリー、`PARENT_ID` で木にする（港湾 ▸ 軍港 など）。地域（[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン)）の `CATEGORY_ID` がこの表の `CATEGORY_ID` を持つ（名前を変えても地域とのつながりは変わらない）。追加・変更・並べ替え・削除は「関心地域・地点」の「組織」の「カテゴリー…」で全員ができる。ID を変えると、そのカテゴリーの地域の `CATEGORY_ID` も直す。地域が 1 件でもあるカテゴリーは削除できない（使わなくなったら `ACTIVE` を 0 に）。`namedAreas.categories`（URL か `{ "url", "fields" }`、既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `CATEGORY_ID` | カテゴリーID | `String(64)` | ID（英数字と `-` `_`。`PORT-NAVAL` など）。**必須** | `id` |
| `CATEGORY_NAME` | カテゴリー名 | `String(255)` | 名前（一覧・カードに出る） | `name` |
| `CATEGORY_NAME_EN` | カテゴリー名（英語） | `String(255)` | 英語の名前 | `nameEn` |
| `PARENT_ID` | 上位のカテゴリー | `String(64)` | 上位のカテゴリーの `CATEGORY_ID`（空はいちばん上） | `parent` |
| `SORT_ORDER` | 表示順 | `Integer` | 同じ上位の中での順（小さいほど先） | `order` |
| `COLOR` | 色 | `String(32)` | 色（`#rrggbb`）。色のない地域はこの色（空なら上位のカテゴリーの色） | `color` |
| `ACTIVE` | 有効 | `SmallInteger` | 0 なら地域の入力で選べない（そのカテゴリーの地域はそのまま） | `active` |
| `NOTE` | メモ | `String(1000)` | 説明 | `note` |

### `TBL_REQUEST_AREA_CHANGE`（依頼の範囲の変更記録）

地域（組織の関心地域、[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン)）の形が変わって、MIKAGE がその地域から作った依頼（[`TBL_IMAGING_REQUEST`](#tbl_imaging_request撮像依頼台帳) の `NAMED_AREA_ID`・`AREA_FOLLOW`）の範囲を合わせた記録（2026-11-24 に追加。tsudo「ネームドエリアの形状が更新されたら、撮像依頼も連動して更新されるようなパイプラインが欲しい」）。1 行 1 依頼 1 回。ポリゴンは**合わせる前の依頼の範囲**（「元に戻す」の行は、元に戻す前の範囲）。プラグインは行を足すだけで、変えも消しもしない。撮像依頼ドックの「範囲の変更記録…」で新しい順に見て、「更新」の行（その依頼の最後の変更）を元に戻せる（範囲を合わせる前に戻し、その依頼は地域の形に合わせるをオフ）。テーブルがなくても範囲は合わせる（記録が残らず、あとで元に戻せない）。`requestStores[].areaChanges`（属性名が違えば `requestStores[].areaChangeFields`、既定は下の名前）。

- **いつ合わせるか**: 「関心地域・地点」の「組織」で地域の形を直して保存したとき（すぐ。結果を画面で出す）と、撮像計画ファイル出力の「計算」の前（ArcGIS Pro などほかで直した分。地名のレイヤーの最終編集日時が前回から動いたときだけ見る）
- **合わせる依頼**: 状態が有効・停止で、地域の形に合わせる（`AREA_FOLLOW` = 1）の依頼。完了の依頼と、地域の形に合わせるがオフの依頼は変えない（オフの依頼は撮像依頼ドックの「地域」の列に ⚠ が付く）
- **合わせない地域**: 使用停止・地名の表にない（削除か管理番号の変更）・同じ管理番号が 2 行以上 の地域は合わせず、理由を残す（「対象外」）
- **保留**: ほかの人が先に依頼を変えていたときは保存せず「保留」と残し、次の「計算」でまた試す
- 出力履歴（[`TBL_REQUEST_HISTORY`](#tbl_request_history撮像依頼の出力履歴)）の行は書き換えない。今日以降の行でシステムの採否が不明のものは撮像予定ドックの「理由」に「出し直し候補」（その日を書き出し直すと新しい範囲で出る）、採用済みのものは「システムには前の形で採用済み」（書き出し直しても採否は新しい範囲の行に引き継がない）と出る

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `CHANGE_ID` | 変更ID | `String(64)` | ID（UUID） | `id` |
| `NAMED_AREA_ID` | 地域の管理番号 | `String(255)` | 依頼の地域の管理番号（`TBL_NAMED_AREA` の `NAMED_AREA_ID`） | `areaId` |
| `REQ_ID` | 依頼ID | `String(64)` | 依頼の ID（`TBL_IMAGING_REQUEST` の `REQ_ID`）。**必須** | `requestId` |
| `REQ_NAME` | 依頼の名称 | `String(255)` | そのときの依頼の名称 | `name` |
| `CHANGE_ACTION` | 結果 | `String(16)` | 更新（範囲を地域の形にした）・保留（保存できず、次の計算でまた試す）・対象外（地域が使用停止など）・元に戻す | `action` |
| `REASON` | 理由 | `String(1000)` | 理由（「地域「横須賀港」の形に合わせた」「ほかの人が先に依頼を変更」など） | `reason` |

いつ・誰がは作成者の 4 属性（[`editTracking`](operations.md#作成者更新者の記録edittracking)）で残る。

### `TBL_IMAGE_REVIEW`（画像の評価）

画像が判読に使えるかを、地名ごとに付ける表（2026-11-24 に追加。tsudo「4は、形状ありで、ネームドエリアごと1行にしようか」）。1 行が画像 1 枚 × 地名 1 つ。形状は地名のポリゴンのうち画像に写っている部分（地名のポリゴンを画像の範囲で切ったもの）で、地図やダッシュボードでそのまま色分けできる。`NAMED_AREA_ID` と `NAMED_AREA_NAME` が空の行は画像全体（地名から離れた海など）。同じ画像と地名をもう一度付けると（だれが付けても）同じ行を書き直し、評価者は最後に付けた人になる。空に戻すとその行を消す。地名に管理番号がなければ地名で見分ける。

「観測記録」の分担の行の下の「評価…」で付ける（分担の「完了」で、まだ評価のない画像なら窓が開く）。画像カタログの一覧の「評価」の列に出る。撮像依頼の照合では、依頼の範囲にかかる地名の評価がすべて「使えない」の画像（かかる地名がなければ画像全体が「使えない」の画像）を撮像済みに数えない。もう撮像済みの行は「撮像予定」と「計画と実績の照合」で印を付けるだけで、変えない。`imageReviews`（URL か `{ "url", "fields", "where", "token" }`、既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `REVIEW_ID` | 評価ID | `String(64)` | ID（`REV-20261124-101500-a1b2`。書き直しても変えない）。**必須** | `id` |
| `IMAGE_ID` | 画像ID | `String(255)` | 画像の ID（観測記録・分担と同じ）。**必須** | `imageId` |
| `IMAGE_NAME` | 画像名 | `String(255)` | 画像の名前 | `imageName` |
| `IMAGE_TIME` | 撮像日時 | `Date` | 画像の撮像日時 | `imageTime` |
| `SATELLITE` | 衛星 | `String(255)` | 画像の衛星 | `satellite` |
| `NAMED_AREA_ID` | 地名の管理番号 | `String(255)` | 地名の管理番号（[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン) の `NAMED_AREA_ID`。空は画像全体） | `areaId` |
| `NAMED_AREA_NAME` | 地名 | `String(255)` | 地名（付けたときの名前） | `areaName` |
| `RATING` | 評価 | `String(16)` | `使える`・`一部`・`使えない`。**必須** | `rating` |
| `CLOUD_PCT` | 雲量（%） | `Double` | 見た雲量（0〜100。分からなければ空） | `cloud` |
| `REASONS` | 理由 | `String(1000)` | 理由（不可の理由の言葉を `,` 区切り。`雲,霞`） | `reasons` |
| `NOTE` | メモ | `String(2000)` | メモ | `note` |
| `REVIEWED_BY` | 評価者 | `String(255)` | 最後に付けた人（作成者の 4 属性と同じ利用者名） | `reviewedBy` |
| `REVIEWED_AT` | 評価日時 | `Date` | 最後に付けた日時 | `reviewedAt` |

### `TBL_REFERENCE_INFO`（参考情報）

画像の外から知った情報（発表・航行警報・報道・ほかの部署からの通報など）を、出典と確からしさをつけて場所と日時で残すレイヤー（2026-11-24 に追加。設計方針案「画像の評価・システムの障害・参考情報」の 7）。1 行が情報 1 件、形状はその場所の点（分からなければ空）。ドック「参考情報」で登録・編集し、地図に点で出る。「観測記録」で画像を選ぶと、その撮像日時の前後 `days` 日（既定 7 日）のうち、画像の範囲にある情報と、画像に写る地名（`NAMED_AREA_ID`）の情報の件数が「参考情報 n 件」に出る。だれでも見て登録・編集でき、削除は作成者（作成者の 4 属性の `CREATED_USER`）か管理者だけ（作成者の分からない行はだれでも）。`referenceInfo.layer`（URL か `{ "url", "fields", "where", "token" }`、既定は下の名前）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `INFO_ID` | 情報ID | `String(64)` | ID（`REF-20261124-103000-1a2b`。編集しても変えない）。**必須** | `id` |
| `TITLE` | 題名 | `String(255)` | 題名。**必須** | `title` |
| `INFO_TIME` | 出来事の日時 | `Date` | 出来事の日時（発表の日時ではなく、起きた・見られた日時）。**必須** | `time` |
| `PUBLISHED_AT` | 発表日時 | `Date` | 発表・入手の日時 | `published` |
| `SOURCE` | 出典 | `String(255)` | 出典（発表した組織・媒体。入力欄は過去の出典を候補に出す） | `source` |
| `SOURCE_REF` | 文書番号・URL | `String(1000)` | 文書番号か URL（URL ならドックから開ける） | `sourceRef` |
| `RELIABILITY` | 確からしさ | `String(64)` | 確からしさ（観測記録の確度と同じ言葉。既定は `確定`・`推定`・`不明`） | `reliability` |
| `SUMMARY` | 要旨 | `String(4000)` | 要旨 | `summary` |
| `KIND` | 分類 | `String(1000)` | 分類体系の種別（`船舶 > 支援船`） | `kind` |
| `NAMED_AREA_ID` | 地名の管理番号 | `String(255)` | 何についての情報か（[`TBL_NAMED_AREA`](#tbl_named_area地名のポリゴン) の `NAMED_AREA_ID`） | `areaId` |
| `NOTE` | メモ | `String(2000)` | メモ | `note` |

### `TBL_REFERENCE_LINK`（参考情報の紐付け）

参考情報とイベント・モノ・観測記録の紐付け（1 行が 1 組。同じ参考情報と項目をもう一度紐付けても 1 行のまま）。「観測記録」の記録の「参考情報 ▾」と、「イベント」の「参考情報」の欄で紐付ける。紐付けを外しても参考情報と項目はそのまま。`referenceInfo.links`（URL か `{ "url", "fields", "where", "token" }`、既定は下の名前。なければ紐付けは使わない）。

| 属性名 | エイリアス | 型 | 中身 | 役割 |
| --- | --- | --- | --- | --- |
| `INFO_ID` | 情報ID | `String(64)` | 参考情報の ID（`TBL_REFERENCE_INFO` の `INFO_ID`）。**必須** | `info` |
| `ITEM_TYPE` | 項目の種類 | `String(16)` | `event`（イベント）・`thing`（モノ）・`observation`（観測記録）。**必須** | `type` |
| `ITEM_ID` | 項目ID | `String(255)` | イベント ID・モノ ID（観測記録の `OBJECT_ID`）・記録 ID。**必須** | `item` |
| `LABEL` | 項目名 | `String(255)` | 一覧に出す名前（紐付けたときの名前） | `label` |

### 上の表の名前で作ったときの設定

既定の属性名のある項目（撮像計画・撮像依頼・観測記録・通報・分担・画像の評価・引き継ぎ・イベント・参考情報・関心地域・地点・担当・衛星の障害情報・備考の単語集・判読の練習・画像の幾何補正）は URL だけ書けばよい。既定のない項目は `fields` に属性名を書く。URL はサービスに合わせる。

```jsonc
{
  "imageCatalogs": [
    { "label": "衛星画像", "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/0",   // TBL_IMAGE_CATALOG
      "fields": { "id": "IMAGE_ID", "acquired": "ACQUIRED_AT", "registered": "REGISTERED_AT", "systemRegistered": "SYSTEM_REGISTERED_AT",
                  "sensor": "SENSOR", "angle": "OFF_NADIR", "azimuth": "SAT_AZIMUTH", "source": ["COG_URL", "FILE_PATH"] } }
  ],
  "satelliteCatalogs": [
    { "label": "衛星", "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/1",       // TBL_SATELLITE
      "fields": { "name": "SAT_NAME", "id": "NORAD_ID", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2", "maxOffNadir": "MAX_OFF_NADIR",
                  "minOffNadir": "MIN_OFF_NADIR", "swath": "SWATH", "length": "SCENE_LENGTH", "lookSide": "LOOK_SIDE", "kind": "SENSOR_TYPE",
                  "scenesPerPass": "SCENES_PER_PASS", "minInterval": "MIN_INTERVAL_MIN", "maxPerDay": "MAX_PER_DAY",
                  "shotsPerPass": "SHOTS_PER_PASS", "series": "SERIES", "leadTime": "LEAD_TIME_H", "sensor": "SENSOR", "mode": "SENSOR_MODE",
                  "resolution": "RESOLUTION_M", "system": "SAT_SYSTEM" } }
  ],
  "sensorCatalogs": [
    { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/2",                         // TBL_SENSOR_MODE
      "fields": { "sensor": "SENSOR", "mode": "SENSOR_MODE", "kind": "SENSOR_TYPE", "maxOffNadir": "MAX_OFF_NADIR", "minOffNadir": "MIN_OFF_NADIR",
                  "swath": "SWATH", "length": "SCENE_LENGTH", "lookSide": "LOOK_SIDE", "scenesPerPass": "SCENES_PER_PASS",
                  "minInterval": "MIN_INTERVAL_MIN", "maxPerDay": "MAX_PER_DAY", "shotsPerPass": "SHOTS_PER_PASS", "leadTime": "LEAD_TIME_H",
                  "resolution": "RESOLUTION_M", "primary": "IS_DEFAULT" } }
  ],
  "satelliteAliases": { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/3",       // TBL_SATELLITE_ALIAS
                        "fields": { "satellite": "SAT_NAME", "alias": "ALIAS" } },
  "requestStores": [
    { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/4",                         // TBL_IMAGING_REQUEST
      "history": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/5",                     // TBL_REQUEST_HISTORY
      "groups": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/21",                     // TBL_REQUEST_GROUP
      "shots": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/22",                      // TBL_REQUEST_SHOT
      "quotas": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/23",                     // TBL_IMAGING_QUOTA
      "areaChanges": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/34" }               // TBL_REQUEST_AREA_CHANGE
  ],
  "observationStores": [
    { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/6",                         // TBL_OBSERVATION
      "areas": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/7",                       // TBL_OBSERVATION_AREA
      "taxonomyTable": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/8" }              // TBL_TAXONOMY
  ],
  "alertReports": {
    "rules": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/9",                         // TBL_ALERT_RULE
    "geofences": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/10",                    // TBL_ALERT_GEOFENCE
    "namedAreas": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/11",                   // TBL_NAMED_AREA
    "table": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/12",                        // TBL_ALERT_LOG
    "countries": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/20",                    // TBL_COUNTRY
    "formats": [{ "name": "既定" }]
  },
  "assignments": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/13",                    // TBL_ASSIGNMENT
  "imageReviews": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/35",                   // TBL_IMAGE_REVIEW
  "handover": { "table": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/14" },          // TBL_HANDOVER_MEMO
  "events": {
    "layer": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/15",                        // TBL_EVENT
    "links": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/16"                         // TBL_EVENT_LINK
  },
  "referenceInfo": {
    "layer": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/36",                        // TBL_REFERENCE_INFO
    "links": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/37"                         // TBL_REFERENCE_LINK
  },
  "interests": {
    "points": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/24",                       // TBL_INTEREST_POINT
    "areas": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/25"                         // TBL_INTEREST_AREA
  },
  "userRoles": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/26",                     // TBL_USER_ROLE
  "satelliteOutages": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/27",              // TBL_SATELLITE_OUTAGE
  "glossary": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/28",                      // TBL_GLOSSARY
  "training": {
    "exercises": {
      "exercises": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/29",                  // TBL_TRAINING_EXERCISE
      "answers": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/30",                    // TBL_TRAINING_ANSWER
      "results": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/31"                     // TBL_TRAINING_RESULT
    }
  },
  "imageCorrections": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/32",              // TBL_IMAGE_CORRECTION
  "namedAreas": {
    "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/11",                          // TBL_NAMED_AREA（組織の関心地域）
    "categories": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/33"                    // TBL_AREA_CATEGORY
  },
  "aiResultSources": [
    { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/17",                        // TBL_AI_RESULT
      "fields": { "id": "RESULT_ID", "imageId": "IMAGE_ID", "class": "CLASS", "score": "SCORE", "model": "MODEL", "detectedAt": "DETECTED_AT",
                  "name": "OBJECT_NAME", "kind": "KIND", "activity": "ACTIVITY", "length": "LENGTH_M", "heading": "HEADING", "width": "WIDTH_M",
                  "note": "NOTE" } }
  ],
  "detectionOutputs": [
    { "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/18",                        // TBL_DETECTION
      "fields": { "imageId": "IMAGE_ID", "fileName": "FILE_NAME", "imageTime": "IMAGE_TIME", "detectedAt": "DETECTED_AT", "sentAt": "SENT_AT",
                  "class": "CLASS", "classId": "CLASS_ID", "score": "SCORE", "model": "MODEL", "status": "STATUS", "lon": "LON", "lat": "LAT",
                  "area": "AREA_M2", "length": "LENGTH_M", "width": "WIDTH_M", "angle": "HEADING", "note": "NOTE" } }
  ],
  "geocoders": [
    { "type": "features", "label": "地名", "url": "https://gis.example/arcgis/rest/services/MIKAGE/FeatureServer/19", "field": ["PLACE_NAME", "PLACE_NAME_KANA", "PLACE_ALIAS"] }  // TBL_PLACE_NAME
  ]
  // editTracking は既定（CREATED_DATE・CREATED_USER・UPDATED_DATE・UPDATED_USER）のままなら書かなくてよい
}
```
