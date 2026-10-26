# ビューアとの違い・できなかった点

参照実装: Tosei-sudo/browser-image-enhancement `8a8563d`（`usecase/image-viewer`）。段階ごとに追記。

## 段階 1（骨組み）

| 項目 | ビューア | プラグイン | 理由 |
| --- | --- | --- | --- |
| 設定ファイルの形式 | JSON（README に「コメントは書けません」） | JSON と JSONC（`//`・`/* */`・末尾のカンマ） | 指示書どおり。ビューアの `config.json` はそのまま読める |
| 読む項目 | 全項目 | `satelliteCatalogs`・`sensorCatalogs`・`planStores`・`groundStations`・`cloudForecast`・`imageCatalogs`・`observationStores`・`observationVocabulary`・`arcgisPortals` のみ（他は無視、問題にも出さない） | 共用のため |
| サインイン | `arcgisPortals` の OAuth（PKCE、ポップアップ） | QGIS の認証設定（`authcfg`）をサービスごとに選ぶ。`arcgisPortals` は読み込みと検証のみ | QGIS の OAuth2 / ESRI token 認証に任せる |
| `token` と認証設定 | `token` があればそれ、なければサインインのトークン | 認証設定を選んだサービスは `token` を送らない | 二重送信を避けるため |
| 全件読み込み | `resultOffset` で `exceededTransferLimit` が立つ間 | 同じ。加えて、ページが満杯なら次も読む。ページングできないレイヤーは ID を先に読み `maxRecordCount` ずつ取得 | 指示書「`maxRecordCount` を超える件数はページングで全件読む」 |
| 通信 | `fetch`（GET か POST） | すべて form POST（`f=json`）、`QgsBlockingNetworkRequest`（QgsTask 内のみ） | トークンを URL に出さない・QGIS のプロキシと認証に従う |
| ローカル保存 | IndexedDB・localStorage | ユーザープロファイルの `satellite_observation/observations.gpkg`（`observations`・`checked_areas`・`taxonomy`・`taxon_photos`） | 指示書どおり。属性名は Esri と同じ |
| ローカルの分類体系 | localStorage に保存先ごと | `taxonomy` テーブルに `STORE`（保存先の label）と `ROW_KEY` の列を追加 | 1 つのテーブルで保存先ごとに持つため |
| 日時（ローカル） | ms の数値 | GeoPackage の DATETIME（UTC の ISO 8601） | QGIS・ArcGIS Pro で日時として扱えるように |

## 参照実装側で見つけた点

- README の `config.json` の例（設定ファイルの節）は `detectionOutputs` の後ろの `],` が抜けていて JSON として読めない。テスト用の写し（`satellite_observation/tests/data/viewer-config.jsonc`）では補った
