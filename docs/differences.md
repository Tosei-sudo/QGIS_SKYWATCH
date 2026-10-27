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

## 段階 2（撮像計画の中核）

数値の一致は `scripts/compare_viewer/compare.py` で確かめる（ビューアの `imaging-plan.ts` を esbuild で束ね Node で実行し、同じ条件をプラグインの core で計算して突き合わせる）。7 ケース（ISS の 1 週間・6 地点、サンプル 2 機の 14 日、条件の絞り込みと地上局、面の組み合わせ、優先度と地点の集約、撮像モード、架空の 20 機の 1 週間）で機会の件数・時刻・角度・被覆・組み合わせのパスはすべて一致し、差は 0（時刻 0.000 s・角度 0.00000°・被覆 0.000%）。

| 項目 | ビューア | プラグイン | 理由 |
| --- | --- | --- | --- |
| SGP4 | satellite.js 7.1.0 | `sgp4`（2.27、C++ 版）。なければ同梱の純 Python 版（`ext/sgp4`、同じ 2.27 の Python 実装） | 指示書どおり。どちらも WGS72・同じ結果 |
| 太陽位置・恒星時・座標変換 | satellite.js の `sunPos`・`gstime`・`eciToGeodetic` | 同じ式を Python に移植（`core/plan/orbit.py`） | 外部の暦ファイルを使わず閉域で動かすため（Skyfield は不要） |
| 速さ（20 機 × 7 日 × 1 地点） | 約 0.2 秒 | `sgp4`（C++）あり: 約 0.2 秒。同梱の純 Python 版: 約 3 秒 | 純 Python の SGP4 が遅いため。1 秒以内は `sgp4` を入れた場合だけでよい（2026-10-27 に決定） |
| 計算の実行 | Web Worker（ダイアログを閉じると停止） | QgsTask（ステータスバーに進捗、そこかドックの「取消」・Esc で取消） | QGIS の作法 |
| 衛星カタログの読み込み | 1 回の問い合わせ（上限を超えたら「先頭 n 機のみ」） | 全件をページングで読む | 段階 1 の Esri クライアントの方針に合わせた |
| 1 つの面の狙う点 | OpenLayers の `getInteriorPoint` | QGIS の `pointOnSurface` | 面の内側の点の選び方が少し違う。点・複数地物・セル（面積の標本）は同じ |
| 面のセル | OpenLayers の `intersectsCoordinate` | 同じ格子、内外判定は自前の偶奇判定（穴を除く） | 境界上の点の扱いだけ異なりうる |
| 対象の選び方 | 選択中の地物・各レイヤー・地図でクリックした地点 | 同じ。「選択中の地物」はアクティブレイヤーの選択。地点は「地図で指定」を押してから地図をクリック | QGIS では地図のクリックを他のツールと取り合うため、明示的にツールを切り替える |
| 地図の表示 | OpenLayers のオーバーレイ | ラバーバンドとキャンバス上の文字（プロジェクトにレイヤーを足さない） | 計画を開いている間だけの表示のため |
| 衛星名の表示 | 衛星の点の上 | 衛星の点の右 | シーンと重なりにくくするため |
| レイヤーとして追加 | Polygon か MultiPolygon（地物ごと） | すべて MultiPolygon・WGS84 のメモリレイヤー。`time` で時系列コントローラーが効く | QGIS の 1 レイヤー 1 形状型のため |
| 並べ替えの文字列 | `Intl.Collator('ja', {numeric: true})` | 数字を数として比べる自然順（それ以外は文字コード順） | ICU の日本語照合順は使わない。かなと漢字の並びが異なりうる |
| CSV | BOM 付き UTF-8・CRLF | 同じ列・同じ書式。ローカル時刻は OS のタイムゾーン | — |
| ショートカット | ダイアログ内の Esc で閉じる | ドックにフォーカスがあるときだけ Ctrl+Enter で計算、Esc で取消 | 指示書どおり |
| 右クリックの「ここを撮像計画」 | 地図の右クリックメニュー | 同じ（QGIS の地図の右クリックメニューに追加。その地点を対象にして計算） | — |
| 3D 表示 | あり | なし | 移植しない（2026-10-27 に決定） |
| 段階 4 以降の機能 | 保存と追跡・通知と予定表・雲量予報・ペア・再訪間隔・移動目標 | 段階 4〜5 で追加 | 段階分けのため。地上局（`groundStations`）がある場合の「受信見込み」は中核が求めるので、段階 2 から一覧・CSV・レイヤーに出る |
| 同梱 sgp4 の変更 | — | NumPy 2.5 で非推奨になった `.shape` への代入を `reshape` に置き換え（2 か所） | 警告をなくすため。計算は同じ |

## 段階 3（組み合わせ・優先度・シーン共有・ドラッグ・タイムライン）

| 項目 | ビューア | プラグイン | 理由 |
| --- | --- | --- | --- |
| 優先度の属性 | 数値を持つ属性から選ぶ（名前に priority・優先 を含むものを自動で選ぶ）。「小さいほど優先」「大きいほど優先」 | 同じ。数値の判定は先頭 2000 件の地物の値で行う | — |
| 撮像範囲のドラッグ | 選んだ回の撮像範囲をいつでもドラッグ | 「撮像範囲を移動」を押している間だけドラッグ（地図ツール）。動かした回は一覧に ✎、CSV・レイヤーも動かした位置で出力 | QGIS では地図のドラッグをパンなど他のツールと取り合うため |
| タイムライン | SVG（日付の線、撮像間隔の帯、対象ごとの色、クリックで選択） | 同じ内容を Qt で描画。色は QGIS のテーマに合わせる | — |
| 組み合わせ・シーンの共有 | `coverPlan` | 同じ（段階 2 で移植済み。数値比較で一致） | — |

## 表示の言語

画面の文言は日本語のみ（2026-10-27 に決定）。ドックの文言は `self.tr()` で包んでいるが、一覧の上の要約文など core が作る文は翻訳の対象にしていない。

## 参照実装側で見つけた点

- README の `config.json` の例（設定ファイルの節）は `detectionOutputs` の後ろの `],` が抜けていて JSON として読めない。テスト用の写し（`satellite_observation/tests/data/viewer-config.jsonc`）では補った
