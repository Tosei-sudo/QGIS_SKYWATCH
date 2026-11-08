# 雲量予報ファイル（閉域向け・GFS）の仕様

閉域ネットワークで Open-Meteo に届かないとき、外部で毎日作った雲量予報ファイルを持ち込み、
撮像計画の「雲量予報」に使うためのファイル仕様。プラグインはこのファイルを読むだけで、
作成（ダウンロード・変換・持ち込み）は外部の処理が受け持つ。

## 元データ

| 項目 | 値 |
|---|---|
| モデル | NOAA NCEP GFS（0.25°） |
| 実行回 | 毎日 00 UTC 初期値（他の回を使ってもよい。ファイルごとに `INIT_TIME` で区別） |
| 変数 | `TCDC`（全雲量、%）、レベル `entire atmosphere`、**瞬間値**（時間平均の通報は使わない） |
| 予報時間 | f000〜f384。f120 までは 1 時間ごと、その先は 3 時間ごと（GFS の出力どおり）。欠けている時刻は入れなくてよい |
| 入手先の例 | AWS Open Data `s3://noaa-gfs-bdp-pds/gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF`（`.idx` で TCDC の部分だけ範囲取得） |
| ライセンス | 米国政府の著作物（パブリックドメイン）。出典表記の例: 「NOAA NCEP GFS」 |

## ファイル形式

| 項目 | 値 |
|---|---|
| 形式 | GeoTIFF（1 ファイル = 1 回の初期値） |
| 座標系 | EPSG:4326（経度・緯度、度） |
| 格子 | 北が上の等間隔格子。GFS の格子点をそのまま **画素の中心** にする（0.25°）。全球なら 1440 × 721 画素、左上の角は経度 −0.125（0〜359.75 の並び）または −180.125（−180〜179.75 の並び）、緯度 90.125。地域だけ切り出してもよい（範囲外の地点は「予報なし」） |
| 型 | Byte（0〜100 = %）。NoData は 255 |
| バンド | 1 バンド = 1 予報時刻。並びは時刻順を推奨（プラグインは時刻で並べ直す） |
| バンドの時刻 | 各バンドのメタデータ `VALID_TIME` に UTC の ISO 8601（例 `2026-11-07T03:00:00Z`）。無ければ `GRIB_VALID_TIME`（1970 年からの秒。GRIB2 から GDAL で変換すると自動で付く）、それも無ければバンドの説明（description）を ISO 8601 で読む。時刻の読めないバンドは使わない |
| ファイルのメタデータ | `INIT_TIME`（初期時刻、ISO 8601 UTC、必須）、`MODEL=GFS`、`VARIABLE=TCDC`、`LEVEL=entire atmosphere`、`UNITS=%`、`SOURCE=NOAA NCEP GFS 0.25`（INIT_TIME 以外は任意） |
| 圧縮（推奨） | `COMPRESS=DEFLATE`、`PREDICTOR=2`、`TILED=YES`、`INTERLEAVE=BAND` |
| 大きさの目安 | 全球で 1 バンド約 0.45 MB（2026-11-06 00Z で実測）。f000〜f384 の全時刻（約 209 バンド）で約 95 MB/日、全期間 3 時間ごと（129 バンド）なら約 60 MB/日。地域の切り出しで面積に比例して小さくなる |
| ファイル名 | `gfs_tcdc_YYYYMMDDHH.tif`（初期時刻 UTC、例 `gfs_tcdc_2026110700.tif`） |

GDAL での作り方の例（f000〜f384 の TCDC だけを入れた GRIB2 を `tcdc.grib2` にまとめた場合）:

```
gdal_translate -ot Byte -a_nodata 255 -co COMPRESS=DEFLATE -co PREDICTOR=2 -co TILED=YES -co INTERLEAVE=BAND \
  -mo INIT_TIME=2026-11-07T00:00:00Z -mo MODEL=GFS -mo VARIABLE=TCDC tcdc.grib2 gfs_tcdc_2026110700.tif
```

GRIB2 から変換すると各バンドに `GRIB_VALID_TIME` が残るので、`VALID_TIME` を書き足さなくても読める
（GDAL は経度を −180〜180 の並びに直す。どちらの並びでもよい）。
GFS の `.idx` では、瞬間値は `:TCDC:entire atmosphere:6 hour fcst:`、時間平均は `:TCDC:entire atmosphere:0-6 hour ave fcst:` の形で区別できる。
入れるのは TCDC の瞬間値の通報だけにする（同じ時刻に時間平均の通報が混ざると、どちらが使われるか決まらない）。
雲量の値は 0〜100（%）、欠測は 255。

## 置き場所と更新

- 共有フォルダ（例 `\\fileserver\weather\gfs`）に置く。プラグインはフォルダ内の `gfs_tcdc_YYYYMMDDHH.tif` の
  うち名前の時刻がいちばん新しいものを読む（名前の形に合うものが無ければ、更新日時が新しい `.tif`）
- 書き込み中のファイルを読まないよう、別名（例 `….tif.part`）で書き終えてから名前を変える
- 古いファイルは残してよい（数日分を残し、それより古いものは消すのを推奨）
- 届かない日があっても、前日のファイルの残りの予報時刻はそのまま使われる（表示に初期時刻と経過日数が出る）

## プラグインの設定（設定ファイル `cloudForecast`）

```jsonc
// フォルダ内の最新のファイルを使う
"cloudForecast": { "folder": "\\\\fileserver\\weather\\gfs", "label": "GFS 雲量" }
// ファイルを 1 つに決める
"cloudForecast": { "file": "D:/weather/gfs_tcdc_latest.tif", "label": "GFS 雲量" }
// これまでどおり Open-Meteo 互換の API
"cloudForecast": { "url": "https://api.open-meteo.com/v1/forecast" }
```

- `url`・`file`・`folder` のどれか 1 つ。パスは絶対パス（UNC 可）
- 地点は 0.25° の格子に丸めてから、その位置を含む画素を読む（最近傍）。時刻は前後のバンドの間を線形に補間する
- ファイルでも、「雲量予報」をオンにするまで何も読まない（API のときと同じ）
- 「雲量マップ」ドックは `file`（`folder` ならいちばん新しいファイル）をそのままラスターで表示し、バンドの時刻をスライダーで切り替える
- 処理ツール「雲量を属性に付与」は `folder` の `gfs_tcdc_YYYYMMDDHH.tif` をすべて読み、各ファイルを次のファイルの初期時刻までつなぐ。古いファイルを消さずに残すと、過去の日時の地物にも雲量が入る
