# 衛星撮像計画・観測記録（QGIS プラグイン）

Web 画像ビューア（[browser-image-enhancement](https://github.com/Tosei-sudo/browser-image-enhancement) の `usecase/image-viewer`）の「撮像計画」と「観測記録」を QGIS プラグイン（Python / PyQGIS）にしたもの。同じ設定ファイル（`config.json`）と同じ ArcGIS フィーチャーサービスを共用する。

- 対応: QGIS 3.34 LTR 以上（Qt5 / Qt6）
- 追加の依存: `sgp4`（同梱のフォールバックあり。段階 2 で追加）

> 開発中。現在は段階 1（骨組み: プラグインの登録、設定ファイルの読み込みと検証、Esri クライアント、ローカル GeoPackage）。ビューアとの違いは [docs/differences.md](docs/differences.md)。

## 導入

1. `python3 scripts/build_zip.py` で `dist/satellite_observation-<版>.zip` を作る
2. QGIS の「プラグイン」→「プラグインの管理とインストール」→「ZIP からインストール」
3. メニュー「プラグイン」→「衛星撮像計画・観測記録」→「設定…」で設定ファイル（パスか URL）を指定し「読み込み」。問題があれば一覧に出る

## 設定

| 項目 | 保存先 |
| --- | --- |
| 設定ファイルのパス・URL、記録者、サービスごとの認証設定 | `QgsSettings`（`satellite_observation/…`） |
| 観測記録・確認範囲・分類体系・写真（ローカル） | ユーザープロファイルの `satellite_observation/observations.gpkg` |

設定ファイルはビューアの `config.json` をそのまま読む（コメント付き JSON も可）。読む項目は `satelliteCatalogs`・`sensorCatalogs`・`planStores`・`groundStations`・`cloudForecast`・`imageCatalogs`・`observationStores`・`observationVocabulary`・`arcgisPortals` で、ほかの項目は無視する。

認証: ArcGIS のサービスごとに QGIS の認証設定（OAuth2・ESRI token・Basic など）を「設定…」の表で選ぶ。設定ファイルの `token` も使えるが、認証設定を選んだサービスでは送らない。トークンや認証情報はログ・CSV・レイヤー・プロジェクトファイルに残さない。

## 構成

```
satellite_observation/
  core/      純 Python（Qt / QGIS に依存しない）: 設定・Esri・GeoPackage・撮像計画・観測記録
  qgis_io/   QGIS の通信（QgsNetworkAccessManager）・QgsTask・設定
  ui/        ドック・ダイアログ
  tests/     pytest
```

## テスト

```
python3 -m pytest                                     # core だけ（QGIS なしで動く）
QT_QPA_PLATFORM=offscreen python3 -m pytest            # QGIS の Python があれば画面・通信のテストも
```
