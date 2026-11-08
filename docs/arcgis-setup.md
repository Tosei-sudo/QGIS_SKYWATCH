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
| `--geometry` | 形状を変える: `TBL_AI_RESULT=POINT`・`TBL_DETECTION=POINT`（既定はポリゴン）、`TBL_EVENT=NONE`（範囲なしのテーブル）、`TBL_PLACE_NAME=POLYGON` |
| `--no-edit-tracking` | 作成者・更新者の 4 属性（`CREATED_DATE`・`CREATED_USER`・`UPDATED_DATE`・`UPDATED_USER`）を足さない |
| `--grant-view` / `--grant-edit` | データベースのユーザー・ロールに権限を付ける。編集は書き込みのテーブル（観測記録・撮像計画など）だけで、読みのテーブル（衛星カタログなど）は閲覧だけ |
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

1. 地図 `SKYWATCH_SERVICE` にテーブルを一覧の順に入れ、レイヤー名を `TBL_…` にする。レイヤー番号を一覧の順（0 = `TBL_IMAGE_CATALOG` … 20 = `TBL_PLACE_NAME`、README の設定の例と同じ番号）に固定する（「一意の数値 ID の割り当てを許可」）
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

## うまくいかないとき

| 出たこと | すること |
| --- | --- |
| 「データがサーバーに登録されていない」（公開のとき） | ジオデータベースをフェデレーションしたサーバーのデータストアに登録する（前提 1） |
| `! … の形状が …（表は …）。作り直さない` | 既にあるフィーチャクラスの形状が違う。そのまま使うか、中身を退避して消してから作り直す |
| `! ….… は String(255)（表は TEXT(4000)）。変えない` | 既にある属性が短い・型が違う。長さが足りないと長い値の保存に失敗する。ArcGIS Pro で属性を作り直す（データの移し替えが要る） |
| `! レイヤー番号を固定できない` | 古い ArcGIS Pro。公開はでき、設定ファイルは実際の番号で書くので、そのまま使える |
| 設定ファイルに書かれない項目がある | そのテーブルを公開していない（`--skip` など）。出力の「サービスにないテーブル」を見る |

テーブル・属性の定義を変えたら（プラグインに機能を足したときなど）、README の表と `tools/arcgis/skywatch_schema.py` の両方を直す。`skywatch/tests/test_arcgis_tools.py` が食い違いを見つける。
