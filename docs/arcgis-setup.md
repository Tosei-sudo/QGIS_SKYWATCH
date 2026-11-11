# ArcGIS 側の準備（テーブルの作成とサービスの公開）

SKYWATCH が読み書きする Esri のテーブル・フィーチャクラス（README 「[Esri 側のテーブル定義](../README.md#esri-側のテーブル定義)」の 21 個）をエンタープライズ ジオデータベースに作り、ポータル（ArcGIS Enterprise）にフィーチャサービスとして公開して、プラグインの設定ファイルまで作るスクリプト。`tools/arcgis/` にある。

| ファイル | 中身 | 使うもの |
| --- | --- | --- |
| `skywatch_schema.py` | テーブル・属性の定義（README の表と同じ。テストで照合） | 標準ライブラリだけ |
| `create_tables.py` | テーブル・フィーチャクラスを作る・足りない属性を足す・インデックス・権限 | ArcGIS Pro の ArcPy |
| `publish_service.py` | フィーチャサービスとして公開し、プラグインの設定ファイルを書き出す | ArcPy（公開）、ArcGIS API for Python（サービスの確認） |

どれも ArcGIS Pro に付いている Python（`propy.bat`）で動く（ArcGIS Pro 3.x で書いた。2.9 以前は未確認）。3 つのファイルは同じフォルダーに置く。

```bat
set PROPY="C:\Program Files\ArcGIS\Pro\bin\Python\scripts\propy.bat"
```

## 1. テーブルを作る（`create_tables.py`）

データ所有者（テーブルを持つユーザー、例 `SKYWATCH`）で接続した `.sde` を ArcGIS Pro で作っておき（「データベース接続の作成」）、それを渡す。

```bat
%PROPY% create_tables.py --workspace C:\gis\connections\skywatch_owner.sde --sr 6668 ^
    --grant-view SKYWATCH_VIEWER --grant-edit SKYWATCH_EDITOR
```

| オプション | 中身 |
| --- | --- |
| `--workspace` | 作る先。`.sde`（試しならファイル ジオデータベース `.gdb` も可） |
| `--sr` | フィーチャクラスの座標系の EPSG。既定 4326（WGS 84）。JGD2011 なら 6668。プラグインは何でも読める（サーバーが変換する） |
| `--tables` / `--skip` | 作るテーブルだけ / 作らないテーブル（ほかのシステムが作る `TBL_IMAGE_CATALOG` など） |
| `--geometry` | 形状を変える: `TBL_AI_RESULT=POINT`・`TBL_DETECTION=POINT`（既定はポリゴン）、`TBL_EVENT=NONE`・`TBL_REQUEST_HISTORY=NONE`（範囲なしのテーブル）、`TBL_PLACE_NAME=POLYGON` |
| `--no-edit-tracking` | 作成者・更新者の 4 属性（`CREATED_DATE`・`CREATED_USER`・`UPDATED_DATE`・`UPDATED_USER`）を足さない |
| `--grant-view` / `--grant-edit` | データベースのユーザー・ロールに権限を付ける。編集は書き込みのテーブル（観測記録・撮像依頼など）だけで、読みのテーブル（衛星カタログなど）は閲覧だけ |
| `--dry-run` | 何をするかだけ出す |

**何度実行してもよい。** 2 回目からは、ないテーブル・属性・インデックスだけを足す。既にある属性の型・長さが表と違っていても変えず（データを壊さないため）、最後に「表と違うところ」として出す。表にない属性は消さない。プラグインを新しくして属性が増えたら、もう一度実行すれば足りない属性だけ足される。

作るもの:

- テーブル・フィーチャクラス（名前は `TBL_…`、エイリアスは日本語の名前）と、README の表のとおりの属性（名前・エイリアス・型・長さ）
- ID で引く属性の属性インデックス（`OBS_ID`・`IMAGE_ID`・`REQ_ID`・`EVENT_ID` など。名前 `IX_<テーブル>_<属性>`）
- バージョン対応・アーカイブ・ArcGIS の編集者トラッキングは付けない（プラグインはバージョン対応なしの編集で、作成者・更新者は自分で 4 属性に書く）

4000 を超える長さの文字列（`TBL_TAXONOMY.SPECS`・`TBL_HANDOVER_MEMO.TEXT` など）は、Oracle では CLOB になる（ArcGIS が決める）。日本語を入れるので、データベースの文字コードは Unicode に。

## 2. サービスを公開する（`publish_service.py publish`）

前提:

1. ジオデータベースが、ポータルとフェデレーションしたサーバーの**データストアに登録**してある（参照で公開する。データはコピーしない）。ArcGIS Pro の「共有」→「データストアの管理」か、Server Manager で登録する
2. 公開に使う `.sde`（登録したものと同じデータベース。公開用ユーザーで接続し、そのユーザーに `--grant-edit` で権限を付けておく）
3. 空の ArcGIS Pro プロジェクト（「新しいプロジェクト」→「マップ」で作って保存したもの）。スクリプトは中に地図 `SKYWATCH_SERVICE` を作り直して使い、ほかの地図には触れない
4. 公開の権限のあるポータルのアカウント

```bat
%PROPY% publish_service.py publish ^
    --workspace C:\gis\connections\skywatch_publisher.sde ^
    --aprx C:\gis\skywatch_publish\skywatch_publish.aprx ^
    --portal https://gis.example/portal --server https://gis.example/server ^
    --service SKYWATCH --groups SKYWATCH利用者 --config C:\gis\skywatch.config.json
```

| オプション | 中身 |
| --- | --- |
| `--portal` / `--server` | ポータルの URL / フェデレーションしたサーバーの URL |
| `--user` / `--password` | ポータルのアカウント。`--user` を省くと ArcGIS Pro でサインインしているアカウント。パスワードは省くと環境変数 `SKYWATCH_PORTAL_PASSWORD` か入力 |
| `--service` / `--folder` | サービス名（既定 `SKYWATCH`）/ サーバー・ポータルのフォルダー |
| `--tables` / `--skip` | 公開するテーブルだけ / 公開しないテーブル |
| `--groups` / `--share-org` | 共有するグループ / 組織に共有する |
| `--max-records` | 1 回の問い合わせで返す上限（既定はサーバーの既定 2000） |
| `--config` | 書き出す設定ファイル（省略すると画面に出す） |
| `--no-verify` | 証明書を確かめない（社内の自己署名証明書のとき） |

すること:

1. 地図 `SKYWATCH_SERVICE` にテーブルを一覧の順に入れ、レイヤー名を `TBL_…` にする。レイヤー番号を一覧の順（0 = `TBL_IMAGE_CATALOG` … 19 = `TBL_PLACE_NAME`、20 = `TBL_COUNTRY`、21 = `TBL_REQUEST_GROUP`、22 = `TBL_REQUEST_SHOT`、README の設定の例と同じ番号。あとから足したテーブルは一覧の最後なので、前からのレイヤー番号は変わらない）に固定する（「一意の数値 ID の割り当てを許可」）
2. マップ イメージ レイヤーとして共有するドラフトを作り、フィーチャ アクセスを有効にして、機能を `Query,Create,Update,Delete,Uploads,Editing` にする（プラグインは追加・更新・削除をする）。データは参照（コピーしない）
3. 同じ名前のサービスがあれば上書きして公開する（何度実行してもよい）
4. 公開したサービスを ArcGIS API for Python で読み、レイヤー名と番号からプラグインの設定ファイル（README 「上の表の名前で作ったときの設定」と同じ形）を書く

書き出した設定ファイルは、ほかの設定（`baseMaps`・`requestExports` など）を足してから利用者に配る。既に設定ファイルがあるなら、出てきた URL の部分だけ写せばよい。

### 公開済みのサービスから設定ファイルだけ作る（`publish_service.py config`）

サービスを手で公開したときや、番号を確かめたいとき。レイヤー名が `TBL_…`（`所有者.TBL_…` も可）のレイヤーを拾う。

```bat
%PROPY% publish_service.py config --url https://gis.example/server/rest/services/SKYWATCH/FeatureServer ^
    --portal https://gis.example/portal --config C:\gis\skywatch.config.json
```

## 3. 撮像予定（採否）のダッシュボード

依頼した人が「その日の計画に採用されたか」を ArcGIS Enterprise のダッシュボードで見るための作り方。日ごとの撮像予定は出力履歴（`TBL_REQUEST_HISTORY`）の対象日の行で、プラグインの「撮像予定」ドックと同じものを見る（[docs/imaging-requests.md](imaging-requests.md) の §20）。台帳（`TBL_IMAGING_REQUEST`）は計画立案の材料なので使わない。

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

## うまくいかないとき

| 出たこと | すること |
| --- | --- |
| 「データがサーバーに登録されていない」（公開のとき） | ジオデータベースをフェデレーションしたサーバーのデータストアに登録する（前提 1） |
| `! … の形状が …（表は …）。作り直さない` | 既にあるフィーチャクラスの形状が違う。そのまま使うか、中身を退避して消してから作り直す |
| `! ….… は String(255)（表は TEXT(4000)）。変えない` | 既にある属性が短い・型が違う。長さが足りないと長い値の保存に失敗する。ArcGIS Pro で属性を作り直す（データの移し替えが要る） |
| `! ….AREA は前の名前（値は GEOFENCE_NAME に写した）…` | 前の名前の属性が残っている。値は写したので、古いプラグインが使っていなければ消してよい。「写せない」と出たら ArcGIS Pro のフィールド演算で写す |
| `! レイヤー番号を固定できない` | 古い ArcGIS Pro。公開はでき、設定ファイルは実際の番号で書くので、そのまま使える |
| 設定ファイルに書かれない項目がある | そのテーブルを公開していない（`--skip` など）。出力の「サービスにないテーブル」を見る |

テーブル・属性の定義を変えたら（プラグインに機能を足したときなど）、README の表と `tools/arcgis/skywatch_schema.py` の両方を直す。`skywatch/tests/test_arcgis_tools.py` が食い違いを見つける。
