# 衛星撮像計画・観測記録（QGIS プラグイン）

Web 画像ビューア（[browser-image-enhancement](https://github.com/Tosei-sudo/browser-image-enhancement) の `usecase/image-viewer`）の「撮像計画」と「観測記録」を QGIS プラグイン（Python / PyQGIS）にしたもの。同じ設定ファイル（`config.json`）と同じ ArcGIS フィーチャーサービスを共用する。

- 対応: QGIS 3.34 LTR 以上（Qt5 / Qt6）
- 追加の依存: `sgp4`（任意。なければ同梱の純 Python 版で動く。入れると約 15 倍速い）。numpy は QGIS 付属のもの
- 閉域ネットワークで動作（外部の暦ファイル・地図タイルを使わない）

> 開発中。現在は段階 2 まで（骨組み、撮像計画の中核: 衛星カタログ・撮像モード・衛星ツリー・撮像機会の計算・組み合わせ・一覧と地図・CSV とレイヤー）。ビューアとの違いは [docs/differences.md](docs/differences.md)。

## 撮像計画

「撮像計画」ドックで、対象（レイヤーの地物・選択中の地物・地図で指定した地点）、開始日時、期間、オフナディア角の上限を選び「計算」（Ctrl+Enter）。衛星の選択・日照・入射角・軌道・向き・TLE の貼り付けは「詳細条件」。

- 一覧の行を選ぶと、地図にその回の地上軌跡（破線）、届く範囲（点線）、撮像範囲（青）、衛星の位置を表示。ダブルクリックか「範囲へ移動」でその場所へ
- 面の対象が 1 回で覆えないときは「組み合わせ（全域を最短で）」で開く。目標の割合まで、複数の衛星のパスを撮像間隔と 1 日の上限を守ってつなぐ
- 「CSV 保存」はビューアと同じ列の CSV（BOM 付き UTF-8）、「レイヤーとして追加」は撮像範囲のメモリレイヤー（撮像日時付き。時系列コントローラーで絞り込み可能）
- 設定ファイルに `satelliteCatalogs` がなければ、架空のサンプル衛星 2 機（光学・SAR、SAR は 3 モード）で試せる

`sgp4` の入れ方（任意）: QGIS の Python コンソールで `import pip; pip.main(["install", "sgp4"])`、または OSGeo4W シェルで `python -m pip install sgp4`。閉域では wheel ファイルを持ち込んで `pip install sgp4-2.*.whl`。

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
SATELLITE_OBSERVATION_SGP4=bundled python3 -m pytest   # 同梱の純 Python 版 SGP4 で
```

## ビューアとの数値比較

```
cd scripts/compare_viewer && npm install
python3 compare.py /path/to/browser-image-enhancement/usecase/image-viewer/src
```

ビューアの `imaging-plan.ts` を esbuild で束ねて Node で動かし、同じ衛星・対象・条件でプラグインの計算と突き合わせる（時刻 ±1 秒、角度 ±0.05°、被覆 ±1%、組み合わせは同じパス）。ネットワークは `npm install` のときだけ使う。
