# ArcGIS ダッシュボード（撮像計画・観測結果）の構成とビュー

ArcGIS Enterprise の Dashboards で、MIKAGE が書く Esri のテーブルを見るためのお試しの構成案（撮像計画系 4 つ・観測結果系 3 つ）と、そのためのビュー 9 本（`tools/arcgis/dashboard_views_oracle.sql`、Oracle・ST_Geometry）。ダッシュボードだけでは数えられないもの（依頼単位の件数・モノごとの最新・年間上限の残り・定点の未撮像日数など）をビューで補う。2026-11-18 に tsudo の依頼（「何種類かお試しで ArcGIS ダッシュボードを作りたい。撮像計画系と観測結果系」）で作った。

| 系 | ダッシュボード | 主な読者 | 答える問い | 読むもの |
| --- | --- | --- | --- | --- |
| 撮像計画 | [P1 撮像予定（今日・明日）](#p1-撮像予定今日明日) | 依頼者 | 自分の依頼はその日の計画に入ったか | `VW_PLAN_REQUEST_DAY`・`VW_PLAN_HISTORY`・`VW_PLAN_SHOT` |
| 撮像計画 | [P2 採否の振り返り](#p2-採否の振り返り) | 計画担当・管理者 | どのシステム・グループで不採用や見送りが多いか、その理由は | `VW_PLAN_REQUEST_DAY`・`VW_PLAN_HISTORY` |
| 撮像計画 | [P3 年間撮像上限](#p3-年間撮像上限) | 計画担当・管理者 | センサー・モードごとに枚数はあと何枚か、このペースで年度末まで足りるか | `VW_PLAN_QUOTA_USAGE`・`VW_PLAN_QUOTA_MONTH`・`VW_PLAN_SHOT` |
| 撮像計画 | [P4 定点の撮れ具合](#p4-定点の撮れ具合) | 計画担当 | 何日も撮れていない定点はどこか、衛星の障害は | `VW_PLAN_FIXED_POINT`・`TBL_SATELLITE_OUTAGE` |
| 観測結果 | [O1 モノの台帳](#o1-モノの台帳) | 観測担当・判読者 | すべてのモノについて、最後にいつどこで見たか | `VW_OBS_THING`・`VW_OBS_RECORD` |
| 観測結果 | [O2 場所ごとの動き](#o2-場所ごとの動き) | 観測担当・管理者 | どの地名で何が増えているか | `VW_OBS_RECORD` |
| 観測結果 | [O3 判読の進み具合](#o3-判読の進み具合) | 班長・管理者 | 画像の分担はどこまで終わったか、だれが何件記録したか | `TBL_ASSIGNMENT`・`VW_OBS_RECORD` |

役割の分け方: プラグインは「いま手を動かす作業」（観測記録ドックの一覧は開いている画像の記録だけ、撮像予定ドックは 1 日ずつ）、ダッシュボードは「期間をまたぐ振り返り」と「QGIS を使わない人（依頼者・管理者）が見るもの」。

前提:

- 日時は UTC で保存されている。`TARGET_DATE` は対象日の日本時間 0 時。ビューが作る日付（`..._DATE`）も同じく「日本時間 0 時を UTC で」持つので、ダッシュボード（ブラウザーの日本時間）でそのまま日本の日になる
- 観測記録・確認範囲は論理削除（`DELETED_AT` が入った行は削除済み）なので、ビューで除く
- 依頼者向けのものは出力履歴を読み、台帳（`TBL_IMAGING_REQUEST`）は読まない。台帳を読むのは計画担当向けの P4 だけで、属性は足さない
- ビューの名前は `VW_` で始め、ほかは `TBL_` の命名の決まり（大文字・`_` 区切り・30 文字以内）に合わせる。書き込めない表だと名前で分かるようにするため
- P1 は [arcgis-setup.md](arcgis-setup.md) の「3. 撮像予定（採否）のダッシュボード」の改良

## 撮像計画系

4 つとも、出力履歴・撮像の範囲・年間上限のテーブルから作ったビューを読む。状態の言葉（採用・不明・不採用・見送り・要求外）と色は、プラグインの「撮像予定」ドックと同じ（採用 = 緑、不明 = 青、不採用 = 赤、見送り = 灰、要求外 = 紫）。

### P1 撮像予定（今日・明日）

依頼した人が、自分のグループの依頼がその日の計画に入ったかを 1 画面で見る。手順書の案との違いは、件数を「依頼の件数」で数えること（1 日に何回も撮る依頼や衛星で分担した依頼を 1 件に）と、撮像済みを採用に含めること。

| 要素 | データ | 設定 |
| --- | --- | --- |
| 日付セレクター「対象日」（ヘッダー） | — | 既定「今日」、「明日」も選べる。下の全要素を `TARGET_DATE` で絞る |
| カテゴリ セレクター「依頼グループ」「依頼者」 | `VW_PLAN_REQUEST_DAY` の `GROUP_LABEL`・`REQUESTER` | 既定はすべて。URL パラメーターで既定のグループを渡せるようにし、グループごとのブックマークを配る |
| インジケーター「計画に入った」「不明」「不採用」「見送り」「要求外」 | `VW_PLAN_REQUEST_DAY` | `PLAN_STATE` ごとの件数。「計画に入った」は参照値を全件にして `{percentage}` も出す |
| マップ | Web マップ（`VW_PLAN_HISTORY` を `PLAN_STATE` の個別値で塗り、`VW_PLAN_SHOT` を塗りなしの破線） | 依頼の範囲と、まとめて撮る範囲を重ねる |
| テーブル「その日の計画」 | `VW_PLAN_HISTORY` | 列 `PLAN_STATE`・`REQ_NAME`・`MANAGEMENT_NO`・`SYSTEM`・`SATELLITE`・`PASS_TIME`・`ADOPTION_REASON`。並べ替え `PASS_TIME`。行を選ぶと地図がその範囲へ |
| 系列グラフ「日ごとの採否」 | `VW_PLAN_REQUEST_DAY` | カテゴリ `TARGET_DATE`（日）、分割 `PLAN_STATE`、積み上げ。日付セレクターは効かせず、固定の絞り込み `DAY_OFFSET` が -30〜1 |

- 壁に映す用途なら日付セレクターをやめ、各要素の固定の絞り込みを `DAY_OFFSET` = 0（今日）・1（明日）にすると、日が変わってもそのまま使える

### P2 採否の振り返り

計画担当が月に 1 回、どのシステム・グループで不採用や見送りが多いかと、その理由を見る。

| 要素 | データ | 設定 |
| --- | --- | --- |
| 日付セレクター「期間」 | — | 範囲、既定は過去 30 日。`TARGET_DATE` で絞る |
| カテゴリ セレクター「システム」「依頼グループ」 | `VW_PLAN_HISTORY` の `SYSTEM`・`GROUP_LABEL` | 複数選択 |
| インジケーター「計画に入った割合」「不採用」「見送り」「撮れた割合」 | `VW_PLAN_REQUEST_DAY`（要求外を除く） | 割合は値 = 該当の件数、参照値 = 全件で `{percentage}` |
| 系列グラフ「システム別の採否」 | `VW_PLAN_HISTORY` | カテゴリ `SYSTEM`、分割 `PLAN_STATE`、100% 積み上げ |
| 系列グラフ「週ごとの推移」 | `VW_PLAN_REQUEST_DAY` | カテゴリ `TARGET_DATE`（週）、分割 `PLAN_STATE` |
| 棒グラフ「不採用の理由」「見送りの理由」 | `VW_PLAN_HISTORY` | カテゴリ `ADOPTION_REASON`（`PLAN_STATE` = 不採用）・`REASON`（見送り）、多い順に 10 件 |
| 棒グラフ「不採用が続く依頼」 | `VW_PLAN_REQUEST_DAY` | カテゴリ `REQ_NAME`、`PLAN_STATE` = 不採用の件数、多い順に 10 件 |

- 理由は文字のまま数えるので、結果ファイルが理由を決まった言葉で返すシステムほど役に立つ。自由文が多いときは、この 2 つをテーブルに変える

### P3 年間撮像上限

センサー・モードごとに今年度の残り枚数と、このペースで年度末まで足りるかを見る。数え方はプラグインの「年間撮像上限」の画面と同じ（消費 = 当初の消費 + 撮像済み − 数えない枚数、残り = 上限 − 消費 − 見込み）。

| 要素 | データ | 設定 |
| --- | --- | --- |
| カテゴリ セレクター「センサー・モード」 | `VW_PLAN_QUOTA_USAGE` の `QUOTA_LABEL` | 1 つ選ぶ。右のゲージ・累計グラフ・撮像の一覧を絞る（撮像は `SENSOR`・`SENSOR_MODE` で） |
| 系列グラフ「上限ごとの消化」 | `VW_PLAN_QUOTA_USAGE` | フィーチャごと、横棒で `CONSUMED`・`PENDING_SCENES`・`REMAINING` を積み上げ。`QUOTA_STATE`（上限超過・残りわずか・ペース超過・順調）のラベルを横に |
| ゲージ「使った割合」 | `VW_PLAN_QUOTA_USAGE` | 値 `USED_PCT`、目安の線を `YEAR_PCT`（年の過ぎた割合） |
| 詳細「このままだと」 | `VW_PLAN_QUOTA_USAGE` | `YEAR_TEXT`・`REMAINING`・`PACE`・`RUNS_OUT_DATE`（上限に届く日の見込み） |
| 系列グラフ「月ごとの消費と累計」 | `VW_PLAN_QUOTA_MONTH` | フィーチャごと、カテゴリ `MONTH_TEXT`。棒 `USED_SCENES`、線 `CUM_CONSUMED`、破線 `PACE_LINE`（均等に使ったとき） |
| テーブル「数えた撮像」 | `VW_PLAN_SHOT` | `QUOTA_STATE` が撮像済み・見込み。列 `TARGET_DAY`・`SATELLITE`・`SENSOR_LABEL`・`SCENES`・`UNCOUNTED_SCENES`・`UNCOUNTED_REASON`・`REQ_NAMES` |

- 上限の行がないセンサーは出ない（プラグインと同じ「行がなければ上限なし」）。上限を空にした行は `QUOTA_STATE` が「上限なし」で、消費だけ数える

### P4 定点の撮れ具合

計画担当が、何日も撮れていない定点と、その原因になりそうな衛星の障害を並べて見る。「遅れ」は繰り返しの間隔（毎日・間隔:3・曜日:月水金）で割った回数なので、毎日の依頼と週 1 回の依頼を同じ物差しで比べられる。

| 要素 | データ | 設定 |
| --- | --- | --- |
| カテゴリ セレクター「依頼グループ」 | `VW_PLAN_FIXED_POINT` の `GROUP_LABEL` | 複数選択 |
| インジケーター「3 回分以上あき」「まだ撮れていない」「有効な定点」 | `VW_PLAN_FIXED_POINT` | `GAP_CLASS` ごとの件数 |
| マップ | Web マップ（`VW_PLAN_FIXED_POINT` を `GAP_CLASS` で色分け） | ポップアップに最後に撮れた日・直近 30 日の内訳 |
| リスト「あいている順」 | `VW_PLAN_FIXED_POINT` | 並べ替え `LATE_TURNS` の多い順。行に `REQ_NAME`、最後に撮れた日、「30 日で 出した n 日・計画に入った n 日・撮れた n 日」。選ぶと地図がその範囲へ |
| 棒グラフ「30 日の内訳」 | `VW_PLAN_FIXED_POINT` | フィーチャごと、カテゴリ `REQ_NAME`、系列 `IN_PLAN_30`・`DECLINED_30`・`HELD_30`（どこで止まっているか） |
| リスト「衛星の障害（復旧前）」 | `TBL_SATELLITE_OUTAGE` | 絞り込み `RECOVERED_AT` が空。`SAT_NAME`・`OUTAGE_REASON`・`EXPECTED_END` |

- 台帳を読むのはこのダッシュボードだけ（有効な定点の一覧と範囲のため）。依頼者には公開しない想定

## 観測結果系

3 つとも、削除していない観測記録だけを読む。分類のパス（`船舶 > 貨物船 > コンテナ船`）はビューで大分類・2 段目・いちばん下に分けてあるので、ダッシュボードのカテゴリとしてそのまま使える。「見た日」は画像の撮像日時（なければ記録した日時）で、プラグインの「流れ」と同じ（`core/obs/flows.py` の `time_key`）。

### O1 モノの台帳

観測記録ドックにモノの一覧はない（2026-11-18 から一覧は「この画像」だけで、モノの流れは行の「流れ」から 1 つずつ見る）ので、モノ全体を見渡すのはこのダッシュボード。モノ 1 つを 1 行にして、最後にいつ・どこで見たか、何枚の画像で見たかを並べ、選んだモノの記録を地図と時系列でたどる。

| 要素 | データ | 設定 |
| --- | --- | --- |
| カテゴリ セレクター「大分類」「最後に見た時期」 | `VW_OBS_THING` の `ROOT_KIND`・`AGE_CLASS` | 時期は 1 か月以内・1〜3 か月前・3〜12 か月前・1 年以上前 |
| カテゴリ セレクター「モノの名前」 | `VW_OBS_THING` の `THING_NAME` | ドロップダウンにして検索を有効に（船名・機番を打って探す） |
| インジケーター「モノの数」「うち 1 か月より前」 | `VW_OBS_THING` | 2 つ目は `IN_LAST_MONTH` = 0（1 か月以上見ていないもの） |
| リスト「モノ」 | `VW_OBS_THING` | 並べ替え `LAST_SEEN_AT` の新しい順。行に `THING_NAME`・`LEAF_KIND`・最後に見た日と場所・`SEEN_COUNT` 回。選ぶと下の記録を `OBJECT_ID` で絞り、地図をその位置へ |
| マップ | Web マップ（`VW_OBS_THING` を `AGE_CLASS` で色分け、`VW_OBS_RECORD` を小さな点で） | モノを選ぶと、その記録の点だけが残り、動いた跡が見える |
| 系列グラフ「見た日」 | `VW_OBS_RECORD` | カテゴリ `SEEN_DATE`（週）、分割 `ACTIVITY`。モノの選択で絞られる |
| テーブル「記録」 | `VW_OBS_RECORD` | 列 `SEEN_AT`・`IMAGE_NAME`・`ACTIVITY`・`PRESENT_LABEL`・`PLACE_LABEL`・`NOTE`・`OBSERVER` |

- モノの名前・分類は最後に書かれたもの。プラグインは「同じ系統でより細かい分類」があればそちらを使うので、分類が「船舶」だけの画像が最後だと、ここでは粗い分類になる

### O2 場所ごとの動き

どの地名で何が増えているかを、月ごとの推移と地図で見る。地名は観測記録の `NAMED_AREA_OBJECTID`（[属性ルール](arcgis-setup.md#属性ルール記録の地名の-objectid)で入れる）で引く。

| 要素 | データ | 設定 |
| --- | --- | --- |
| 日付セレクター「期間」 | — | 範囲、既定は過去 6 か月。`SEEN_DATE` で絞る |
| カテゴリ セレクター「大分類」「活動」「地名の種類」 | `VW_OBS_RECORD` の `ROOT_KIND`・`ACTIVITY`・`PLACE_KIND` | 複数選択 |
| 系列グラフ「月ごとの件数」 | `VW_OBS_RECORD` | カテゴリ `SEEN_MONTH`、分割 `ROOT_KIND`、積み上げ |
| 棒グラフ「地名ごと」 | `VW_OBS_RECORD` | カテゴリ `PLACE_LABEL`、件数の多い順に 15 件。棒を選ぶと地図とグラフをその地名で絞る |
| マップ | Web マップ（`VW_OBS_RECORD` をヒートマップ、拡大すると `ROOT_KIND` で色分けした点。`TBL_NAMED_AREA` を塗りなしで） | 表示範囲でほかの要素を絞る |
| テーブル「最新の記録」 | `VW_OBS_RECORD` | 並べ替え `SEEN_AT` の新しい順。列 `SEEN_AT`・`PLACE_LABEL`・`LEAF_KIND`・`OBJECT_NAME`・`ACTIVITY`・`NOTE` |

- 「平常との差」（前の 8 区間と比べて急に増えたもの）はダッシュボードでは計算できないので、プラグインの「活動の推移」に任せる。必要なら地名 × 週のビューを足す

### O3 判読の進み具合

班長が、画像の分担がどこまで終わったか、だれが何件記録したかを見る。分担（`TBL_ASSIGNMENT`）を使っているときだけ意味がある。

| 要素 | データ | 設定 |
| --- | --- | --- |
| 日付セレクター「担当にした日」 | — | 既定は過去 7 日。`TAKEN_DATE` で絞る |
| インジケーター「作業中」「保留」「今日完了」 | `VW_OBS_IMAGE_WORK` | `STATUS` ごとの件数。今日完了は `DONE_DATE` が今日 |
| 棒グラフ「担当者ごと」 | `VW_OBS_IMAGE_WORK` | カテゴリ `ASSIGNEE`、分割 `STATUS`、積み上げ |
| 系列グラフ「日ごとの記録」 | `VW_OBS_RECORD` | カテゴリ `SEEN_DATE` ではなく記録した日 `CREATED_AT`（日）、分割 `OBSERVER` |
| リスト「保留の画像」 | `VW_OBS_IMAGE_WORK` | `STATUS` = 保留。`IMAGE_NAME`・`ASSIGNEE`・`NOTE`・`DAYS_SINCE_TAKEN` 日 |
| リスト「完了だが記録 0 件」 | `VW_OBS_IMAGE_WORK` | `DONE_EMPTY` = 1（何もなかったのか、付け忘れかを確かめる） |

- 担当者の記録の件数（`MY_RECORDS`）は、記録の作成者 `CREATED_USER` か記録者 `OBSERVER` が担当者と同じ名前のもの（大文字・小文字は区別しない）。記録者に表示名を入れている人は、作成者のほうで合う

## ビュー（`tools/arcgis/dashboard_views_oracle.sql`）

上の 3 本はほかのビューの土台も兼ねるので、ファイルの順に作る（ファイルもその順）。

| ビュー | 1 行は | 形状 | 元のテーブル | 足した主な列 | 使うダッシュボード |
| --- | --- | --- | --- | --- | --- |
| `VW_PLAN_HISTORY` | 出力履歴の 1 行 | ポリゴン | `TBL_REQUEST_HISTORY`・`TBL_REQUEST_SHOT` | `PLAN_STATE`（撮像予定ドックと同じ状態）・`IN_PLAN`・`DAY_OFFSET`（0 = 今日）・`GROUP_LABEL`・`PASS_TIME` | P1・P2 |
| `VW_PLAN_REQUEST_DAY` | 依頼 × 対象日 | なし | `VW_PLAN_HISTORY` | その日のいちばん良い `PLAN_STATE`・`CAPTURED`・行数・`SYSTEMS` | P1・P2 |
| `VW_PLAN_SHOT` | 撮像（ファイルの 1 行） | ポリゴン | `TBL_REQUEST_SHOT`・`TBL_REQUEST_HISTORY` | `QUOTA_STATE`（撮像済み・見込み・未撮像・不採用）・`SENSOR_LABEL` | P1・P3 |
| `VW_PLAN_QUOTA_USAGE` | 年間上限の行（今年度） | なし | `TBL_IMAGING_QUOTA`・`VW_PLAN_SHOT` | `CONSUMED`・`PENDING_SCENES`・`REMAINING`・`USED_PCT`・`YEAR_PCT`・`PACE`・`RUNS_OUT_DATE`・`QUOTA_STATE` | P3 |
| `VW_PLAN_QUOTA_MONTH` | 年間上限 × 今年度の月 | なし | `VW_PLAN_QUOTA_USAGE`・`VW_PLAN_SHOT` | `USED_SCENES`・`CUM_CONSUMED`（累計）・`PACE_LINE` | P3 |
| `VW_PLAN_FIXED_POINT` | 有効な定点の依頼 | ポリゴン | `TBL_IMAGING_REQUEST`・`VW_PLAN_HISTORY` | `LAST_CAPTURED_DATE`・`EVERY_DAYS`・`LATE_TURNS`・`GAP_CLASS`・直近 30 日の日数 | P4 |
| `VW_OBS_RECORD` | 削除していない観測記録 | ポイント | `TBL_OBSERVATION`・`TBL_NAMED_AREA` | `ROOT_KIND`・`KIND_2`・`LEAF_KIND`・`SEEN_AT`・`SEEN_DATE`・`SEEN_MONTH`・`PLACE_LABEL` | O1・O2・O3 |
| `VW_OBS_THING` | モノ（`OBJECT_ID`） | ポイント（最後の記録） | `VW_OBS_RECORD` | `THING_NAME`・`LAST_SEEN_AT`・`FIRST_SEEN_AT`・`SEEN_COUNT`・`IMAGE_COUNT`・`IN_LAST_MONTH`・`AGE_CLASS` | O1 |
| `VW_OBS_IMAGE_WORK` | 分担の 1 行 | なし | `TBL_ASSIGNMENT`・`VW_OBS_RECORD`・`TBL_OBSERVATION_AREA` | `MY_RECORDS`・`IMAGE_RECORDS`・`IMAGE_AREAS`・`HOURS`・`DONE_EMPTY` | O3 |

作りの決まり:

- 形状のあるビューは、形状と同じテーブルの `OBJECTID` をそのまま使う（ArcGIS の決まり）。形状のないビューも元の行の `OBJECTID` から作るので、重ならず、データが増えても変わらない
- 状態の決め方はプラグインのコードに合わせる（撮像予定は `core/request/store.py` の `state_of`、年間上限は `core/request/quota.py` の `shot_state`・`usage_of`、見た日時は `core/obs/flows.py` の `time_key`）。プラグインの決め方を変えたら、ビューも合わせる
- 文字の列は Oracle のジオデータベースの既定で NVARCHAR2 なので、文字の定数と混ぜる CASE・LISTAGG では `TO_CHAR` で揃える（ORA-12704 を避けるため。データベースの文字コードが AL32UTF8 の前提）
- テスト（`mikage/tests/test_dashboard_views.py`）: ビューの順・名前・`OBJECTID` と形状の決まりを確かめ、sqlglot があれば 9 本を Oracle の構文で解析して、読む列がすべて `tools/arcgis/mikage_schema.py`（README のテーブル定義）か先に作ったビューにあることを照合する。プラグインの属性が変わったときの食い違いはここで分かる。本物の Oracle での実行はまだ（2026-11-18）

## 作り方と試す順番

試す順番は P1 → O1 → P3 を勧める。P1 は手順書の案の改良で導入がいちばん軽く、O1 は観測記録ドックからなくなったモノの一覧の代わりにすぐなり、P3 はプラグインの外では見られない数字（年間の残り）を管理者に届けられる。

1. 開発用のデータベースで、データ所有者（例 `MIKAGE`）として `dashboard_views_oracle.sql` を流す（SQL*Plus か SQL Developer。何度流してもよい）
2. 各ビューを `SELECT * FROM VW_PLAN_QUOTA_USAGE` などで開き、プラグインの「撮像予定」ドック・「年間撮像上限」の画面・観測記録の行の「流れ」と数が合うか見る
3. 閲覧用のロールに `GRANT SELECT ON VW_… TO MIKAGE_VIEWER` を付ける（ファイルの先頭のコメント）
4. ArcGIS Pro で所有者として接続し、各ビューを「ジオデータベースに登録」する（ObjectID は `OBJECTID`。形状のあるビューは元のテーブルと同じ形状の種類・座標系）
5. ビューだけを入れた地図を別のサービス（例 `MIKAGE_DASHBOARD`、参照で公開、フィーチャ アクセスは Query だけ）として公開する。編集用の `MIKAGE` サービスには入れない（`publish_service.py` がレイヤー番号を一覧の順に固定しているため）
6. Map Viewer で Web マップを作り（色は上の各案のとおり）、Dashboards で要素を置く。レイヤーの更新間隔は 5〜10 分

注意:

- 前提: Oracle 19c 以降（`LISTAGG(DISTINCT …)`・`TO_DATE(… DEFAULT NULL ON CONVERSION ERROR)` を使う）、データベースの文字コードは AL32UTF8、サービスの日付のタイムゾーンは既定の UTC
- ビューは問い合わせのたびに計算する。使う結合キー（`REQ_ID`・`SHOT_ID`・`TARGET_DAY`・`OBJECT_ID`・`IMAGE_ID`・`NAMED_AREA_OBJECTID`）には `create_tables.py` が属性インデックスを張ってある。記録が数十万件を超えて `VW_OBS_THING` が重くなったら、マテリアライズド ビュー（夜に更新）に切り替える
- 地名の属性ルール（`NAMED_AREA_OBJECTID`）をまだ入れていないときは、`VW_OBS_RECORD` の地名の結合を外して流す（SQL のコメントに手順）
- 元のテーブルを形状なしで作った場合（出力履歴など）は、そのビューの `SHAPE` の行を消す
