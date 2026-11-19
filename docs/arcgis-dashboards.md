# ArcGIS ダッシュボード（撮像計画・観測結果・地名）の構成とビュー

ArcGIS Enterprise の Dashboards で、MIKAGE が書く Esri のテーブルを見るためのお試しの構成案（撮像計画系 4 つ・観測結果系 3 つ・地名系 3 つ）と、そのためのビュー 13 本（`tools/arcgis/dashboard_views_oracle.sql`、Oracle・ST_Geometry）。ダッシュボードだけでは数えられないもの（依頼単位の件数・モノごとの最新・年間上限の残り・定点の未撮像日数・地名ごとの平常との差など）をビューで補う。2026-11-18 に tsudo の依頼（「何種類かお試しで ArcGIS ダッシュボードを作りたい。撮像計画系と観測結果系」）で作り、同じ日に観測記録の `NAMED_AREA_OBJECTID` を軸にした地名系を足した（「せっかく NAMED_AREA_OBJECTID があるので、これを活用したダッシュボードも欲しい」）。

| 系 | ダッシュボード | 主な読者 | 答える問い | 読むもの |
| --- | --- | --- | --- | --- |
| 撮像計画 | [P1 撮像予定（今日・明日）](#p1-撮像予定今日明日) | 依頼者 | 自分の依頼はその日の計画に入ったか | `VW_PLAN_REQUEST_DAY`・`VW_PLAN_HISTORY`・`VW_PLAN_SHOT` |
| 撮像計画 | [P2 採否の振り返り](#p2-採否の振り返り) | 計画担当・管理者 | どのシステム・グループで不採用や見送りが多いか、その理由は | `VW_PLAN_REQUEST_DAY`・`VW_PLAN_HISTORY` |
| 撮像計画 | [P3 年間撮像上限](#p3-年間撮像上限) | 計画担当・管理者 | センサー・モードごとに枚数はあと何枚か、このペースで年度末まで足りるか | `VW_PLAN_QUOTA_USAGE`・`VW_PLAN_QUOTA_MONTH`・`VW_PLAN_SHOT` |
| 撮像計画 | [P4 定点の撮れ具合](#p4-定点の撮れ具合) | 計画担当 | 何日も撮れていない定点はどこか、衛星の障害は | `VW_PLAN_FIXED_POINT`・`TBL_SATELLITE_OUTAGE` |
| 観測結果 | [O1 モノの台帳](#o1-モノの台帳) | 観測担当・判読者 | すべてのモノについて、最後にいつどこで見たか | `VW_OBS_THING`・`VW_OBS_RECORD` |
| 観測結果 | [O2 場所ごとの動き](#o2-場所ごとの動き) | 観測担当・管理者 | どの地名で何が増えているか | `VW_OBS_RECORD` |
| 観測結果 | [O3 判読の進み具合](#o3-判読の進み具合) | 班長・管理者 | 画像の分担はどこまで終わったか、だれが何件記録したか | `TBL_ASSIGNMENT`・`VW_OBS_RECORD` |
| 地名 | [N1 いつもと違う地名](#n1-いつもと違う地名) | 観測担当・管理者 | どの地名で、いつもより多く（少なく）記録しているか | `VW_NAMED_AREA_STATUS`・`VW_NAMED_AREA_WEEK`・`VW_OBS_RECORD` |
| 地名 | [N2 地名カルテ](#n2-地名カルテ) | 観測担当・依頼者・ほかの部署 | この地名でいま何が起きているか（管理番号のリンクで開く） | `VW_NAMED_AREA_STATUS`・`VW_NAMED_AREA_WEEK`・`VW_NAMED_AREA_THING`・`VW_OBS_RECORD`・`VW_ALERT_LOG` |
| 地名 | [N3 地名の付け方の点検](#n3-地名の付け方の点検) | 管理者（属性ルールを入れた人） | 記録に地名が正しく付いているか、入れ直しや地名の追加が要るか | `VW_OBS_RECORD`・`VW_NAMED_AREA_STATUS` |

役割の分け方: プラグインは「いま手を動かす作業」（観測記録ドックの一覧は開いている画像の記録だけ、撮像予定ドックは 1 日ずつ）、ダッシュボードは「期間をまたぐ振り返り」と「QGIS を使わない人（依頼者・管理者）が見るもの」。

前提:

- 日時は UTC で保存されている。`TARGET_DATE` は対象日の日本時間 0 時。ビューが作る日付（`..._DATE`）も同じく「日本時間 0 時を UTC で」持つので、ダッシュボード（ブラウザーの日本時間）でそのまま日本の日になる
- 観測記録・確認範囲は論理削除（`DELETED_AT` が入った行は削除済み）なので、ビューで除く
- 雲などで見えなかった記録（「不可」、2026-11-19）は `UNSEEN_REASON` に理由が入り `PRESENT` は空。ビューでは `PRESENT_LABEL` が「不可」、`LOOKED` が 0。モノの「最後に見た」・地名の件数には数えない。不可範囲（`TBL_OBSERVATION_AREA` の `UNSEEN_REASON` あり）は判読の進み具合で確認範囲と分けて数える。ビューは両方のテーブルに `UNSEEN_REASON` を足してから作り直す（`create_tables.py` をもう一度実行すれば足りない属性だけ足される）
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

- 地名ごとの「平常との差」（前の 8 区間と比べて急に増えたもの）は [N1](#n1-いつもと違う地名) で見る。地名で絞らない全体の推移は、プラグインの「活動の推移」に任せる

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

## 地名系

3 つとも、観測記録の `NAMED_AREA_OBJECTID`（[属性ルール](arcgis-setup.md#属性ルール記録の地名の-objectid)が入れる、記録の点を含むいちばん小さい有効な地名の `OBJECTID`）で地名（`TBL_NAMED_AREA`）を引く。空間の結合をしないので、ビューは軽く、地名の数が多くても速い。地名側の組織内の管理番号 `NAMED_AREA_ID` は、ダッシュボードのリンクと表示に使う。

- 1 件の記録は 1 つの地名（含むうちいちばん小さいもの）にだけ数える。湾の中の港の記録は港に数え、湾には足さない。大きさの違う地名を比べないよう、「地名の種類」で同じ種類どうしを並べる
- 件数は「消失」以外の記録。判読した画像の量にも左右される（画像の多い週は増える）ので、急に増えた地名は、モノの数（`THINGS`）や記録の中身と合わせて見る
- 属性ルールを入れる前の記録で `NAMED_AREA_OBJECTID` が空のものは、どの地名にも数えない。前からある記録には、ArcGIS Pro のフィールド演算で同じ式を一度かけておく（[N3](#n3-地名の付け方の点検) で残りを確かめられる）

### N1 いつもと違う地名

どの地名で、いつもより多く（少なく）記録しているかを地図と一覧で見る。区間は今日で終わる 7 日ずつ（暦の週にしないのは、月曜の朝に「今週」が少なく見えないようにするため）。平常との差の決め方はプラグインの「活動の推移」（`core/dashboard.py` の `trends`）と同じで、直近 7 日を前の 8 区間の平均・ばらつきと比べ、2 ばらつき以上かつ 3 件以上多ければ「急に増えた」。「急に減った」（2 ばらつき以上少なく、平常が 3 件以上）はダッシュボード用に足した（プラグインにはない）。

| 要素 | データ | 設定 |
| --- | --- | --- |
| カテゴリ セレクター「地名の種類」「動き」 | `VW_NAMED_AREA_STATUS` の `PLACE_KIND`・`TREND_CLASS` | 複数選択。固定の絞り込み `ACTIVE_LABEL` = 有効 |
| インジケーター「急に増えた」「急に減った」「7 日に記録あり」「30 日に通報あり」 | `VW_NAMED_AREA_STATUS` | 地名の数。`TREND_CLASS` = 急に増えた・急に減った、`LAST_7D` > 0、`ALERTS_30D` > 0 |
| マップ | Web マップ（`VW_NAMED_AREA_STATUS` を `TREND_CLASS` で塗る: 急に増えた = 赤、急に減った = 青、平常 = 灰、記録なし = 枠だけ） | ラベル `PLACE_NAME`。表示範囲で一覧を絞る |
| テーブル「地名」 | `VW_NAMED_AREA_STATUS` | 並べ替え `TREND_SCORE` の高い順。列 `PLACE_LABEL`（地名（管理番号））・`PLACE_KIND`・`LAST_7D`・`NORMAL_7D`・`CHANGE_7D`・`THINGS_7D`・`ALERTS_30D`。行を選ぶと下の 2 つを `NAMED_AREA_OBJECTID`（地名側は `OBJECTID`）で絞り、地図をその地名へ |
| 系列グラフ「7 日ごとの件数と平常」 | `VW_NAMED_AREA_WEEK` | 統計（地名を選ぶまでは全地名の合計）、カテゴリ `WEEK_START_DATE`、棒 `USUAL_RECORDS` と `SURGE_RECORDS`（赤。急に増えた地名・区間の分）の合計を積み上げ、線 `NORMAL` の合計。地名を 1 つ選ぶと、その地名の件数と平常になる |
| テーブル「直近 7 日の記録」 | `VW_OBS_RECORD` | 固定の絞り込み `DAYS_AGO` が 0〜6・`PRESENT_LABEL` が消失以外。列 `SEEN_AT`・`LEAF_KIND`・`OBJECT_NAME`・`ACTIVITY`・`NOTE` |

- 「急に増えた」の目安（`UPPER_LINE` = 平常 + 2 ばらつき）は地名ごとの値なので、合計のグラフには出さず、N2 で出す

### N2 地名カルテ

地名 1 つの様子を 1 画面にまとめる。組織内の管理番号 `NAMED_AREA_ID` を URL パラメーターにして、ほかの台帳・メール・通報文から `…/dashboards/<id>#area=NA-0101` で開けるようにする。`OBJECTID` は地名のデータを入れ直すと変わるが、管理番号は変わらないのでリンクが切れない。

| 要素 | データ | 設定 |
| --- | --- | --- |
| URL パラメーター `area`（カテゴリ） | 5 つのデータの `NAMED_AREA_ID` | ダッシュボードの設定で、`VW_NAMED_AREA_STATUS`・`VW_NAMED_AREA_WEEK`・`VW_NAMED_AREA_THING`・`VW_OBS_RECORD`・`VW_ALERT_LOG` の `NAMED_AREA_ID` を絞る |
| カテゴリ セレクター「地名」（ヘッダー） | `VW_NAMED_AREA_STATUS` の `PLACE_LABEL` | ドロップダウン・検索あり・1 つだけ選ぶ（URL なしで開いたとき用）。ほかの要素を `NAMED_AREA_ID` で絞る |
| 詳細「地名」 | `VW_NAMED_AREA_STATUS` | `PLACE_NAME`・`NAME_EN`・`PLACE_KIND`・`ACTIVE_LABEL`・`NAMED_AREA_ID`・`FIRST_SEEN_AT`〜`LAST_SEEN_AT`・`RECORDS_TOTAL` 件 |
| インジケーター「直近 7 日の記録」 | `VW_NAMED_AREA_STATUS` | 値 `LAST_7D`、参照値 `NORMAL_7D`（「平常 n より +n」）。`TREND_CLASS` = 急に増えた なら赤（条件付き書式） |
| インジケーター「いまいそうなモノ」「30 日の通報」 | `VW_NAMED_AREA_STATUS` | `THINGS_LAST_HERE`（最後に見たのがここで 1 か月以内のモノ）・`ALERTS_30D`（下に `LAST_ALERT_AT`） |
| マップ | Web マップ（`VW_NAMED_AREA_STATUS` を枠だけ、`VW_OBS_RECORD` を `ROOT_KIND` で色分けした小さな点、`VW_NAMED_AREA_THING` を `WHERE_NOW` で色分けした大きな点） | 地名を選ぶとその範囲へ |
| 系列グラフ「7 日ごとの件数と平常」 | `VW_NAMED_AREA_WEEK` | フィーチャごと、カテゴリ `WEEK_START_DATE`、棒 `USUAL_RECORDS` と `SURGE_RECORDS`（赤）を積み上げ（急に増えた区間だけ赤くなる）、線 `NORMAL`、破線 `UPPER_LINE`（超えると急に増えた） |
| 系列グラフ「何を見たか」 | `VW_OBS_RECORD` | 固定の絞り込み `DAYS_AGO` が 0〜90、カテゴリ `SEEN_DATE`（週）、分割 `ROOT_KIND`、積み上げ |
| リスト「この地名のモノ」 | `VW_NAMED_AREA_THING` | 並べ替え `LAST_SEEN_AT` の新しい順。行に `THING_NAME`・`LEAF_KIND`・ここで最後に見た日・`SEEN_DAYS` 日・`WHERE_NOW`（その後ほかで見たなら `THING_LAST_PLACE`）。選ぶと地図をそのモノへ |
| テーブル「最近の記録」 | `VW_OBS_RECORD` | 並べ替え `SEEN_AT` の新しい順。列 `SEEN_AT`・`LEAF_KIND`・`OBJECT_NAME`・`ACTIVITY`・`PRESENT_LABEL`・`NOTE`・`OBSERVER` |
| リスト「通報」 | `VW_ALERT_LOG` | 並べ替え `REPORTED_AT` の新しい順。行に `REPORT_NAME`・`OBJECT_NAME`・`LEAF_KIND`・`ACTIVITY`・`MANUAL_LABEL`（条件・手動）・`REPORTED_BY` |

- 通報のフォーマットに管理番号（`place.id`）を入れておけば、受け取った人がその番号でカルテを開ける
- `WHERE_NOW` は「ここ（1 か月以内）」「ここ（1 か月より前）」「消失を確認」「その後ほかで見た」。いまいそうなモノは「最後に見たのがここ」なので、見ていない間に出て行ったかは分からない
- 依頼者・ほかの部署に配るなら、管理番号ごとのリンクを地名の一覧（Excel など）に入れておく

### N3 地名の付け方の点検

属性ルールを入れた人が、記録に地名が正しく付いているか、入れ直しや地名の追加が要るかを見る。`VW_OBS_RECORD` の `PLACE_STATE` で分ける。

| 要素 | データ | 設定 |
| --- | --- | --- |
| インジケーター「地名あり」「地名なし」「地名が見つからない」「無効な地名」 | `VW_OBS_RECORD` | `PLACE_STATE` ごとの件数。「地名が見つからない」が 1 件でもあれば赤 |
| 系列グラフ「月ごとの地名の付き方」 | `VW_OBS_RECORD` | カテゴリ `CREATED_AT`（月）、分割 `PLACE_STATE`、100% 積み上げ |
| マップ | Web マップ（`VW_OBS_RECORD` の `PLACE_STATE` が地名あり以外を点で、`VW_NAMED_AREA_STATUS` を枠だけ） | 地名の枠の中に「地名なし」「地名が見つからない」の点があれば、フィールド演算で入れ直す。枠の外に点が固まっていれば、地名を足す候補 |
| テーブル「点検の対象の記録」 | `VW_OBS_RECORD` | `PLACE_STATE` が地名あり以外。列 `PLACE_STATE`・`NAMED_AREA_OBJECTID`・`SEEN_AT`・`OBJECT_NAME`・`CREATED_USER` |
| テーブル「記録のない有効な地名」 | `VW_NAMED_AREA_STATUS` | `RECORDS_TOTAL` = 0・`ACTIVE_LABEL` = 有効。列 `PLACE_LABEL`・`PLACE_KIND`・`NAME_EN`（範囲がずれていないか、小さすぎないかの確認） |

- `PLACE_STATE`: 地名あり・地名なし（値が空: どの地名にも入らないか、ルールを入れる前の記録）・地名が見つからない（値の `OBJECTID` の地名がない: 地名のデータを入れ直した）・無効な地名（記録の後で地名の `ACTIVE` を 0 にした）
- 地名のポリゴンを直した後、前の記録の値が今の形と合っているか（別の地名に入るべきか）は、空間の結合をしないと分からないので、ここでは見ない。ポリゴンを直したらフィールド演算をかけ直す（[arcgis-setup.md](arcgis-setup.md#属性ルール記録の地名の-objectid)）

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
| `VW_OBS_RECORD` | 削除していない観測記録 | ポイント | `TBL_OBSERVATION`・`TBL_NAMED_AREA` | `ROOT_KIND`・`KIND_2`・`LEAF_KIND`・`SEEN_AT`・`SEEN_DATE`・`SEEN_MONTH`・`DAYS_AGO`・`PLACE_LABEL`・`PLACE_STATE`・`UNSEEN_REASON`・`LOOKED` | O1・O2・O3・N1・N2・N3 |
| `VW_OBS_THING` | モノ（`OBJECT_ID`） | ポイント（最後の記録） | `VW_OBS_RECORD` | `THING_NAME`・`LAST_SEEN_AT`・`FIRST_SEEN_AT`・`SEEN_COUNT`・`IMAGE_COUNT`・`IN_LAST_MONTH`・`AGE_CLASS`・`LAST_NAMED_AREA_OBJECTID`・`LAST_UNSEEN_AT`・`LAST_UNSEEN_REASON`・`UNSEEN_COUNT` | O1 |
| `VW_OBS_IMAGE_WORK` | 分担の 1 行 | なし | `TBL_ASSIGNMENT`・`VW_OBS_RECORD`・`TBL_OBSERVATION_AREA` | `MY_RECORDS`・`IMAGE_RECORDS`・`IMAGE_AREAS`・`UNSEEN_AREAS`・`HOURS`・`DONE_EMPTY` | O3 |
| `VW_ALERT_LOG` | 通報記録の 1 行 | なし | `TBL_ALERT_LOG`・`VW_OBS_RECORD` | 通報した記録の地名・モノ・分類、`REPORTED_DATE`・`DAYS_AGO`・`MANUAL_LABEL` | N2 |
| `VW_NAMED_AREA_WEEK` | 地名 × 7 日の区間（直近 26） | なし | `TBL_NAMED_AREA`・`VW_OBS_RECORD` | `RECORDS`（`USUAL_RECORDS`・`SURGE_RECORDS` に分けたものも）・`THINGS`・`NORMAL`・`UPPER_LINE`・`CHANGE`・`SCORE`・`TREND_CLASS`・`WEEK_START_DATE` | N1・N2 |
| `VW_NAMED_AREA_THING` | 地名 × モノ | ポイント（その地名での最後の記録） | `VW_OBS_RECORD`・`VW_OBS_THING`・`TBL_NAMED_AREA` | ここで `FIRST_SEEN_AT`・`LAST_SEEN_AT`・`SEEN_COUNT`・`SEEN_DAYS`、`WHERE_NOW`・`THING_LAST_PLACE` | N2 |
| `VW_NAMED_AREA_STATUS` | 地名 | ポリゴン | `TBL_NAMED_AREA`・上の 3 本・`VW_OBS_THING` | `PLACE_LABEL`（地名（管理番号））・`LAST_7D`・`NORMAL_7D`・`CHANGE_7D`・`TREND_SCORE`・`TREND_CLASS`・`RECORDS_30D`・`THINGS_30D`・`TOP_KIND_30D`・`THINGS_LAST_HERE`・`ALERTS_30D` | N1・N2・N3 |

作りの決まり:

- 形状のあるビューは、形状と同じテーブルの `OBJECTID` をそのまま使う（ArcGIS の決まり）。形状のないビューも元の行の `OBJECTID` から作るので、重ならず、データが増えても変わらない
- 状態の決め方はプラグインのコードに合わせる（撮像予定は `core/request/store.py` の `state_of`、年間上限は `core/request/quota.py` の `shot_state`・`usage_of`、見た日時は `core/obs/flows.py` の `time_key`）。プラグインの決め方を変えたら、ビューも合わせる
- 文字の列は Oracle のジオデータベースの既定で NVARCHAR2 なので、文字の定数と混ぜる CASE・LISTAGG では `TO_CHAR` で揃える（ORA-12704 を避けるため。データベースの文字コードが AL32UTF8 の前提）
- テスト（`mikage/tests/test_dashboard_views.py`）: ビューの順・名前・`OBJECTID` と形状の決まりを確かめ、sqlglot があれば 13 本を Oracle の構文で解析して、読む列がすべて `tools/arcgis/mikage_schema.py`（README のテーブル定義）か先に作ったビューにあることを照合する。プラグインの属性が変わったときの食い違いはここで分かる。本物の Oracle での実行はまだ（2026-11-18）

## 作り方と試す順番

試す順番は P1 → O1 → P3 を勧める。P1 は手順書の案の改良で導入がいちばん軽く、O1 は観測記録ドックからなくなったモノの一覧の代わりにすぐなり、P3 はプラグインの外では見られない数字（年間の残り）を管理者に届けられる。地名系は N3 で地名が付いているのを確かめてから N1・N2 へ（属性ルールの前の記録にフィールド演算をかけていないと、地名ごとの件数が少なく出る）。

1. 開発用のデータベースで、データ所有者（例 `MIKAGE`）として `dashboard_views_oracle.sql` を流す（SQL*Plus か SQL Developer。何度流してもよい）
2. 各ビューを `SELECT * FROM VW_PLAN_QUOTA_USAGE` などで開き、プラグインの「撮像予定」ドック・「年間撮像上限」の画面・観測記録の行の「流れ」と数が合うか見る。地名系は、プラグインの「データ分析」で観測記録と地名を `NAMED_AREA_OBJECTID` = `OBJECTID` で結合した件数と比べる
3. 閲覧用のロールに `GRANT SELECT ON VW_… TO MIKAGE_VIEWER` を付ける（ファイルの先頭のコメント）
4. ArcGIS Pro で所有者として接続し、各ビューを「ジオデータベースに登録」する（ObjectID は `OBJECTID`。形状のあるビューは元のテーブルと同じ形状の種類・座標系）
5. ビューだけを入れた地図を別のサービス（例 `MIKAGE_DASHBOARD`、参照で公開、フィーチャ アクセスは Query だけ）として公開する。編集用の `MIKAGE` サービスには入れない（`publish_service.py` がレイヤー番号を一覧の順に固定しているため）
6. Map Viewer で Web マップを作り（色は上の各案のとおり）、Dashboards で要素を置く。レイヤーの更新間隔は 5〜10 分

注意:

- 前提: Oracle 19c 以降（`LISTAGG(DISTINCT …)`・`TO_DATE(… DEFAULT NULL ON CONVERSION ERROR)` を使う）、データベースの文字コードは AL32UTF8、サービスの日付のタイムゾーンは既定の UTC
- ビューは問い合わせのたびに計算する。使う結合キー（`REQ_ID`・`SHOT_ID`・`TARGET_DAY`・`OBJECT_ID`・`IMAGE_ID`・`OBS_ID`・`NAMED_AREA_OBJECTID`）には `create_tables.py` が属性インデックスを張ってある。記録が数十万件を超えて `VW_OBS_THING`・`VW_NAMED_AREA_STATUS` が重くなったら、または閲覧する人が多い（数十人が開いたままにする）なら、マテリアライズド ビュー（10〜15 分ごとに更新）に切り替える。閲覧のたびに全件を集計しなくなる
- `NAMED_AREA_OBJECTID` の属性は要る（`create_tables.py` をもう一度実行すると足される）。属性ルールをまだ入れていなければ値が空なので、地名の列は空、地名系のビューは「記録なし」になる
- 元のテーブルを形状なしで作った場合（出力履歴など）は、そのビューの `SHAPE` の行を消す
