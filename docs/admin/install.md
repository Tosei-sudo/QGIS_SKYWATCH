# 導入と設定

導入（zip・社内リポジトリ）、設定の画面、接続と表の点検。（[README](../../README.md) から機能ごとに分けた説明。）

## 導入

1. `python3 scripts/build_zip.py` で `dist/mikage-<版>.zip` を作る
2. QGIS の「プラグイン」→「プラグインの管理とインストール」→「ZIP からインストール」（閉域で多くの PC に配り、更新も知らせるなら下の「社内のプラグインリポジトリで更新を配る」）
3. メニュー「プラグイン」→「MIKAGE」→「設定…」で設定ファイル（パスか URL）を指定し「読み込み」。問題があれば一覧に出る。設定ファイルの指定はこのメニューだけで、各ドックには出さない。読み込んだ設定ファイルに問題があると、QGIS のメッセージバーに「問題 n 件」と「設定…」ボタンを出す（同じファイル・同じ問題では繰り返さない）

### Windows 版（onnxruntime・onnxruntime-genai・sgp4・tzdata を同梱）

```
python3 scripts/build_zip.py --win64
```

- `dist/mikage-<版>-win64.zip` を作る。中の `mikage/ext/win64/` に ONNX Runtime（AI の道具用、QGIS の Python 3.12 向け）と ONNX Runtime GenAI（[文章の手伝い](../features/observations.md#文章の手伝い小さな生成-aitextassist)の生成 AI 用、その ONNX Runtime の上で動く）と `sgp4`（C++ 版、軌道計算が約 15 倍速い）と `tzdata`（タイムゾーンのデータベース、設定ファイルの `timeZone` 用）が入り、QGIS 側に入っていなければこちらを使う。pip を入れられない閉域の Windows でもそのまま動く
- 4 つの wheel は pip で取得して `dist/wheels` に置く（2 回目からは再利用）。閉域では `--onnxruntime-wheel onnxruntime-*-cp312-cp312-win_amd64.whl --onnxruntime-genai-wheel onnxruntime_genai-*-cp312-cp312-win_amd64.whl --sgp4-wheel sgp4-*-win_amd64.whl --tzdata-wheel tzdata-*-py2.py3-none-any.whl` で持ち込んだものを指定
- ONNX Runtime は推論の本体（`capi`）だけを入れ、変換・量子化の道具は除く（約 15 MB）。ONNX Runtime GenAI はモデルの変換の道具（`models`）を除く（zip で約 5 MB 増える）。ライセンス文書（`LICENSE`・`ThirdPartyNotices.txt`）は同梱。生成 AI のモデル（0.5〜1 GB）は zip に入れない（[文章の手伝い](../features/observations.md#文章の手伝い小さな生成-aitextassist)）
- ONNX Runtime のテレメトリー（公式版は 1.31 から Microsoft へ HTTPS で送る。既定でオン）は、プラグインが読み込む前に環境変数 `ORT_DISABLE_TELEMETRY=1` を設定し、読み込み後も `disable_telemetry_events()` で止める（同梱・QGIS 側に入れたもののどちらでも）。管理者が `ORT_DISABLE_TELEMETRY` を設定済みならその値のまま
- `--config` と併用可
- DLL を含む onnxruntime・onnxruntime-genai・sgp4 は、プラグインのフォルダーからではなく `%LOCALAPPDATA%\mikage\win64\<名前>-<内容のハッシュ>` に写したものを読み込む。Windows は読み込み中の DLL を QGIS を閉じるまで掴み続けるため、プラグインのフォルダーから読むと「ZIP からインストール」での更新（Failed to unzip the plugin package）やアンインストール（Failed to remove the directory）が失敗するため。写しは中身が変わったときだけ作り直し、使われていない古い写しは消す
- 写しが作れないときも、プラグインのフォルダーから直接は読まない。写した直後にウイルス対策ソフトが DLL を調べていてフォルダー名の変更を拒むときは数秒やり直し、それでもだめなら写し終えた作業用のフォルダー（同じ場所の `.<名前>-…`）をそのまま使う。`%LOCALAPPDATA%` に書けない（権限・空き容量）ときは一時フォルダー（`%TEMP%\mikage\win64`）に写す。どこにも写せなければ読み込まず、AI の道具・文章の手伝いは理由を出して使えず、軌道計算は同梱の純 Python 版で動く（2026-11-21。直接読むと更新・アンインストールが QGIS を閉じるまで失敗するため）
- どこから読み込んだか（または写せなかった理由）は QGIS のログの「MIKAGE」タブに 1 行ずつ出る。更新が失敗したらまずここを見る
- `tzdata`（純 Python）はプラグインのフォルダーから読むが、それだけを読み込み、`ext\win64` のフォルダーを Python の読み込み先には入れない。入れると、隣の onnxruntime・onnxruntime-genai もそこから（DLL ごと）読み込まれるため（2026-11-24 に直した不具合: 設定ファイルに `timeZone` があると、そのあと生成 AI を使ったときにプラグインのフォルダーの DLL を掴み、更新・アンインストールが QGIS を閉じるまで失敗していた）
- この修正より前の版で生成 AI（または AI の道具）を使った QGIS では、DLL がもう掴まれている。一度 QGIS を閉じて開き直し、AI を使う前に更新する（生成 AI を使っていなければそのまま更新・アンインストールできる）
- プラグインを止めるとき（QGIS は更新・アンインストールの直前に止める）、読み込んだ AI のモデル（検出・クリック抽出・文章の手伝い）を手放す。モデルの重みが別ファイル（`model.onnx.data`）のものは、読み込み中ずっとそのファイルを掴むため
- この修正より前の Windows 版から更新するときは一度だけ、プラグインを無効にして QGIS を再起動してから ZIP を入れる（または QGIS を閉じて `python/plugins/mikage` を消す）

### 設定ファイルを同梱して配る

画像ビューアの `config.json`（`public/config.json` など）をそのまま zip に入れて配れる。

```
python3 scripts/build_zip.py --config /path/to/image-viewer/public/config.json
```

- zip の中では `mikage/config.json`（`.jsonc` ならそのまま）になる。`mikage/` に `config.json` を置いて `--config` なしで作っても同じ（この場所は git の対象外）
- 受け取った人が「設定…」で設定ファイルを指定していなければ、同梱のものを読む。指定していればそちらが優先。欄を空にすると同梱のものに戻る。「設定…」の表示は「同梱の設定ファイルを使用」
- 作るときに設定ファイルを読んで確かめ、読めない・問題があるときは zip を作らない。`token` が書いてあるのも問題（サインインは設定ファイルの [`signIn`](config-file.md#サインインsignin) で決めるので、zip に秘密は入らない）
- プラグインを入れ直すと同梱の設定ファイルも新しい zip のものに置き換わる。各自の指定（`QgsSettings`）は残る

#### 同梱の設定ファイルに固定する（`--lock-config`）

```
python3 scripts/build_zip.py --config /path/to/config.json --lock-config
python3 scripts/build_zip.py --config /path/to/config.json --lock-config --win64
```

- 受け取った人は同梱の設定ファイルしか使えない。「設定…」の設定ファイル欄は同梱のファイルを表示するだけで変えられず（「参照…」は押せない、「読み込み」は同梱のものを読み直す）、前に指定したパス・URL があっても無視する。「設定…」の表示は「同梱の設定ファイルに固定」
- 記録者とサービスごとの認証設定はこれまでどおり各自で選べる
- 仕組み: zip に設定ファイルと並べて `mikage/config.lock` を入れる。プラグインは `config.lock` と `config.json`（または `config.jsonc`）が両方あるときだけ固定する（`config.lock` だけなら何もしない）。`mikage/` に `config.json` と `config.lock` を置いて `--config` なしで作っても固定になる（どちらも git の対象外）
- 設定ファイルなしで `--lock-config` を付けると zip を作らない
- 固定を外すには `--lock-config` なしで作った zip を入れ直す（またはプラグインのフォルダの `config.lock` を消す）。各自が前に指定していたファイルがあれば、またそちらを読む

### MIKAGE 練習版（練習モードだけの別のプラグイン、`--training-only`）

研修用の PC など、本番を使わせずに練習だけをさせたいところに入れる版（2026-11-26）。コードは MIKAGE と同じで、作るときの指定だけで別のプラグインになる。

```
python3 scripts/build_zip.py --training-only
python3 scripts/build_zip.py --training-only --win64 --config /path/to/config.json
```

- `dist/mikage_training-<版>[-win64].zip` を作る。zip の中のフォルダー（QGIS のプラグイン ID）は `mikage_training`、名前は「MIKAGE 練習版」、アイコンは MIKAGE のものに橙色の「練」の印。目印のファイル `mikage_training/training.only` があると練習版として動く（`mikage/core/edition.py`）。本番の仕事の `scheduled_follow.py`（定期照合）は入れない
- 動き: QGIS を起動すると確認なしで練習モードに入り（初回だけ練習用の画像を作る）、終えられない（「練習モードを始める」「練習モードを終える」・帯の「練習モードを終える」なし）。練習モードに入る前も、ドックに本番の設定ファイルを渡さない。開いているプロジェクトはそのまま（新しいプロジェクトにしない）。監査ログは書かない。「判読の練習の問題を作る…」「判読の練習の成績…」は出さない（指導者は本番の MIKAGE で使う）。「設定…」は練習モードと同じく使えない（「最初の設定」の「設定ファイルの指定・接続の認証…」も出さない）
- 設定ファイル: 同梱しなければ内蔵の見本（横須賀）だけで練習し、ネットワークには出ない。同梱すれば、練習モードが本番の設定ファイルから読むもの（`training` の設定、`training.fromProduction` の台帳、`mode` が `training`・`both` の画像カタログ、`training.exercises` の組織の問題と成績の書き込み）だけを使い、ほかの表（本番の依頼・記録・カタログなど）は読まない。各自が「設定…」で指定した設定ファイルは読まない（同梱のものだけ）。そのため `--lock-config` は付けない（付けると zip を作らない）。本番の表を読むのにサインインが要る場合は、設定ファイルの `token` を使う（各自の QGIS の認証設定は「設定…」で選ぶため、練習版では選べない）
- 個人設定（`QgsSettings`）は `mikage_training/…`、プロファイルの中のフォルダーは `mikage_training/`（練習データは `mikage_training/training`）で、MIKAGE の `mikage/…` とは混ざらない。担当は練習版の中で選んだものがそのまま残る（MIKAGE の練習モードのように終えると戻す、ということはない）
- MIKAGE と同じ QGIS（同じプロファイル）に両方入れられる。ただし同時には動かさない（Processing の ID `mikage`、式の関数、ショートカット、ドックの名前が同じため）。両方を有効にすると、あとから読み込まれた方がメッセージバーに「…が有効なため、…は動かさない」と出して何もしない。両方使う人（指導者など）は MIKAGE の練習モードで足りる
- ソースのまま試すときは `mikage/` に空のファイル `training.only` を置く（git の対象外。MIKAGE の zip には入れない）

### 社内のプラグインリポジトリで更新を配る（閉域）

公式のプラグインリポジトリ（plugins.qgis.org）に届かない閉域でも、社内の共有フォルダーか Web サーバーに「社内リポジトリ」を置けば、各 PC の QGIS が普通のプラグインと同じく更新を知らせ、「プラグインの管理」のボタン 1 つで入れ替えられる。

**作る（リリースのたび）**

```
python3 scripts/make_plugin_repo.py \\fileserver\share\qgis-plugins
python3 scripts/make_plugin_repo.py /srv/www/qgis-plugins --url https://intra.example/qgis-plugins
python3 scripts/make_plugin_repo.py \\fileserver\share\qgis-plugins --config config.json --lock-config --changelog "通報の地名を追加"
python3 scripts/make_plugin_repo.py \\fileserver\share\qgis-plugins --no-training      # MIKAGE だけ
python3 scripts/make_plugin_repo.py \\fileserver\share\qgis-plugins --training-only    # MIKAGE 練習版だけ
```

- MIKAGE と [MIKAGE 練習版](#mikage-練習版練習モードだけの別のプラグイン--training-only) のそれぞれ通常版と Windows 版の zip を新しい版で作り、フォルダーに置く。最後に各 PC で登録するアドレスを出す

| ファイル | 中身 |
|---|---|
| `plugins.xml` | 通常版の一覧。MIKAGE と MIKAGE 練習版の両方が載り、各 PC の「プラグインの管理」でどちらを入れるか選ぶ |
| `plugins-win64.xml` | Windows 版の一覧（同じく両方） |
| `plugins-training.xml`・`plugins-training-win64.xml` | MIKAGE 練習版だけの一覧。研修用の PC にはこちらを登録すると、MIKAGE（本番）は一覧に出ない |
| `mikage.<版>[.win64].zip`・`mikage_training.<版>[.win64].zip` | それぞれの zip |
| `icon.svg`・`icon-training.svg` | 一覧に出すアイコン |

- 片方だけ（`--no-training`・`--training-only`、`--zip` で作ってある片方だけ）を載せても、一覧にあるもう片方はそのまま残る
- 版は `metadata.txt` の版の後ろに UTC の日時（秒まで）を付けたもの（例 `0.4.0.20261108123000`）。QGIS は掲載の版が入っている版より大きいときだけ更新と見なすため、作るたびに必ず大きくなるようにしている。`--version` で指定もできる。大きな変更では `metadata.txt` の版（`0.4.0` → `0.5.0`）を上げる
- 掲載中の版より新しくない zip は載せない。前の版に戻すときは、残っている古い zip を `--zip 古い.zip --allow-older` で載せ直す（各 PC では「プラグインの管理」で入れ直し）。古い zip は消さずに残す
- `--config`・`--lock-config`（同梱の設定ファイルに固定）は `build_zip.py` と同じで、通常版・Windows 版の両方に効く。`--config` は MIKAGE 練習版にも同梱する（練習版は固定しなくても同梱のものだけを読むので `--lock-config` は MIKAGE だけに効く）。`--zip a.zip a-win64.zip` で作ってある zip を載せる（中の版のまま、MIKAGE か練習版か・Windows 版かどうかは中身で判断）。`--no-win64`・`--no-regular` で片方だけ作る
- 閉域では Windows 版の wheel（onnxruntime・onnxruntime-genai・sgp4・tzdata）を pip で取れないので、外で `pip download onnxruntime onnxruntime-genai sgp4 tzdata --only-binary=:all: --platform win_amd64 --python-version 3.12 --no-deps -d wheels` で取ったものを持ち込み、`dist/wheels/` に置く（そのまま使われる）か `--onnxruntime-wheel`・`--onnxruntime-genai-wheel`・`--sgp4-wheel`・`--tzdata-wheel` で指定する。持ち込み直すのはライブラリの版を上げるときだけ
- zip を先に置いてから `plugins.xml` を差し替えるので、作っている途中に各 PC が読んでも、ない zip を指すことはない
- `--url` を付けないと、ダウンロード先はフォルダーの `file://` アドレス（`\\fileserver\share\qgis-plugins` なら `file://fileserver/share/qgis-plugins`）。Linux でマウントした共有フォルダーに作るときは、各 PC から見えるアドレスを `--url file://fileserver/share/qgis-plugins` のように付ける
- 掲載は「実験的でない」扱いにする（`metadata.txt` は `experimental=True` だが、そのままだと各 PC で「実験的なプラグインも表示する」を入れないと見えないため）。`--experimental` で実験版として載せることもできる

**置き場所**

- 共有フォルダー: 各 PC から読めればよい（書けるのはリリースする人だけにする）。サーバーは要らない
- Web サーバー: IIS・Apache・nginx など、ファイルをそのまま返すもの（`.xml`・`.zip` の MIME 型が要る。IIS の既定で可）。サインインが要るなら、各 PC で QGIS の認証設定をリポジトリに付ける

**各 PC で登録する（最初の 1 回）**

1. 「プラグイン」→「プラグインの管理とインストール」→「設定」→「追加…」
2. 名前（例「社内」）と URL を入れる。Windows 版を使う PC は `…/plugins-win64.xml`、それ以外は `…/plugins.xml`（研修用で MIKAGE 練習版だけを入れさせる PC は `…/plugins-training-win64.xml`・`…/plugins-training.xml`）。共有フォルダーなら `file://fileserver/share/qgis-plugins/plugins-win64.xml`、Web サーバーなら `https://intra.example/qgis-plugins/plugins-win64.xml`（http:// は経路で zip を差し替えられるため避ける）
3. 公式のリポジトリ（QGIS Official Plugin Repository）は届かないので、チェックを外して無効にしておくとエラーが出ない
4. 同じ画面の「起動時に更新を確認する」を入れておくと、QGIS は 3 日おきに起動時に確かめ、新しい版があれば知らせる。すぐ確かめるときは「すべてのリポジトリを再読み込み」
5. 「すべて」の一覧で「MIKAGE」（練習だけの PC は「MIKAGE 練習版」）を選び「プラグインをインストール」（入っていれば「プラグインをアップグレード」）

QGIS の設定（`QgsSettings`）の `app/plugin_repositories/<名前>/url` に入るので、多くの PC に配るときは QGIS の `qgis_global_settings.ini` の `[app]` に `plugin_repositories\internal\url=file://fileserver/share/qgis-plugins/plugins-win64.xml` と書いておくこともできる（公式のリポジトリは消しても QGIS が足し直すので、`plugin_repositories\QGIS%20Official%20Plugin%20Repository\enabled=false` のように無効にする）。

**更新のとき（Windows 版）**

- 更新は QGIS の中でプラグインのフォルダーを消して zip を展開し直す。Windows 版の DLL（onnxruntime・sgp4）は `%LOCALAPPDATA%` の写しから読むので、QGIS を開いたままでも更新できる（上の「Windows 版」）
- この仕組みより前の Windows 版が入っている PC は一度だけ、プラグインを無効にして QGIS を再起動してから更新する（または QGIS を閉じて `python/plugins/mikage` を消してから入れる）。今の版の `metadata.txt` は `0.4.0` 以上なので、`0.3.0` などの手元の版から入れた PC にも更新として出る
- 更新後に動きがおかしければ QGIS を再起動する

**確かめたこと**: QGIS 3.34 のプラグイン管理のコード（`pyplugin_installer`）で、`file://` と `http://` のどちらのリポジトリも読めて、`0.4.0` が入った状態で「アップグレード可能」になり、ダウンロードと展開で新しい版に入れ替わることを確かめた（Linux）。UNC の `file://サーバー/共有/…` は Qt が Windows で UNC パスとして開く書き方。

## 設定

| 項目 | 保存先 |
| --- | --- |
| 設定ファイルのパス・URL、記録者、サービスごとの認証設定 | `QgsSettings`（`mikage/…`） |
| 同梱の設定ファイル（指定がないときに読む） | プラグインのフォルダーの `config.json`（または `config.jsonc`） |
| 同梱の設定ファイルへの固定（`--lock-config`） | プラグインのフォルダーの `config.lock` |
| 観測記録・確認範囲・分類体系・写真・新着画像の監視と通知（ローカル） | ユーザープロファイルの `mikage/observations.gpkg` |
| URL で指定した AI のモデル | ユーザープロファイルの `ai_models/` |
| `pathMappings` で選んだフォルダ、類似画像の許容差、カーソル位置の表記、担当とドックの開き方（最初の設定）、新規スクラッチレイヤーの保存済みテンプレート、Web マップのポータル | `QgsSettings` |
| 入力の初期値（前回の入力） | `QgsSettings`（`mikage/last_inputs/…`） |
| 定期照合（`scheduled_follow.py`）のログ・ロック・監査ログの控え | `%LOCALAPPDATA%\MIKAGE\scheduled_follow`（`--folder` で変更） |
| 送信待ち（オフライン作業の変更・保存できなかった観測記録）とサーバーの控え | ユーザープロファイルの `mikage/offline_work.sqlite` |
| 地名・国のポリゴンの控え（通報・エクスポート・式の関数で使った場所の分） | ユーザープロファイルの `mikage/place_cache.sqlite` |
| 監査ログ（日ごとのファイルと、送り先に送った位置 `sent.json`） | ユーザープロファイルの `mikage/audit/`（`auditLog.localFolder` で変更） |
| 分類体系の取り込み（表から）の前の分類体系の控え（JSON、新しい 20 個） | ユーザープロファイルの `mikage/taxonomy-backups/` |

設定ファイルはビューアの `config.json` をそのまま読む（コメント付き JSON も可、ほかの項目は無視）。読む項目と書き方は[設定ファイルの書き方](config-file.md#設定ファイルの書き方)。

認証: サーバーのドメインごとに、設定ファイルの [`signIn`](config-file.md#サインインsignin) で統合 Windows 認証（IWA）・SAML・サインインなしを決める。`signIn` にないサーバーは、各自が「設定…」の表でサービスごとに QGIS の認証設定（OAuth2・ESRI token・Basic など）を選ぶ。設定ファイルに `token` は書かない（書いてあれば問題の一覧に出して無視）。トークンや認証情報はログ・CSV・レイヤー・プロジェクトファイルに残さない（IWA のトークンは、地図のレイヤー用に QGIS の認証設定へ暗号化して置き、プラグインを止めると消す）。

## 接続と表の点検

メニュー「接続と表の点検…」: 設定ファイルにある ArcGIS のサービス（レイヤー・テーブル、背景地図・サーバ処理などのサービス）に 1 回ずつ問い合わせ、作業が止まる前に問題を見つける。書き込みはしない（レイヤーの説明 `?f=json` を読むだけ）。

- 見ること: 応答があるか、サインインが通るか（設定ファイルの `signIn`・「設定…」の認証設定。一番上の「サインイン（signIn）」の行に、決まりごとにサインインできたかと利用者）、設定の `fields` の属性がレイヤーにあるか（大文字・小文字は区別しない）、プラグインが書き込むレイヤー（観測記録・確認範囲・分類テーブル・撮像計画・撮像依頼と出力履歴・分担・引き継ぎメモ・通報記録・イベント・検出結果の転記先）が編集できるか、書き込むレイヤーに作成者・更新者の属性（[`editTracking`](operations.md#作成者更新者の記録edittracking)）があるか
- 結果は「問題」（その表が使えない: 応答なし・サインインできない・ID などの欠かせない属性がない・編集できない）、「注意」（一部の機能だけ働かない: そのほかの属性がない・追加のみ・作成者・更新者の属性がない）、「OK」。問題・注意の順に並ぶ
- 「結果をコピー」で、設定ファイルの場所・日時・各サービスの URL と結果を文章でクリップボードへ（管理者に送る、問い合わせに貼る）。「もう一度点検」で読み直す
- 設定ファイルに `baseMaps` がないときの既定の背景地図（インターネットのもの）は点検しない
