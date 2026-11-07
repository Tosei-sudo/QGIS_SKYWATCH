# Esri モックサーバー（開発・試験用）

ArcGIS Server / Enterprise がない環境で、プラグインを開発・確認するための **ArcGIS フィーチャーサービスのモック**。Python の標準ライブラリだけで動き、データは SQLite（`data/mock.sqlite`）に保存する（再起動しても残る）。プラグインの zip には入らない。

プラグイン側は何も変えず、設定ファイルのサービスの URL をモックに向けるだけで切り替わる。

## 起動

```
python scripts/mock_esri/mock_esri.py
```

- 初回はサンプルデータ（下記）と画像（`data/files/catalog/*.tif`）を作る
- 起動のたびに `data/config.mock.jsonc`（モックを指す設定ファイル）を書き出す。QGIS でプラグインの「設定…」→ 設定ファイルにこれを選ぶ
- 要求は 1 行ずつ表示する（where、件数、エラー）。`--quiet` で表示しない
- Windows なら QGIS 付属の Python（OSGeo4W Shell の `python`）でも、ほかの Python 3.9 以降でも動く

| オプション | 内容 |
| --- | --- |
| `--port 8765` | 待ち受けるポート（既定 8765） |
| `--host 0.0.0.0` | ほかの PC から使う（既定は 127.0.0.1 のみ）。`--public-url http://<名前>:8765` で設定ファイルの URL も変える |
| `--data <フォルダ>` | データの置き場所（既定 `scripts/mock_esri/data`） |
| `--reset` | データを消してサンプルから作り直す |
| `--empty` | サンプルの地物を入れない（レイヤーの定義だけ） |
| `--token secret` | トークン必須にする（`token=` か `X-Esri-Authorization: Bearer` で `secret`、または `generateToken` で発行したもの）。設定ファイルの各サービスに `token` を書く |
| `--services 定義.json` | レイヤーを足す（下記）。複数可 |

```
python scripts/mock_esri/mock_esri.py new-image   # 画像カタログに「いま登録された」画像を 1 件足す（新着通知・外部AI結果の確認）
python scripts/mock_esri/mock_esri.py config      # 設定ファイルだけ書き出す
```

`new-image` はサーバーを起動したままでも使える（同じ SQLite に書く）。

## サンプルのサービス

URL は `http://127.0.0.1:8765/arcgis/rest/services/<名前>/FeatureServer/<番号>`。属性名はプラグインの既定値（README の各項目）に合わせてある。

| サービス | 番号 | 中身 | 設定ファイルの項目 |
| --- | --- | --- | --- |
| Catalog | 0 | 画像カタログ（ポリゴン、6 件。東京湾の 512×512 の GeoTIFF、`FILE_PATH` と `COG_URL`） | `imageCatalogs` |
| Satellites | 0 | 衛星（OPT-X1・X2 光学、SAR-Y1・Y2 SAR。TLE は起動日の前日元期で生成） | `satelliteCatalogs` |
| Satellites | 1 | SAR-Y のセンサーモード（ストリップマップ・スポットライト・スキャンSAR） | `sensorCatalogs` |
| ImagingPlans | 0 | 撮像計画の保存先（空） | `planStores` |
| Observations | 0 | 観測記録（ポイント、2 件） | `observationStores` |
| Observations | 1 | 分類体系テーブル（`ACTIVITIES` 付き。船舶・航空機・車両・建物に活動） | `observationStores[].taxonomyTable` |
| Observations | 2 | 確認範囲（空） | `observationStores[].areas` |
| AI | 0 | 外部AIの結果（画像ごとの船と誤検出 1 件） | `aiResultSources` |
| Detections | 0 | AI 結果の転記先（空） | `detectionOutputs` |
| Places | 0 | 地名（横須賀港など 6 件） | `geocoders`（`type: "features"`） |
| Requests | 0, 1 | 撮像依頼台帳（3 件）と出力履歴（設計書 imaging-requests-design.md の既定の属性名） | `requestStores`（開発中） |

画像の `FILE_PATH` はこの PC の絶対パス、`COG_URL` はモックの `/files/…`（HTTP の Range に対応。`/vsicurl/` で開ける）。

QGIS の「ArcGIS REST サーバー」接続に `http://127.0.0.1:8765/arcgis/rest/services` を登録すれば、各レイヤーを普通のレイヤーとして開いて編集もできる。ブラウザ版ビューアからも使える（CORS 許可済み）。

## レイヤーを足す・変える

`--services` に次の形の JSON を渡す。同じサービス名・番号のレイヤーがあれば、足りない属性だけ列を足す（データは残る）。作り直すなら `--reset`。

```json
{
  "services": [
    { "name": "MyService",
      "layers": [
        { "id": 0, "name": "巡回記録", "geometryType": "polygon",
          "maxRecordCount": 1000,
          "fields": { "NAME": "string", "COUNT": "integer", "SCORE": "double", "AT": "date",
                      "NOTE": { "type": "string", "length": 4000, "alias": "備考" } },
          "features": [
            { "attributes": { "NAME": "A", "AT": "2026-11-07T00:00:00Z" },
              "geometry": { "rings": [[[139.7, 35.3], [139.8, 35.3], [139.8, 35.4], [139.7, 35.3]]] } } ] },
        { "id": 1, "name": "テーブル", "fields": { "KEY": "string" } }
      ] }
  ]
}
```

- `geometryType`: `point`・`multipoint`・`polyline`・`polygon`（なしでテーブル）
- 型: `string`（既定の長さ 255）・`integer`・`smallinteger`・`biginteger`・`double`・`single`・`date`・`guid`、または `esriFieldType…`
- 座標は WGS 84（経度・緯度）で持つ。`inSR`・`outSR`・ジオメトリの `spatialReference` が 3857 / 102100 なら変換する

## 本物との違い

実際の ArcGIS Server の答え方に寄せているが、次は違う。プラグインの不具合を見つけるのに足りるところまで。

- where は SQLite で実行する（`TIMESTAMP '…'`・`DATE '…'`・`CURRENT_TIMESTAMP` は ms に置き換え）。存在しない属性はエラー。関数は UPPER・LOWER・TRIM・CHAR_LENGTH・SUBSTRING・COALESCE など一部だけ
- 範囲の条件（`geometry`）は外接矩形どうしの重なりで判定する（ポリゴンの形までは見ない）
- 日時は UTC の ms で持つ（`dateFieldsTimeReference` は UTC）
- 文字列が属性の長さを超える・型が違う値は、その地物だけ失敗にする（`rollbackOnFailure` が true なら全部戻す）。レイヤーにない属性は無視して、コンソールに表示する
- 添付ファイル・統計（`outStatistics`）・バージョン管理・リレーション・ジオメトリサービス・マップ画像（MapServer の export）・GP サービスはない
- トークンは `--token` のときだけ確認する。ユーザー名とパスワードは確認しない

## テスト

`satellite_observation/tests/test_mock_esri.py`（QGIS なし）と `test_qgis_mock_esri.py`（QGIS の通信で、カタログ検索・画像表示・外部AI結果・観測記録と分類・撮像計画・QGIS の ArcGIS プロバイダー）。
