# 構成と依存のきまり（モジュールの独立性）

機能が増えても 1 件の改修が 1 つの機能の中で済むように、機能ごとの部品を共通基盤の上に並べ、部品同士は決まった窓口だけでつなぐ。きまりはテストで守る（違反を増やすとテストが落ちる）。機能・画面・設定ファイルの書き方・Esri のテーブル定義は、この形にしたときに何も変えていない。

## 層

```
plugin.py ── features/<機能>.py（登録票） ── ui/ ── qgis_io/ ── core/
                         │                 └ ui/ports.py（窓口・通知）
                         └ plugin_api.py（plugin.api）
```

| 層 | 中身 | import してよいもの |
| --- | --- | --- |
| `core/` | 純 Python（QGIS・Qt なし）。機能ごとのパッケージ（`core/ai`・`catalog`・`enhance`・`obs`・`plan`・`request`）と共通基盤 | core だけ |
| `qgis_io/` | QGIS との出入り（通信・QgsTask・個人設定・レイヤー） | core |
| `ui/` | ドック・ダイアログ | qgis_io・core |
| `features/` | 機能の登録票（ドック・メニュー・右クリック・先読み・つなぎ） | ui・qgis_io・core（ほかの機能の登録票は不可） |
| `plugin.py` | 登録票を並べて起動・終了するだけ | すべて |

## きまり（`mikage/tests/architecture.py`）

| きまり | 内容 |
| --- | --- |
| R1 層の向き | 上の表のとおり。逆向きは禁止（違反 0） |
| R2 機能の窓口 | 機能パッケージの外からは `core/<機能>/api.py` だけを import する |
| R3 他オブジェクトの中に触らない | ui・features・plugin.py で `ほか._名前` を使わない（窓口か通知で頼む） |
| R4 共通基盤は機能を知らない | `KERNEL` のモジュールは KERNEL だけを import する（違反 0） |
| R5 依存を隠さない | 関数の中の import も含めて、機能をまたぐ循環を作らない（違反 0） |

いまある違反は台帳（`mikage/tests/architecture_baseline.json`）に載っていて、新しい違反はテストが落とす。直したら `MIKAGE_UPDATE_ARCHITECTURE=1 pytest mikage/tests/test_architecture.py` で台帳から外す（台帳は減らすだけ）。2026-11-20 時点の台帳: R2 63 件（はじめは 370）、R3 6 モジュール 8 か所（はじめは 11 モジュール 49 か所）、R4・R5 0 件（はじめは 4 件・1 件）。

画面は `mikage/tests/test_qgis_screen_snapshot.py` が写し（`tests/snapshots/screen.json`: メニュー・ツールバー・ドックの名前と置き場と中の部品・右クリック・plugin.api・設定キー・個人設定のキー）と比べる。画面を変えたときだけ `MIKAGE_UPDATE_SNAPSHOT=1` で撮り直す。

## 機能の登録票（`mikage/features/`）

1 つのファイルが 1 つの機能（`Feature` の子クラス）。`plugin.FEATURES` がその並び。登録票はフックで本体に答える。

| フック | 答えるもの |
| --- | --- |
| `dock_specs()` | 自分のドック `{名前: DockSpec(タイトル, アイコン, 作り方)}`（置き場は `plugin.DOCK_GROUPS`、並びは `DOCK_ORDER`） |
| `connect()` | ドックができたあとのつなぎ（窓口・通知の受け取り） |
| `add_actions()` | メニューの項目（`self.entry(...)`） |
| `map_menu()` | 地図の右クリック `{文言: fn(x, y)}`（並びは `plugin.MAP_MENU`） |
| `preload()` | 先読み `[(キー, 表示名, 始め方, いつも)]`（並びは `plugin.PRELOAD_ORDER`） |
| `COMMANDS` | `plugin.open_xxx()` などで呼べる名前（plugin.api・テスト・ヘルプのため） |
| `start` / `qgis_started` / `roles_changed` / `after_offline` / `unload` / `unload_after_docks` | 起動・担当の変更・オフラインからの復帰・終了 |

`mikage/tests/test_qgis_features.py` が、1 ファイル 1 機能、すべてのドック・右クリック・先読みに置き場があること、機能が他の機能のドックの名前を直接書かないことを確かめる。

## 窓口と通知（`mikage/ui/ports.py`）

機能が他の機能に頼むことは、小さな窓口（Protocol）を通す。担当で使えない機能の窓口は `None` を返す（いままでと同じ）。

| 窓口 | 中身 |
| --- | --- |
| `ports.observations()` | 観測記録（記録を開く・画像を記録する・通報済みの印など） |
| `ports.catalog()` | 画像カタログ（画像を一覧に出す・期間と範囲で探す・イベントの画像を出す） |
| `ports.requests()` | 撮像依頼（読んだか・読み直す・選ぶ・計画から依頼を作る） |
| `ports.satellites` | 衛星一覧（撮像計画の衛星カタログ） |
| `ports.events()` | イベント（「イベントに追加」） |
| `ports.notices()` | 新着画像の通知 |

「変わった」の知らせは `PluginEvents`（Qt シグナル。例: `requests_changed`）。ドックの中の `_loaded` や部品を他の機能が見ることはしない。窓口の形は `test_qgis_features.py` がドックの実物と突き合わせる。

## 機能の公開窓口（`core/<機能>/api.py`）

機能パッケージの外（画面・qgis_io・他の機能・plugin.py）は `core/<機能>/api.py` から import する。パッケージの中は互いに直接 import してよい。中を並べ替えても外は壊れない。

## 共通基盤（`KERNEL`）

`core.esri`（EsriClient・Transport）、`core.util`、`core.time_zone`・`core.time_text`（日時）、`core.sql`（SQL の日時の書き方）、`core.sources`（Esri の表の指定）、`core.shapes`（図形）、`core.audit`・`core.edit_stamp`・`core.offline`・`core.auth`・`core.gpkg` ほか、`qgis_io.tasks`・`qgis_io.network`・`qgis_io.geometry`、`ui.fit` など。一覧は `architecture.py` の `KERNEL`。

## データ窓口（リポジトリ）

画面は Esri の読み書きを、保存先・クライアント・トークンを結んだリポジトリを通して行う。画面は処理を始めるときに `self.repository()` で作り（そのときのトークンで）、処理の中で使う。テストは画面に別のリポジトリを渡せる。

| リポジトリ | ファイル | 中身 |
| --- | --- | --- |
| `RequestRepository` | `core/request/repository.py` | 撮像依頼の台帳（読む・保存・一括変更・削除・グループ） |
| `ScheduleRepository` | 同上 | 撮像予定（出力履歴・撮像・年間上限・照合・ショットと画像の対応）と台帳 |
| `AssignmentRepository` | `core/obs/repository.py` | 分担（読む・取る・変える・手放す） |
| `AlertLogRepository` | 同上 | 通報記録（通報済み・書く・読む・消す・新着の確認） |

各メソッドは同じ名前の関数に三つを渡すだけで、SQL・TBL_ の名前・日時の書き方はその関数と基盤だけが知る。SQL の `TIMESTAMP '…'`・`DATE '…'` は `core/sql.py` だけが書く（`test_repositories.py` が他のモジュールの直書きを落とす）。

## 設定

- 設定ファイル: `core/config.py` の `SECTIONS` が、キー・`PluginConfig` の項目・読み方の一覧表（読む順 = 問題を知らせる順）。`KEYS` はそこから作る。新しいキーは `PluginConfig` の項目と `SECTIONS` の 1 行、設定ビルダー（`tools/config_builder.html`）の画面を足し、`python3 -m mikage.tests.test_config_builder` で定数を書き直す
- 個人設定（QgsSettings）: 機能ごとのモジュール `qgis_io/prefs/<機能>.py`（画像・観測記録・撮像計画・地図・AI・撮像依頼）。読み書きは `qgis_io/qsettings.py` の `pref` / `set_pref`（キーは `mikage/<キー>`）。`qgis_io/settings.py` から従来どおり `settings.<名前>` で読める

## 大きなドックの部分（`mikage/ui/obs_dock/`）

観測記録ドックは担当ごとのファイル（通報・画像・記録のしかた・地図での操作・入力欄・記録の操作・書き出しと連携・一覧・送信待ち・表示・地図の印）の部分を組み合わせたクラス。`observation_dock.py` に残るのは組み立て（`_build`）・保存先・読み込みと実行の流れ。部分はドックの部品と状態（`self`）をそのまま使う。`test_obs_dock_parts.py` が、すべての部分がドックに入り、同じ名前のメソッドが二か所にないことを確かめる。

## 改修の手引き

- 機能を足す: `features/<機能>.py` に `Feature` の子クラスを書き、`plugin.FEATURES` に足す。ドックなら `DOCK_ORDER`・`DOCK_GROUPS` にも
- 他の機能のデータや操作を使う: その機能の窓口（`ui/ports.py`）か `core/<機能>/api.py` を使う。足りなければ窓口に足す（相手のドックの中を直接見ない）
- 「変わった」を知らせる: `PluginEvents` にシグナルを足して、受け取る側の登録票の `connect()` でつなぐ
- Esri の表を読み書きする: その機能のリポジトリにメソッドを足す（画面で `client, token` を組まない）
- 説明: 機能ごとの `docs/features/*.md`、運用は `docs/admin/*.md`

## 残り

- R2 の台帳 63 件: 同じ名前が二つのモジュールにあるもの（`core.request` の `KINDS`・`REJECTED`、`core.obs` の `select`・`ORIGINS` など）と、core の中で循環を作るため窓口に寄せていないもの（`core.config` ほか）
- R3 の台帳 8 か所（ui の小さな部品同士の参照: analysis_dock・colorize_box・curve_editor・list_views・notice_feed・shift）
- 撮像計画（1,906 行）・画像カタログ（1,662 行）のドックの分割、機能ごとの小さな組み立てでのテスト（段階 8）
