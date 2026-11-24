# ArcGIS 側の準備（テーブルの作成とサービスの公開）

MIKAGE が読み書きする Esri のテーブル・フィーチャクラス（「[Esri 側のテーブル定義](admin/esri-tables.md#esri-側のテーブル定義)」の 21 個）をエンタープライズ ジオデータベースに作り、ポータル（ArcGIS Enterprise）にフィーチャサービスとして公開して、プラグインの設定ファイルまで作るスクリプト。`tools/arcgis/` にある。

| ファイル | 中身 | 使うもの |
| --- | --- | --- |
| `mikage_schema.py` | テーブル・属性の定義（docs/admin/esri-tables.md の表と同じ。テストで照合） | 標準ライブラリだけ |
| `create_tables.py` | テーブル・フィーチャクラスを作る・足りない属性を足す・インデックス・権限 | ArcGIS Pro の ArcPy |
| `publish_service.py` | フィーチャサービスとして公開し、プラグインの設定ファイルを書き出す | ArcPy（公開）、ArcGIS API for Python（サービスの確認） |
| `dashboard_views_oracle.sql` | ArcGIS ダッシュボード用の読み取り専用のビュー 13 本（Oracle。[arcgis-dashboards.md](arcgis-dashboards.md)） | SQL*Plus か SQL Developer |

どれも ArcGIS Pro に付いている Python（`propy.bat`）で動く（ArcGIS Pro 3.x で書いた。2.9 以前は未確認）。3 つのファイルは同じフォルダーに置く。

```bat
set PROPY="C:\Program Files\ArcGIS\Pro\bin\Python\scripts\propy.bat"
```

## 1. テーブルを作る（`create_tables.py`）

データ所有者（テーブルを持つユーザー、例 `MIKAGE`）で接続した `.sde` を ArcGIS Pro で作っておき（「データベース接続の作成」）、それを渡す。

```bat
%PROPY% create_tables.py --workspace C:\gis\connections\mikage_owner.sde --sr 6668 ^
    --grant-view MIKAGE_VIEWER --grant-edit MIKAGE_EDITOR
```

| オプション | 中身 |
| --- | --- |
| `--workspace` | 作る先。`.sde`（試しならファイル ジオデータベース `.gdb` も可） |
| `--sr` | フィーチャクラスの座標系の EPSG。既定 4326（WGS 84）。JGD2011 なら 6668。プラグインは何でも読める（サーバーが変換する） |
| `--tables` / `--skip` | 作るテーブルだけ / 作らないテーブル（ほかのシステムが作る `TBL_IMAGE_CATALOG` など） |
| `--geometry` | 形状を変える: `TBL_AI_RESULT=POINT`・`TBL_DETECTION=POINT`（既定はポリゴン）、`TBL_EVENT=NONE`・`TBL_REQUEST_HISTORY=NONE`（範囲なしのテーブル）、`TBL_PLACE_NAME=POLYGON` |
| `--no-edit-tracking` | 作成者・更新者の 4 属性（`CREATED_DATE`・`CREATED_USER`・`UPDATED_DATE`・`UPDATED_USER`）を足さない |
| `--grant-view` / `--grant-edit` | データベースのユーザー・ロールに権限を付ける。編集は書き込みのテーブル（観測記録・撮像依頼など。衛星カタログ・センサーモード・衛星名の対応表も `tools/satellite_editor.html` で直すので書き込み）だけで、読みのテーブル（画像カタログなど）は閲覧だけ |
| `--dry-run` | 何をするかだけ出す |

**何度実行してもよい。** 2 回目からは、ないテーブル・属性・インデックスだけを足す。既にある属性の型・長さが表と違っていても変えず（データを壊さないため）、最後に「表と違うところ」として出す。表にない属性は消さない。プラグインを新しくして属性が増えたら、もう一度実行すれば足りない属性だけ足される。

作るもの:

- テーブル・フィーチャクラス（名前は `TBL_…`、エイリアスは日本語の名前）と、[テーブル定義](admin/esri-tables.md)の表のとおりの属性（名前・エイリアス・型・長さ）
- ID で引く属性と日時の範囲で引く属性の属性インデックス（`OBS_ID`・`IMAGE_ID`・`UPDATED_AT`・`REPORTED_AT` など。名前 `IX_<テーブル>_<属性>`。一覧と使い道は README の[推奨のインデックス](admin/esri-tables.md#推奨のインデックス)）
- バージョン対応・アーカイブ・ArcGIS の編集者トラッキングは付けない（プラグインはバージョン対応なしの編集で、作成者・更新者は自分で 4 属性に書く）

4000 を超える長さの文字列（`TBL_TAXONOMY.SPECS`・`TBL_HANDOVER_MEMO.TEXT` など）は、Oracle では CLOB になる（ArcGIS が決める）。日本語を入れるので、データベースの文字コードは Unicode に。

## 2. サービスを公開する（`publish_service.py publish`）

前提:

1. ジオデータベースが、ポータルとフェデレーションしたサーバーの**データストアに登録**してある（参照で公開する。データはコピーしない）。ArcGIS Pro の「共有」→「データストアの管理」か、Server Manager で登録する
2. 公開に使う `.sde`（登録したものと同じデータベース。公開用ユーザーで接続し、そのユーザーに `--grant-edit` で権限を付けておく）
3. 空の ArcGIS Pro プロジェクト（「新しいプロジェクト」→「マップ」で作って保存したもの）。スクリプトは中に地図 `MIKAGE_SERVICE` を作り直して使い、ほかの地図には触れない
4. 公開の権限のあるポータルのアカウント

```bat
%PROPY% publish_service.py publish ^
    --workspace C:\gis\connections\mikage_publisher.sde ^
    --aprx C:\gis\mikage_publish\mikage_publish.aprx ^
    --portal https://gis.example/portal --server https://gis.example/server ^
    --service MIKAGE --groups MIKAGE利用者 --config C:\gis\mikage.config.json
```

| オプション | 中身 |
| --- | --- |
| `--portal` / `--server` | ポータルの URL / フェデレーションしたサーバーの URL |
| `--user` / `--password` | ポータルのアカウント。`--user` を省くと ArcGIS Pro でサインインしているアカウント。パスワードは省くと環境変数 `MIKAGE_PORTAL_PASSWORD` か入力 |
| `--service` / `--folder` | サービス名（既定 `MIKAGE`）/ サーバー・ポータルのフォルダー |
| `--tables` / `--skip` | 公開するテーブルだけ / 公開しないテーブル |
| `--groups` / `--share-org` | 共有するグループ / 組織に共有する |
| `--max-records` | 1 回の問い合わせで返す上限（既定はサーバーの既定 2000） |
| `--config` | 書き出す設定ファイル（省略すると画面に出す） |
| `--no-verify` | 証明書を確かめない（社内の自己署名証明書のとき） |

すること:

1. 地図 `MIKAGE_SERVICE` にテーブルを一覧の順に入れ、レイヤー名を `TBL_…` にする。レイヤー番号を一覧の順（0 = `TBL_IMAGE_CATALOG` … 19 = `TBL_PLACE_NAME`、20 = `TBL_COUNTRY`、21 = `TBL_REQUEST_GROUP`、22 = `TBL_REQUEST_SHOT`、README の設定の例と同じ番号。あとから足したテーブルは一覧の最後なので、前からのレイヤー番号は変わらない）に固定する（「一意の数値 ID の割り当てを許可」）
2. マップ イメージ レイヤーとして共有するドラフトを作り、フィーチャ アクセスを有効にして、機能を `Query,Create,Update,Delete,Uploads,Editing` にする（プラグインは追加・更新・削除をする）。データは参照（コピーしない）
3. 同じ名前のサービスがあれば上書きして公開する（何度実行してもよい）
4. 公開したサービスを ArcGIS API for Python で読み、レイヤー名と番号からプラグインの設定ファイル（docs/admin/esri-tables.md 「上の表の名前で作ったときの設定」と同じ形）を書く

書き出した設定ファイルは、ほかの設定（`baseMaps`・`requestExports` など）を足してから利用者に配る。既に設定ファイルがあるなら、出てきた URL の部分だけ写せばよい。

### 公開済みのサービスから設定ファイルだけ作る（`publish_service.py config`）

サービスを手で公開したときや、番号を確かめたいとき。レイヤー名が `TBL_…`（`所有者.TBL_…` も可）のレイヤーを拾う。

```bat
%PROPY% publish_service.py config --url https://gis.example/server/rest/services/MIKAGE/FeatureServer ^
    --portal https://gis.example/portal --config C:\gis\mikage.config.json
```

## 3. 撮像予定（採否）のダッシュボード

依頼した人が「その日の計画に採用されたか」を ArcGIS Enterprise のダッシュボードで見るための作り方。依頼の件数で数える・撮像済みを採用に含めるなどを足した改良版（P1）と、ほかの撮像計画・観測結果・地名のダッシュボード・そのためのビューは [arcgis-dashboards.md](arcgis-dashboards.md)。日ごとの撮像予定は出力履歴（`TBL_REQUEST_HISTORY`）の対象日の行で、プラグインの「撮像予定」ドックと同じものを見る（[docs/imaging-requests.md](imaging-requests.md) の §20）。台帳（`TBL_IMAGING_REQUEST`）は計画立案の材料なので使わない。

| 属性 | 中身 | ダッシュボードでの使い方 |
| --- | --- | --- |
| `TARGET_DATE` | 対象日（日本時間 0 時の日付型） | 日付セレクターで絞る・日ごとのグラフ |
| `RESULT` | `採用`（システムに出した）・`見送り`・`要求外`（システムが依頼なしで計画したもの） | 出したか・要求外か |
| `ADOPTION` | システムの採否: `採用`・`不採用`・`不明`（要求外は `採用`） | 計画に入ったか。`不明` は採否の分からないもの（画像が入れば `採用` になる） |
| `CAPTURED` | `未判定`・`撮像済み`・`未撮像` | 撮れたか |
| `REQUEST_GROUP` | 書き出したときの依頼グループ（2026-11-11 に追加。空はグループなし） | カテゴリ セレクターで自分のグループだけを見る |
| `REQ_NAME`・`MANAGEMENT_NO`・`REQUESTER`・`SYSTEM`・`SATELLITE`・`PLANNED_AT`・`ADOPTION_REASON`・`REASON` | 名称・番号・依頼者・システム・衛星・撮像予定日時・理由 | 一覧・ポップアップ |
| 形状（ポリゴン） | 依頼の範囲（要求外は結果ファイルか手で描いた範囲） | 地図 |
| `SHOT_ID` | その依頼を撮る撮像（`TBL_REQUEST_SHOT` の `SHOT_ID`。まとめた依頼は同じ値。2026-11-11 に追加） | 撮像の範囲と結び付ける |
| `PART_NO` | 広い範囲を衛星で分担したときの何番目のショットか（1, 2 …。範囲全体の行は空。2026-11-12 に追加）。行の範囲はそのショットの部分 | 一覧・ポップアップでショットを分ける。依頼ごとに数えるときは `REQ_ID` と `TARGET_DATE` でまとめる |
| `OCCURRENCE` | 1 日に何回も撮る依頼の何回目か（1, 2 …。1 日 1 回の依頼は 1。2026-11-11 に追加） | 一覧・ポップアップで回を分ける。依頼ごとに数えるときは `REQ_ID` と `TARGET_DATE` でまとめる |

撮像の範囲（`TBL_REQUEST_SHOT`、2026-11-11 に追加）は、書き出したファイルの 1 行ごとのポリゴン（撮るシーンの外形。まとめた行はファイルに書いた、まとめた依頼を囲むパスに平行な四角）。依頼の範囲（出力履歴）と別のレイヤーなので、Web マップに両方を入れ、撮像の範囲は塗りなしの破線にすると、依頼ごとの範囲とまとめて撮る範囲を重ねて見られる。属性は `TARGET_DAY`・`TARGET_DATE`・`SYSTEM`・`SATELLITE`・`PASS_TIME`（撮像時刻）・`REQ_IDS`（まとめた依頼）・`REQ_COUNT` など（README 「`TBL_REQUEST_SHOT`」）。

「その日の計画に入ったもの」は `ADOPTION` = `採用`（要求外を含む）。依頼だけなら、さらに `RESULT` = `採用`。

### 属性を足す・ポリゴンにする

既にテーブルを作ってあるなら、プラグインを新しくしてから `create_tables.py` をもう一度実行する（足りない属性だけ足す）。参照で公開したサービスは属性を足しただけでは見えないので、続けて `publish_service.py publish` で上書き公開する（レイヤー番号は変わらない）。

名前を変えた既定の属性（`TBL_ALERT_RULE` の `AREA` → `GEOFENCE_NAME`（2026-11-10。`AREA` は ArcSDE が形状に使う名前で、Oracle のエンタープライズ ジオデータベースではうまく扱えない）、`TBL_REQUEST_HISTORY` の `FILE` → `OUTPUT_FILE`）は、古い名前だけがあるテーブルに新しい名前の属性を足し、古い属性の値をそのまま写す。古い属性は消さないので、古いプラグインが使っていなければ ArcGIS Pro で消してよい（警告に出る）。

出力履歴は形状なしのテーブルで作っていたので、地図に出すならポリゴンのフィーチャクラスで作り直す（`create_tables.py` は既にある表の形状を変えず、形状が違うと警告だけを出す。形状なしのまま使うなら `--geometry TBL_REQUEST_HISTORY=NONE` で警告を止める。プラグインはどちらでも動き、形状なしなら「撮像予定」ドックは台帳の範囲で描く）。作り直すときは、ArcGIS Pro で古い表の名前を変えて `create_tables.py --tables TBL_REQUEST_HISTORY` で作り、「追加（Append）」で古い行を入れてから公開し直す（古い行には形状がない）。

前からある行には `TARGET_DATE` が空なので、過去の日もグラフに出すなら ArcGIS Pro の「フィールド演算」で一度だけ埋める（式の種類 Python）。サービスの日付のタイムゾーンを既定（UTC）のままにしているとき、日本時間の 0 時は UTC の前日 15 時:

```python
datetime.datetime.strptime(!TARGET_DAY![:10], "%Y-%m-%d") - datetime.timedelta(hours=9)
```

古い行の `REQ_NAME`・`MANAGEMENT_NO`・`REQUESTER`・`REQUEST_GROUP` も要るなら、`REQ_ID` で台帳をテーブル結合してからフィールド演算で写す（`REQUEST_GROUP` は台帳・出力履歴とも、プラグインを新しくして `create_tables.py` をもう一度実行すれば足される。依頼グループのテーブル `TBL_REQUEST_GROUP`・撮像の範囲 `TBL_REQUEST_SHOT` と出力履歴の `SHOT_ID` も作られる）。`ADOPTION` が空の古い行（`RESULT` が `採用`）は `不明` と同じに扱う。

### 作り方（例）

1. Map Viewer で Web マップを作り、`TBL_REQUEST_HISTORY` を入れる。シンボルは「個別値」で `ADOPTION`（採用 = 緑、不明 = 青、不採用 = 赤、その他（見送り） = 灰）。要求外を分けて見せるなら、同じレイヤーをもう一度入れて `RESULT` = `要求外` で絞り、紫に。ポップアップに `REQ_NAME`・`MANAGEMENT_NO`・`SYSTEM`・`SATELLITE`・`ADOPTION`・`PLANNED_AT`・`ADOPTION_REASON`・`CAPTURED`
2. ダッシュボードを作り、要素を置く:

| 要素 | データ | 設定 |
| --- | --- | --- |
| 日付セレクター「対象日」 | — | 既定値「今日」（翌日の計画を前日に見るなら「明日」も選べるように）。アクションで下の要素と地図を `TARGET_DATE` で絞る |
| インジケーター「計画に入った」「不明」「不採用」「見送り」 | `TBL_REQUEST_HISTORY` | フィルター `ADOPTION` = `採用`（順に `不明`・`不採用`、見送りは `RESULT` = `見送り`）、統計 = 件数 |
| インジケーター「うち要求外」 | `TBL_REQUEST_HISTORY` | フィルター `RESULT` = `要求外` |
| 円グラフ「システム別」 | `TBL_REQUEST_HISTORY` | カテゴリ `SYSTEM`、フィルター `ADOPTION` = `採用` |
| テーブル（または リスト）「その日の計画」 | `TBL_REQUEST_HISTORY` | 列 `ADOPTION`・`REQ_NAME`・`MANAGEMENT_NO`・`SYSTEM`・`SATELLITE`・`PLANNED_AT`・`REQUESTER`・`ADOPTION_REASON`・`CAPTURED`。並べ替え `PLANNED_AT` |
| マップ | 1. の Web マップ | 日付セレクターで絞る（撮像の範囲 `TBL_REQUEST_SHOT` も `TARGET_DATE` で同じように絞る） |
| 系列グラフ「日ごとの採否」 | `TBL_REQUEST_HISTORY` | カテゴリ `TARGET_DATE`（日単位）、系列を `ADOPTION` で分ける、フィルター `TARGET_DATE` が過去 30 日 |
| カテゴリ セレクター「依頼グループ」 | `TBL_REQUEST_HISTORY` の `REQUEST_GROUP`（グループの一覧を `TBL_REQUEST_GROUP` の `GROUP_NAME` から出してもよい） | 自分のグループの依頼だけを見る。「なし」を許可、既定は「すべて」。アクションで上の要素と地図を `REQUEST_GROUP` で絞る |
| カテゴリ セレクター「依頼者」（任意） | `TBL_REQUEST_HISTORY` の `REQUESTER` | 自分の依頼だけを見る |

- 採否はプラグインの「撮像予定」ドックで入れる（システムの結果ファイルの取り込みか手入力）。分からないものは `不明` のままで、画像が入って撮像済みになったら `採用` になる（`ADOPTION_SOURCE` = `画像の入着`）。
- 同じ対象日・システムを書き出し直すと、そのシステムの未判定の行（採否も）は置き換わる（要求外の行は残る）。
- 日付型は UTC で保存している（日本時間の 0 時 = 前日 15 時 UTC）。ダッシュボードはブラウザーの時刻（日本時間）で日を分けるので、そのままで日本時間の日になる。

## 属性ルール（記録の地名の OBJECTID）

観測記録の `NAMED_AREA_OBJECTID`（2026-11-17 に追加）は、記録の点を含む地名（`TBL_NAMED_AREA`）の `OBJECTID`。**プラグインは書かず**、エンタープライズ ジオデータベースの属性ルール（計算ルール）で入れる。組織内の管理番号は地名の側の `NAMED_AREA_ID`（同じ日に追加）に持つ。

`create_tables.py` をもう一度実行すると、この 2 つの属性と属性インデックス（`IX_OBSERVATION_NAMED_AREA_OBJE`・`IX_NAMED_AREA_NAMED_AREA_ID`）が足される。属性ルールそのものは作らないので、ArcGIS Pro で足す:

1. `TBL_OBSERVATION` にグローバル ID を足す（「グローバル ID の追加」。属性ルールに要る。プラグインは使わない）
2. `TBL_OBSERVATION` の「属性ルール」で計算ルールを足す: フィールド `NAMED_AREA_OBJECTID`、トリガー「挿入」「更新」、式は下の例
3. サービスを上書き公開する（`publish_service.py publish`。式が読む `TBL_NAMED_AREA` も同じサービスに入れておく）

式の例（Arcade。記録を含む地名のうち、いちばん小さいもの。プラグインの通報の `place` と同じ選び方で、`ACTIVE` が 0 のものと、用途 `AREA_USE` が `関心地域` だけのもの（組織の関心地域。2026-11-24 から）は使わない）:

```js
var areas = FeatureSetByName($datastore, "MIKAGE.TBL_NAMED_AREA", ["OBJECTID", "ACTIVE", "AREA_USE"], true);
var best = null;
var smallest = -1;
for (var a in Intersects(areas, Geometry($feature))) {
    if (!IsEmpty(a.ACTIVE) && a.ACTIVE == 0) {
        continue;
    }
    if (a.AREA_USE == "関心地域") {
        continue;
    }
    var size = Area(a);
    if (smallest < 0 || size < smallest) {
        best = a.OBJECTID;
        smallest = size;
    }
}
return best;
```

- `MIKAGE.` はデータ所有者（エンタープライズ ジオデータベースでは所有者付きの名前。ファイル ジオデータベースで試すなら `TBL_NAMED_AREA`）。面積は比べるだけなので、座標系の単位のままでよい
- プラグインは記録を保存するとき `NAMED_AREA_OBJECTID` を送らない（ほかの属性と位置だけを送る）ので、ルールが入れた値を消さない。位置を動かした記録は「更新」のトリガーで入れ直される
- 地名のポリゴンを直しても、前からある記録の値は変わらない（記録が保存されたときに計算する）。入れ直すなら、同じ式を ArcGIS Pro の「フィールド演算」（Arcade）で `TBL_OBSERVATION` の `NAMED_AREA_OBJECTID` に一度かける
- `OBJECTID` は地名のデータを入れ直す（消して追加し直す）と変わる。そのときも上のフィールド演算で入れ直す
- プラグインの「データ分析」で観測記録と地名のポリゴンを結合すると、キーの候補に `NAMED_AREA_OBJECTID` = `OBJECTID` が出る（地名ごとの記録の件数などを `NAMED_AREA_ID` つきで数えられる）
- ArcGIS のダッシュボードで地名ごとに見るなら（いつもと違う地名・管理番号で開く地名カルテ・地名の付け方の点検）、[arcgis-dashboards.md](arcgis-dashboards.md#地名系)

- 2026-11-24 より前にこのルールを作った環境は、式を上のものに替える（`AREA_USE` の 2 行。属性がなければ `create_tables.py` をもう一度実行して足す）。替えないと、記録が組織の関心地域の中にあるとき、その関心地域が記録の地名になる

## 属性ルール（地名の管理番号の自動付番）

地名（`TBL_NAMED_AREA`）の管理番号 `NAMED_AREA_ID` は、すべての行に付け、重ならないようにする（2026-11-24 tsudo「自動採番が欲しい」「全てに付番すること」。組織の関心地域と依頼・記録の紐付けに使う）。形は `NA-000123`（設定ファイルの `namedAreas.idPrefix`・`idDigits` で変えられる。変えたら下のスクリプトにも同じ値を渡す）。

`tools/arcgis/named_area_ids.py`（ArcGIS Pro の Python、データ所有者の .sde）が、まとめて作る。何度実行してもよい:

```bat
"C:\Program Files\ArcGIS\Pro\bin\Python\scripts\propy.bat" named_area_ids.py --workspace C:\gis\connections\mikage_owner.sde
```

1. 管理番号が空の行に、`OBJECTID` の順で、今ある `NA-` の番号のいちばん大きいものの次から番号を付ける（地名だけの行も。重なっている番号があれば一覧で出すので、直してからもう一度）
2. Oracle のシーケンス `SEQ_NAMED_AREA_ID` を、その次の番号から作る（`CreateDatabaseSequence`）
3. `TBL_NAMED_AREA` にグローバル ID を足し、属性ルールを 2 つ足す:
   - 計算ルール `MIKAGE_NAMED_AREA_ID`（トリガー「挿入」「更新」、フィールド `NAMED_AREA_ID`）: 空なら `'NA-' + Text(NextSequenceValue('MIKAGE.SEQ_NAMED_AREA_ID'), '000000')`。手で入れた番号はそのまま
   - 制約ルール `MIKAGE_NAMED_AREA_ID_UNIQUE`（「挿入」「更新」）: ほかの行と同じ番号（大文字・小文字は区別しない）なら保存させない
4. サービスを上書き公開する（`publish_service.py publish`）。プラグインの「関心地域・地点」の「組織」で保存した行にも、ArcGIS Pro・Portal で足した行にも番号が付く

- `--dry-run` で何をするかだけ、`--fill-only` で空の行に番号を付けるだけ（シーケンス・ルールなし。ファイル ジオデータベースで試すときも番号だけ）
- ルールがなくてもプラグインは番号を付ける（保存の後に、いちばん大きい番号の次を書き、もう一度重なりを確かめる。2 台が同時に付けたら `OBJECTID` の大きい行が次の番号に）。ルールがあれば ArcGIS Pro で足した行にも付き、同時の保存でも重ならない
- 手で `NA-` の番号を先の値まで入れると、シーケンスがその番号に追いついたとき制約ルールで保存が止まる。そのときは管理番号を空にして保存し直す（次の番号が付く）か、シーケンスを進める
- 番号を付けた地名は、通報フォーマットの `place.id`・`places.id` に今まで空だった番号も出るようになる

## 前からある記録に衛星名を入れる（任意）

観測記録・確認範囲の `SATELLITE`（衛星名、2026-11-20 に追加）は、プラグインを新しくして `create_tables.py` をもう一度実行すると 2 つのテーブルに足される（参照で公開しているなら続けて `publish_service.py publish` で上書き公開）。プラグインが入れるのは新しい記録・範囲と、直して保存した記録だけで、**前からある記録は空のまま**（ダッシュボードでは「（衛星不明）」）。画像カタログの画像の記録は、画像 ID でカタログと結べば一度に埋められる。カタログ外の画像（画像 ID がファイル名）の記録は、その画像を開いて「衛星不明（入力）」で入れると、その画像の空の記録・範囲に入る。

SQL で埋める（データ所有者で接続。`create_tables.py` で作ったテーブルはバージョン対応・アーカイブなしなので、そのまま更新してよい）。`TBL_IMAGE_CATALOG`・`IMAGE_ID`・`SENSOR` は設定ファイルの `imageCatalogs[].fields`（`id`・`sensor`）に合わせ、ほかのスキーマのテーブルなら所有者を付ける:

```sql
UPDATE TBL_OBSERVATION o
   SET o.SATELLITE = (SELECT MAX(c.SENSOR) FROM TBL_IMAGE_CATALOG c WHERE c.IMAGE_ID = o.IMAGE_ID)
 WHERE o.SATELLITE IS NULL
   AND EXISTS (SELECT 1 FROM TBL_IMAGE_CATALOG c WHERE c.IMAGE_ID = o.IMAGE_ID AND c.SENSOR IS NOT NULL);

UPDATE TBL_OBSERVATION_AREA a
   SET a.SATELLITE = (SELECT MAX(c.SENSOR) FROM TBL_IMAGE_CATALOG c WHERE c.IMAGE_ID = a.IMAGE_ID)
 WHERE a.SATELLITE IS NULL
   AND EXISTS (SELECT 1 FROM TBL_IMAGE_CATALOG c WHERE c.IMAGE_ID = a.IMAGE_ID AND c.SENSOR IS NOT NULL);

COMMIT;
```

- `UPDATED_AT`（修正日時）は変えない。プラグインはこの日時でほかの人の変更を見分けるので、変えると開いている人の画面で「ほかの人が更新した」記録になる。開いている人には、画像を替えるか「最新を取得」で入る
- テーブルをバージョン対応にした・アーカイブを付けたときは SQL で直接更新しない。ArcGIS Pro で `TBL_OBSERVATION` に `TBL_IMAGE_CATALOG` を `IMAGE_ID` で「テーブル結合」し、`SATELLITE` が空の行を選んでから「フィールド演算」で `SATELLITE` = `!TBL_IMAGE_CATALOG.SENSOR!`（確認範囲も同じ）
- 衛星名の書き方は画像カタログのセンサーのまま（プラグインもカタログの書き方にそろえる）

## うまくいかないとき

| 出たこと | すること |
| --- | --- |
| 「データがサーバーに登録されていない」（公開のとき） | ジオデータベースをフェデレーションしたサーバーのデータストアに登録する（前提 1） |
| `! … の形状が …（表は …）。作り直さない` | 既にあるフィーチャクラスの形状が違う。そのまま使うか、中身を退避して消してから作り直す |
| `! ….… は String(255)（表は TEXT(4000)）。変えない` | 既にある属性が短い・型が違う。長さが足りないと長い値の保存に失敗する。ArcGIS Pro で属性を作り直す（データの移し替えが要る） |
| `! ….AREA は前の名前（値は GEOFENCE_NAME に写した）…` | 前の名前の属性が残っている。値は写したので、古いプラグインが使っていなければ消してよい。「写せない」と出たら ArcGIS Pro のフィールド演算で写す |
| `! レイヤー番号を固定できない` | 古い ArcGIS Pro。公開はでき、設定ファイルは実際の番号で書くので、そのまま使える |
| 設定ファイルに書かれない項目がある | そのテーブルを公開していない（`--skip` など）。出力の「サービスにないテーブル」を見る |

テーブル・属性の定義を変えたら（プラグインに機能を足したときなど）、[テーブル定義](admin/esri-tables.md)の表と `tools/arcgis/mikage_schema.py` の両方を直す。`mikage/tests/test_arcgis_tools.py` が食い違いを見つける。
