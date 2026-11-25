# 参考情報の JSON ファイル

ドック「参考情報」の「JSON 取り込み…」で読むファイルの形。ほかのシステムの担当者に渡すのは次の 2 つ。

| ファイル | 中身 |
| --- | --- |
| [`mikage/resources/schemas/reference-info.schema.json`](../../mikage/resources/schemas/reference-info.schema.json) | JSON Schema（draft 2020-12）。プラグインもこのファイルで検証する |
| [`sample.json`](sample.json) | 見本（更新・地点つき・地名の中の点の 3 件） |

```json
{
  "defaults": { "source": "海上保安庁", "reliability": "確定" },
  "infos": [
    { "id": "NAV-2026-0123", "title": "〇〇沖で射撃訓練（航行警報）", "time": "2026-11-28T09:00:00+09:00",
      "sourceRef": "航行警報 第123号", "area": "NA-000012", "links": [{ "type": "event", "id": "EV-20261120-0001" }] },
    { "title": "△△港に大型クレーン設置と報道", "time": "2026-11-20", "source": "〇〇新聞", "lon": 139.652, "lat": 35.284 }
  ]
}
```

| キー | 必須 | 中身（かっこ内は文字数の上限） |
| --- | --- | --- |
| `id` | | 情報ID（64）。同じ ID があれば更新、なければこの ID で追加。省くと `REF-…` の ID で追加 |
| `title` | ○ | 題名（255） |
| `time` | ○ | 出来事の日時。`2026-11-24`・`2026-11-24T10:30`・`2026-11-24T10:30:00+09:00`・`…Z`。タイムゾーンのないものは PC の時刻 |
| `published` | | 発表日時（形は `time` と同じ） |
| `source` | | 出典（255） |
| `sourceRef` | | 文書番号か URL（1000） |
| `reliability` | | 確からしさ（64）。観測記録の確度と同じ言葉（既定は確定・推定・不明） |
| `summary` | | 要旨（4000） |
| `kind` | | 分類（1000）。`船舶 > 支援船` |
| `area` | | 地名の管理番号（`NAMED_AREA_ID`）か、地名の名前・別名（255） |
| `lon`・`lat` | | 地点の経度・緯度（WGS 84、両方） |
| `note` | | メモ（2000） |
| `links` | | 紐付け。`{ "type": "event" / "thing" / "observation", "id", "label" }` の配列 |

- 更新で変わるのはファイルに書いたキーだけ。`""` か `null` で消す（`title`・`time` は消せない）。紐付けは足すだけ
- `id` のない行は、題名・出来事の日時（分まで）・出典が同じ参考情報が既にあれば重複として飛ばす（同じファイルを 2 回取り込んでも増えない）
- 場所は `lon`・`lat`、なければ `area` の地名の中の点、どちらもなければ場所なし
- `defaults` は各行に書いていないキーに入る値（`id`・`title`・`time`・`lon`・`lat`・`links` は書けない）
- 文字数の上限は既定のテーブル定義（[`TBL_REFERENCE_INFO`](../admin/esri-tables.md#tbl_reference_info参考情報)）の長さ。実際のテーブルの属性が短ければ、それを超える行はエラー（切らない）
- エディター（VS Code など）で補完させるときは、ファイルの頭に `"$schema": "<スキーマへのパス>"` を書く（プラグインは読まない）
- スキーマを変えたら `mikage/tests/test_reference_import.py` が、見本がスキーマに合うこと、上限と紐付けの種類がプラグインの定義と同じことを確かめる
