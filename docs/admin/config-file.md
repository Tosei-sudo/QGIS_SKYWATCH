# 設定ファイルの書き方

設定ファイル（config.json）の項目の一覧と書き方。（[README](../../README.md) から機能ごとに分けた説明。）

## 設定ファイルの書き方

プラグインが読む設定ファイル（`config.json`）の書き方。ビューアの `config.json` と共用できるが、プラグインだけの項目・読み方の違いが増えたので、**プラグインが実際に読む項目**をここにまとめる（`mikage/core/config.py` の `KEYS` と各項目の読み込み処理が元）。各機能の細かい動きは、表の「詳しく」の節を参照。Esri 側に作るテーブル・属性の一覧は [Esri 側のテーブル定義](esri-tables.md#esri-側のテーブル定義)。

### 設定ファイル作成ツール（`tools/config_builder.html`）

ブラウザで開くだけで使える設定ファイルの作成・編集ツール（サーバー・ネットワーク・インストール不要、閉じたネットワークでも動く）。

- 左の項目を選んで入力すると、右に `config.json` ができていく。「config.json を保存」で書き出し、プラグインのメニュー「設定…」で読み込む
- 「開く…」で既存の設定ファイル（JSONC も可）を読み込んで直せる。保存するとコメントは消える。プラグインが読まない項目（ビューアだけの項目）はそのまま残す
- 必須の項目・属性名の形・数の範囲・重複などを「確認」に出す（最後はプラグインの「設定…」の問題の一覧で確かめる）
- 撮像依頼の日次出力（`requestExports`、オーダー用ファイル）: CSV の列（`columns`）を表で、JSON の JSON Schema（`x-source` など）を木で組める。システムから受け取ったスキーマを読み込んで `x-source` を書き添え、`.schema.json` として保存もできる。架空の依頼 2 件での出力の見本と、スキーマでの検査結果を出す
  - 項目ごとに「既定値」（`default`。値が空のときに書く。必須の項目に）と固定値（`const`）を指定できる。配列は JSON で書く（`[]`・`[""]`・`["On"]`）。型が `string` の固定値・既定値は書いたとおりの文字（`001` も `001`）
  - 管理番号・識別番号（`managementNo`・`identificationNo`）、衛星シリーズ配下の衛星の配列（`seriesSatellites`）、範囲の中央の GeoJSON（`centerGeojson`）、軌道から出した撮像（`passTime`・`passOffNadir`・`passIncidence`・`passDirection`・`passLookSide`）、まとめた依頼（`mergedIds`・`mergedNames`・`mergedCount`）、1 日に何回も撮る依頼の何回目か（`occurrence`・回つきの ID `occurrenceId`・`timesMin`・`timesMax`）、出力の行の外部連携ID（`linkId`）、広い範囲を分担したショットの番号・数・割合（`shareNo`・`shareCount`・`shareCoverage`）とその回で撮れる範囲の割合（`coverage`）。数値の書式（`%03d`・`%.1f` など）の書き方は [docs/imaging-requests.md](../imaging-requests.md) の「8.6 数値の書式」
- 入力途中の内容はブラウザに残り、次に開いたとき続きから
- 項目の役割・選択肢はプラグインの定数の写し。プラグインを変えたら `python3 -m mikage.tests.test_config_builder` で更新する（テストが食い違いを見つける）

### 衛星・センサー編集ツール（`tools/satellite_editor.html`）

衛星カタログ（`satelliteCatalogs`）・センサーモード（`sensorCatalogs`）・衛星名の対応表（`satelliteAliases`）・年間撮像上限（`requestStores[].quotas`）の Esri のテーブルを、衛星ごとにまとめて直すツール。ブラウザで開くだけで使える（インストール不要、閉じたネットワークでも動く）。書き込みはすぐテーブルに入り、プラグインは次に読んだとき（撮像計画の「読み直し」・QGIS の再起動）から使う。

- **使い方**: 「設定ファイルを開く…」でプラグインの設定ファイル（JSONC も可）を選ぶ（画面に落としてもよい）。どのテーブルのどの属性を使うかは設定ファイルの `url`・`fields`・`where` のとおり。次からは「前回の設定ファイルで開く」でも開ける（サインインはブラウザに控えない）
- **衛星の一覧**（左）: 衛星名・ID・シリーズ・センサー・別名で絞り込み。モード・別名の数、TLE がない・不正・14 日以上古い、`where` に合わない（プラグインが使わない）行を印で出す
- **衛星**: 役割ごとの入力欄（属性名・単位・空のときの既定つき）と、`fields` にない属性（「その他の属性」）。TLE は 2 行（名前の行つき 3 行も可）を貼り付ければ `tle1`・`tle2`（か `tle`）に分け、空の衛星名・ID に名前の行・衛星番号を入れる。チェックサム・文字数・衛星番号を確かめ、元期・傾斜角・周期を出す。「複製して追加…」で名前・ID・TLE のほかを写した新しい衛星。新しい行には `where` の「属性 = 値」を最初から入れる（プラグインが使う行になるように）
- **センサー（撮像モード）**: その衛星が使うセンサーのモードの一覧と追加・編集・削除。どのセンサーを使うかはプラグインと同じ決まり（衛星の `sensor`、なければシリーズ、なければ衛星名と同じ名前のセンサー）。シリーズで共有するセンサーは「ほかに n 機が使う」と出す。モード名・既定モード（ほかのモードの既定の印は外す）など。センサー名を変えると、同じセンサーのほかのモードと、そのセンサーを使う衛星の `sensor` もまとめて変えるか聞く
- **別名**: その衛星（衛星名か ID）の対応表の行の追加・編集・削除。ほかの衛星の名前・別名と重なるとき・衛星自身の名前のときは注意を出す。衛星名・ID を変えると、対応表の衛星名もいっしょに変えるか聞く
- **年間撮像上限**（設定ファイルの `requestStores[].quotas` があるとき）: その衛星の撮像を数える上限の行（センサー名、センサーがなければ衛星名・ID で一致。モードが空なら全モード）の一覧と追加・編集・削除。年の始まりの月（1〜12）・当初の消費枚数と数え始める日（当初の消費枚数を入れると、空なら今日を入れる。日は日本時間）。どの衛星にも合わないセンサー・その衛星にないモード・同じセンサーとモードの 2 行目は注意を出す（2 行目は保存しない）。センサー名・衛星名を変えると、上限の行のセンサーもいっしょに変えるか聞く。消費・残り・数えない画像はプラグインの「年間撮像上限…」で見る
- **削除**: 衛星を消すと、その衛星だけが使うセンサーモードと別名もいっしょに消す（年間撮像上限の行はセンサーのものなので残す）（確かめる画面に一覧）。ほかの衛星も使うセンサー（シリーズで共有など）・同じ名前のほかの行が使う別名は残す。設定ファイルに書いたモード（`sensors`）はここでは消さない
- **つながりのない行**: どの衛星にも使われないセンサーモードと、カタログにない衛星名の別名、どの衛星にも合わない年間撮像上限（消した衛星の残り・打ち間違い）を一覧にして直す・消す
- **作成者・更新者**: 右上の「作業者」の名前を `editTracking` の属性（既定 `CREATED_USER`・`UPDATED_USER`、日時も）に入れる。プラグインは Windows のユーザー名（環境変数 `USERNAME`）を入れるので、同じ名前を入れる（`editTracking.user` があればその名前）。テーブルにない属性は書かない。`timeZone`・`timeZones` の決まりがあるテーブルの日時はプラグインと同じに換算する
- **ほかの人の変更**: 保存の前に行を読み直し、読み込んでからほかの人が変えていれば、変わった属性を出して「上書きする」「最新を読み直す」を選ぶ
- **サインイン**: 設定ファイルの [`signIn`](#サインインsignin) が `iwa` のサーバーは、ポータルの generateToken をブラウザの Windows のサインインで呼んで、聞かずにトークンを取る（ポータルの CORS で、このページの置き場所から資格情報つきの要求を許していること。`file://` で開いたページの送り元は `null` なので、許されないときは社内の Web サーバーに置く）。取れないときと `iwa` 以外のサーバーは、トークンを求められたときに出る画面で、ユーザー名とパスワード（サーバー・ポータルの generateToken）・トークンの貼り付け（`saml` のときの既定）・ブラウザの資格情報。トークンとパスワードは控えない（ページを閉じれば消える）
- **サーバーの準備**: テーブルのサービスで編集（作成・更新・削除）を許す。ブラウザから直接 ArcGIS REST（`query`・`applyEdits`）を呼ぶので、サーバーの CORS（許可するオリジン。既定はすべて）でこのページからの要求を断っていないこと。`tools/arcgis/create_tables.py` はこの 3 つのテーブルにも作成者・更新者の属性を足し、`--grant-edit` で編集の権限を付ける
- 役割の一覧はプラグインの定数の写し。プラグインを変えたら `python3 -m mikage.tests.test_satellite_editor` で更新する（テストが食い違いを見つける）

### 読み込みと基本の決まり

- 指定: メニュー「プラグイン」→「MIKAGE」→「設定…」で、設定ファイルのパスか URL を選んで「読み込み」。問題があれば一覧に出る。指定がなければプラグインのフォルダーに同梱の `config.json`（または `config.jsonc`）を読む（[設定ファイルを同梱して配る](install.md#設定ファイルを同梱して配る)、固定するなら [`--lock-config`](install.md#同梱の設定ファイルに固定する--lock-config)）
- 形式: UTF-8 の JSON。コメント（`//`・`/* */`）と末尾のカンマも可（JSONC）。いちばん外はオブジェクト
- URL の設定ファイルを取得できなかったとき（一時的な通信障害など）は、最後に読めた内容（プロファイルに控え）で動き、「前回読めた設定で動作中」と問題の一覧に出す。控えがなければ既定の設定。「読み込み」を続けて押したときは最後の 1 回の結果だけを使う
- 知らない項目は無視する（ビューアだけの項目があってもよい）。項目の値が誤っていればその項目（配列なら誤った要素だけ）を既定に戻し、理由を問題の一覧に出す。設定ファイルが壊れていてもプラグインは起動する
- 相対パス・相対 URL（`./`・`../`）は設定ファイルの場所が基準（`layers`・`baseMaps`・`movingFeeds`・`aiModels` など）
- `label`（画面に出す名前）は省略可。省略すると「カタログ 1」のような名前

多くのサービスの項目に共通する書き方:

| 項目 | 内容 |
| --- | --- |
| `label` | 一覧・メニューに出す名前 |
| `url` | ArcGIS のレイヤーかテーブルの URL（`…/FeatureServer/0` など） |
| `fields` | `{ 役割: 属性名 }`。属性名は英数字と `_`。書かない役割は既定の属性名（既定のある項目）か、使わない |
| `where` | 対象を絞る SQL の条件（任意） |
| `timeZone`・`serviceTimeZone` | そのサービスの日時の本当のタイムゾーン（[日時のタイムゾーン](operations.md#日時のタイムゾーンtimezonetimezones)） |

サーバーへのサインインはサービスの項目には書かない（`token` は使わない。書いてあると問題の一覧に出して無視する）。サーバーのドメインごとに [`signIn`](#サインインsignin) で決める。

### サインイン（`signIn`）

サーバーへのサインインの仕方を、サーバーのドメインごとに決める（2026-11-26。設定ファイルの `token` はこのとき廃止）。

```jsonc
"signIn": [
  // AD 認証のポータル（IIS の Windows 認証、Web 層の認証）。連携したサーバーとホストされたサービスにも、ポータルのトークンで入る
  { "method": "iwa", "portal": "https://gis.example.co.jp/portal", "domains": ["*.gis.example.co.jp"], "label": "社内ポータル" },
  // SAML・OIDC のポータル（ポータル層の認証）。ポータルに登録したアプリのクライアント ID
  { "method": "saml", "portal": "https://map.example.com/portal", "clientId": "AbC123" },
  // サインインしないサーバー
  { "method": "none", "domains": ["open.example.org"] }
]
```

| キー | 内容 |
| --- | --- |
| `method` | `iwa`（統合 Windows 認証）・`saml`（SAML・OIDC を OAuth2 で）・`none`（サインインなし）。必須 |
| `portal` | ポータルの URL（`https://…/portal` など）。`iwa`・`saml` で必須。そのホストは書かなくても `domains` に入る |
| `domains` | この決まりを使うサーバーのホスト名の配列。`gis.example.co.jp`（そのホストだけ）か `*.example.co.jp`（その下のホストすべて。`example.co.jp` そのものは含まない）。`none` は必須 |
| `clientId` | `saml` だけ（必須）。ポータルに登録したアプリのクライアント ID |
| `label` | 「設定…」・点検に出す名前（既定は最初のドメイン） |

- **どれを使うか**: 要求ごとに URL のホストで決める。ホスト名そのものが `*.` より、長い `*.` が短い `*.` より優先。同じドメインを 2 つの決まりに書いたら前のものを使う（問題の一覧に出す）
- **`iwa`**: ポータルの generateToken を、PC の Windows のサインイン（Kerberos・NTLM）で呼んでトークンを取り、そのドメインのサービスへの要求に付ける。ポータル自身のページ（`…/portal/sharing/rest/…`）には Windows のサインインで入る。地図に足したレイヤー（QGIS が読む）には、プラグインが作る QGIS の認証設定「MIKAGE: label（統合 Windows 認証）」（API ヘッダー）でトークンを付ける（トークンが変わるたびに書き直し、プラグインを止めるとトークンを消す。作るときに QGIS のマスターパスワードが要る。作れなくてもプラグイン自身の読み書きは動く）。トークンはプロジェクト・レイヤーのソースには入れない。切れる少し前と、サーバーに断られたとき（498・499）に取り直す。Windows でだけ使える（ほかの OS では [E204](error-codes.md)）。ポータルの Web Adaptor で Windows 認証を有効にし、ポータルの利用者が AD のアカウントであること
- **`saml`**: ポータルの OAuth2 を使う QGIS の認証設定（「MIKAGE: label」）をプラグインが作り、そのドメインのサービスに使う。初回はブラウザーが開き、いつもの SAML のサインイン。ポータルにアプリを登録し（コンテンツ → 新しいアイテム → アプリケーション → その他のアプリケーション → 登録）、リダイレクト URI に `http://127.0.0.1:7070/` を入れて、クライアント ID を `clientId` に書く
- **`none`**: 何も付けない（ドメインが `iwa`・`saml` の `*.` に入るが、サインインしないサーバー）
- **`signIn` にないサーバー**: 各自が「設定…」の表でサービスごとに選んだ QGIS の認証設定を使う（`signIn` のドメインのサービスはその表で「設定ファイル（…）」と出て、選べない）
- **確かめる**: 「設定…」の「サインイン」の行に決まりごとの状態とサインインした利用者（`iwa` はポータルの `community/self`）。「確認し直す」でトークンを取り直す。「接続と表の点検」の一番上にも出る。サインインできないときのエラーコードは [E201〜E204](error-codes.md)
- 担当（`userRoles`）・記録者・作成者と更新者（`editTracking`）の利用者名は、これまでどおり Windows のユーザー名（`USERNAME`）
- 撮像の予定の自動追跡（`tools/follow_requests.py`、Windows のタスク スケジューラー）は `iwa` を使える。`saml` は使えない（`--user`・`--credential` のトークンなら使える）
- 練習モード: 本番の設定ファイルから何かを読むとき（`fromProduction`・本番の画像カタログ）は、本番の `signIn` も使う

### 最小の例

```jsonc
{
  // 画像カタログ（必須なのは url・fields.source）
  "imageCatalogs": [
    { "label": "衛星画像", "url": "https://gis.example/arcgis/rest/services/Catalog/FeatureServer/0",
      "fields": { "id": "IMG_ID", "acquired": "ACQ_TIME", "sensor": "SENSOR", "source": "PATH" } }
  ],
  // 衛星（TLE）。撮像計画に必要
  "satelliteCatalogs": [
    { "label": "衛星", "url": "https://gis.example/arcgis/rest/services/Sat/FeatureServer/0",
      "fields": { "name": "NAME", "tle1": "TLE1", "tle2": "TLE2", "swath": "SWATH_KM" } }
  ],
  // 観測記録を Esri に保存（書かなければローカルの GeoPackage だけ）
  "observationStores": [
    { "label": "共有の観測記録", "url": "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0" }
  ]
}
```

Esri のサービスにつなぐ項目（画像カタログ・衛星・観測記録・撮像依頼・通報・分担・イベントなど）がそろった例は、モックサーバーが書き出す `scripts/mock_esri/data/config.mock.jsonc`（`python3 scripts/mock_esri/mock_esri.py config`、[Esri なしで試す](../../README.md#esri-なしで試すモックサーバー)）。撮像依頼の例は [docs/request-samples/config.sample.jsonc](../request-samples/config.sample.jsonc)。

### 項目の一覧

いずれも省略可。省略時は「既定」の列のとおり。

#### サインイン

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `signIn` | 配列 | サーバーのドメインごとのサインインの仕方。`method`（`iwa`・`saml`・`none`）・`portal`・`domains`・`clientId`（`saml`）・`label` | なし（各自が「設定…」の表で選ぶ QGIS の認証設定） | [サインイン](#サインインsignin) |

#### 画像カタログ・通知・画像

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `imageCatalogs` | 配列 | `url`（必須）、`imageFiles`（`false` で画像ファイルのないカタログ: 「開く」の代わりに「ファイルなしで記録」、`source` 不要。既定 `true`）、`mode`（出すモード。`production` 本番だけ（既定）・`training` 練習モードだけ（本番では判読練習の指導者にだけ「（練習用）」で出す）・`both` 両方）、`fields`（`source` 必須。ほかに `id`・`acquired`・`registered`・`systemRegistered`・`sensor`・`angle`・`azimuth`・`keywords`（フリーワード検索の属性）。`source`・`keywords` は配列で複数可）、`labels`（役割ごとの列見出し）、`columns`（追加の列。属性名か `{ "field", "label" }`）、`where`、`maxResults`（検索と「続きを読む」で 1 回に読む件数。既定 500、最大 5000）、`angleKind`（`offNadir`・`incidence`・`elevation`）、`orbitHeight`（km、既定 600）、`imagePatterns`、`thumbnail`（新着画像のカードのサムネイルのアドレスのパターン）、`footprintLayers`（カードに出す写っている地物のレイヤー）、`timeZone` | なし | [画像カタログ](../features/images.md#画像カタログ) |
| `imagePatterns`（`imageCatalogs` の中） | オブジェクト・文字列・配列 | `{ "センサー名": "正規表現" か [ … ], "*": … }`。フォルダのレコードから開く画像を選ぶ | ふつうの画像ファイル | [フォルダのレコードの画像](../features/images.md#フォルダのレコードの画像imagepatterns) |
| `thumbnail`（`imageCatalogs` の中） | 文字列かオブジェクト | サムネイルのアドレスのパターン（`{id}`・`{acquired:%Y%m%d}`・`{attr.属性名}`・`{bbox}`・`{ur.lat}` など）か `{ "url", "width", "height" }` | なし（フットプリントの形） | [サムネイル](../features/images.md#サムネイルimagecatalogsthumbnail) |
| `footprintLayers`（`imageCatalogs` の中） | 配列 | `{ "label", "url", "fields", "format", "where", "max", "background" }`。フットプリントと重なる地物の名前をカードに（1 件 1 行、`background` で行の背景色を属性から） | なし | [写っている地物](../features/images.md#写っている地物imagecatalogsfootprintlayers) |
| `watchInterval` | 数 | 新着画像を確かめる間隔（分、1〜1440） | 10 | [通知](../features/images.md#通知) |
| `pathMappings` | 配列 | `prefix`（レコードのパスの先頭）、`label`、`url`（同じフォルダの http(s)。なければ各自が PC のフォルダを選ぶ）。または `pattern`（正規表現）・`to`（読み替え先。`$1` などがグループ）で先に試す読み替え（なければ元のパス）。読み替えに `overviewsBeside: true` で、そこで見つかった画像のピラミッドを画像の隣に置く（共有フォルダ向け） | なし | [パスの読み替え](../features/images.md#パスの読み替えpathmappings) |
| `overviews` | `false` かオブジェクト | `enabled`、`resampling`（`AVERAGE` など）、`minSize`、`compress`（`DEFLATE` など）、`location`（`auto`: 書ければ画像の隣、`cache`: キャッシュのフォルダ、`temp`: 一時フォルダ。QGIS を閉じたら消す）、`scope`（`all`: QGIS に追加したローカル画像すべて、`catalog`: カタログなどから開いたものだけ）、`cacheFolder`（空ならプロファイルの中）、`tempFolder`（`temp` のときのフォルダ。空なら `%TEMP%\MIKAGE\overviews`。どちらのフォルダにも `%LOCALAPPDATA%` などの環境変数を書ける）、`threads`（作成に使うコア数。0 = 半分） | 作る（`auto`・`all`） | [ピラミッドの自動作成](../features/images.md#ピラミッドovrの自動作成overviews) |
| `rasterSpeed` | `false` かオブジェクト | `gdalThreads`（`"auto"` = コア数 − 1・数・`false`）、`gdalCacheMB`（`"auto"` = RAM の 10 %・最大 2048・数・`false`）、`qgisSettings`（QGIS のオプションの遅い値を 1 回だけ直す） | 使う（`auto`・`auto`・`true`） | [地図の描画を速くする設定](../features/images.md#地図の描画を速くする設定rasterspeed) |
| `complexImages` | `"dB"`・`"amplitude"`・`false` | SICD（I・Q のバンド）や複素数の画素の画像を振幅で表示。`"dB"` は 20·log10(振幅)、`false` で QGIS のまま | `"dB"` | [複素 SAR 画像の表示](../features/images.md#複素-sar-画像sicdの表示compleximages) |
| `imageRules` | 配列 | `match`（ファイル名の正規表現、必須）、`caseSensitive`、`preset`・`enhance`（補正）、`bands`、`noData`、`complexImages`（複素 SAR の表示。`"dB"`・`"amplitude"`・`false`） | なし | [画像補正](../features/enhance.md#画像補正) |
| `dem` | URL・パスかオブジェクト | クイック幾何補正の標高。ArcGIS のイメージサーバー（`…/ImageServer`）の URL か標高ファイルのパス、または `{ "url" か "file", "geoidHeight"（ジオイド高 m、−150〜150）, "renderingRule"（既定 `{ "rasterFunction": "None" }`）, "mosaicRule", "maxSize"（一辺の画素数、256〜16384） }` | なし（地形は補正しない。目印の点だけ） | [クイック幾何補正](../features/enhance.md#クイック幾何補正) |
| `imageCorrections` | URL かオブジェクト | クイック幾何補正を共有する Esri のテーブル（編集可、画像のファイル 1 つで 1 行）。`{ "url", "fields", "where" }`（既定の属性名は [`TBL_IMAGE_CORRECTION`](esri-tables.md#tbl_image_correction画像の幾何補正)） | なし（その PC だけに残す） | [クイック幾何補正](../features/enhance.md#クイック幾何補正) |

#### 撮像計画・撮像依頼

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `satelliteCatalogs` | 配列 | `url`（必須）、`fields`（`tle` か `tle1`＋`tle2` が必須。ほかに `name`・`id`・`maxOffNadir`・`minOffNadir`・`swath`・`length`・`lookSide`・`kind`・`scenesPerPass`・`minInterval`・`maxPerDay`・`shotsPerPass`・`series`・`leadTime`・`sensor`・`mode`・`resolution`・`system`）、`defaults`（属性がないときの値）、`swathUnit`（`km` か `m`）、`sarPattern`（SAR とみなす衛星名の正規表現）、`where` | なし | [撮像計画](../features/plan.md#撮像計画) |
| `sensorCatalogs` | 配列 | 設定ファイルに書くなら `sensors`（`[{ "sensor", "modes": [{ "mode", "swath", … }] }]`）、テーブルなら `url` と `fields`（`sensor` 必須。`mode`・`kind`・`swath`・`primary` など）。`swathUnit`・`sarPattern` | なし | [撮像計画](../features/plan.md#撮像計画) |
| `satelliteAliases` | オブジェクトか配列 | 衛星名の対応表（Esri のテーブル）の `url`（必須）、`fields`（`satellite` 衛星カタログの名前か ID・`alias` 画像カタログでの名前、ともに必須）、`label`・`where` | なし（名前の英数字の部分一致のみ） | [衛星名の対応表](../features/plan.md#衛星名の対応表satellitealiases) |
| `satelliteOutages` | URL かオブジェクト | 衛星・システムの障害情報の Esri のテーブル（編集可、障害 1 件で 1 行。システムの受付停止・臨時の 1 日の上限も、2026-11-24）。`{ "url", "fields", "where" }`（既定の属性名は [`TBL_SATELLITE_OUTAGE`](esri-tables.md#tbl_satellite_outage衛星の障害情報)） | なし（障害を計算に入れない。メニューの「衛星・システムの障害情報…」は使用不可） | [衛星の障害情報](../features/plan.md#衛星の障害情報satelliteoutages) |
| `groundStations` | 配列 | `name`・`lon`・`lat`（必須）、`minElevation`（既定 5°） | なし | [撮像計画](../features/plan.md#撮像計画) |
| `cloudForecast` | オブジェクトか `false` | `url`（Open-Meteo 形式の API）・`file`（予報ファイル）・`folder`（いちばん新しいファイルを読む）のどれか 1 つと `label`。`false` で雲量予報を出さない | Open-Meteo | [docs/cloud-forecast-file.md](../cloud-forecast-file.md) |
| `requestStores` | 配列 | 撮像依頼台帳の `url`・`fields`・`history`（履歴テーブル）・`historyFields`・`groups`（依頼グループのテーブル）・`groupFields`・`shots`（撮像の範囲のレイヤー）・`shotFields`・`quotas`（年間撮像上限のテーブル。`shots` も要る）・`quotaFields`・`catalog`・`missAfterDays`・`lookAheadDays`（残りの撮像機会を見る日数。既定 `7`、`0` で見ない）・`sameGeometry`（既定 `true`: 定点は前回と同じ撮り方を優先） | なし | [docs/imaging-requests.md](../imaging-requests.md) |
| `requestExports` | 配列 | 日次出力の衛星システムごとの `system`・`format`（`csv` など）・`satellites`・`series`・`limit`・`quota`・`orbit`（既定 `true`: 衛星の軌道でその日のパスに割り当て、撮像間隔・1 日の回数を守る）・`merge`（既定 `true`: 同じパスの連続シーンに収まる依頼を 1 行にまとめる）・`mergeMargin`（まとめるとき撮像の枠の内側に残す余裕、メートル。既定 `500`。分担の行の範囲にも使う）・`share`（既定 `true`: 1 ショットに収まらない範囲を、そのシステムのほかの衛星のパスで分担する）・`areaShape`（依頼の範囲の書き方。既定 `polygon` は範囲の形のまま、`rotatedBox` は範囲を囲む回転した最小の矩形。台帳はポリゴンのまま）・`priority`・`columns`・`folder`・`fileName`・`maxPerFile`（1 ファイルの依頼件数の上限。超えた日は分けて書き出す）・`useTime`（軌道の使用時間: `setupSeconds` 準備秒・`perPass` 1 パスの上限秒・`perDay` 1 日の上限秒。衛星ごと）・`linkIdLength`（外部連携ID の文字数、6〜32。既定 `12`）・`linkIdChars`（その文字: `base32`（既定）・`digits`・`hex`）・`timeZone`・`encoding`・`resultFile`（採否の結果ファイルの読み方: `format`・`encoding`・`delimiter`・`header`・`skipLines`・`columns`（`key`・`column`・`target`（外部連携ID は `linkId`）・`map`・`pattern`・`format`・`default`・`only`・`skip`）か JSON の `schema`（`x-target`）・`missing`・`sizeKm`・`linkDays`（外部連携ID をファイルの行の日の何日前の出力まで探すか。既定 `7`））など | なし | [docs/imaging-requests.md](../imaging-requests.md) |


#### 観測記録・分類体系

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `observationStores` | 配列 | `url`（編集できるポイントレイヤー、必須）、`fields`（`id`・`imageId`・`imageName`・`imageTime`・`objectId`・`objectName`・`kind`・`activity`・`present`・`note`・`length`・`heading`・`width`・`pixelX`・`pixelY`・`imageFile`・`satellite`（衛星名）・`unseen`・`confidence`・`recordedBy`・`updatedBy`・`created`・`updated`・`deleted`・`namedAreaObjectId`（地名の OBJECTID。Esri の属性ルールが入れ、プラグインは書かない）。既定は `OBS_ID`・`LENGTH_M`・`WIDTH_M` など）、`where`、`softDelete`（既定 `true`: 削除は印を付けるだけ）、`refreshMinutes`（ほかの人の記録を読み直す間隔（分）。なしなら画像の切り替えなどのときだけ。[読み直し](../features/observations.md#ほかの人の記録の読み直しrefreshminutes)）、`kinds`・`activities`・`confidences`・`taxonomy`（この保存先の語彙）、`areas`（確認範囲のポリゴンレイヤー。URL か `{ "url", "fields", "softDelete" }`）、`taxonomyTable`（共有の分類テーブル。URL か `{ "url", "fields" }`。役割は `path`・`levels`・`nameLabel`・`members`・`specs`・`photos`・`activities`・`order`、既定の属性名は `PATH`・…・`ACTIVITIES`・`SORT_ORDER`） | なし | [観測記録](../features/observations.md#観測記録)、[活動](../features/observations.md#活動大分類ごとの切り替え) |
| `localObservations` | `true`・`false` | `false` でローカルの GeoPackage を保存先にしない（`observationStores` だけ） | `true` | [観測記録](../features/observations.md#観測記録) |
| `offlineWork` | `true`・`false`・オブジェクト | サーバー障害時のオフライン作業。`enabled`（既定 `true`）、`cacheMB`（控えの上限、既定 500）、`probeSeconds`（復旧を確かめる間隔、既定 30） | 有効 | [オフライン作業](operations.md#サーバー障害時のオフライン作業offlinework) |
| `preload` | `true`・`false`・配列 | 起動直後に裏で読む Esri の表。配列は `satellites`・`requests`・`events` から（前の版の `savedPlans` は読み飛ばす） | `true`（すべて） | [先読み](operations.md#先読み起動直後に-esri-の表を読む) |
| `observationVocabulary` | オブジェクト | `taxonomy`（分類の木。`{ "name", "levels", "nameLabel", "members", "specs", "photos", "activities", "children" }`、名前だけの文字列も可）、`kinds`（`taxonomy` がないときの種別）、`activities`（既定の活動）、`confidences`（確度。文字列か `{ "label", "color", "note", "en", … }`。`en` は英語の名前で通報の `confidence.en`、ほかのキー（`code` など）は通報の `confidence.code` など）、`specFields`（諸元の項目。名前か `{ "name", "type": "text"・"number"・"list"・"url", "unit" }`）、`matchRadius`（同一候補の半径 m）、`unseenReasons`（不可の理由の候補。既定は 雲・霞・影・画像の欠け・その他） | 既定の分類体系（大分類ごとの活動つき） | [活動](../features/observations.md#活動大分類ごとの切り替え)、[分類体系の編集](../features/observations.md#分類体系の編集) |
| `glossary` | URL かオブジェクト | 備考の単語集の Esri のテーブル（1 語 1 行、読むだけ）。`{ "url", "fields", "where" }`（既定の属性名は [`TBL_GLOSSARY`](esri-tables.md#tbl_glossary備考の単語集)） | なし（備考に候補を出さない） | [備考の単語集](../features/observations.md#備考の単語集glossary) |
| `textAssist` | オブジェクト（文字列も可） | 文章の手伝いの小さな生成 AI。`{ "models": { 名前: 置き場所か { "path", "threads" } か社内サーバー { "url", "model", "timeoutSeconds" } }, "tasks": { タスク: モデルの名前か { "model", "maxTokens", "prompt", "examples" } }, "keepMinutes" }`。タスクは `tidyNote`・`handoverDraft`・`noteFields`・`searchWords`・`summary`・`translate`・`requestDraft`（撮像依頼のフォームの依頼文から下書き）・`command`（ツールバーの「文で操作」。`words` で操作を指す言葉を足せる）・`dataChat`（「データに質問」。9B 級以上。`tables`・`rules`・`examples`（`[{ "question", "sql" }]`）。[データに質問](../features/analysis.md#データに質問datachat)）。使う人が「担当と個人設定…」で「生成 AI の機能」を入れるまで出ない（オプトイン）。置き場所は ONNX Runtime GenAI のフォルダー・その `.zip`・`.zip` の URL。社内サーバーは OpenAI 互換の `…/v1`（`model` はサーバーでの名前。QGIS の認証設定・プロキシを使う。`timeoutSeconds` は 5〜3600、既定 300）。以前の書き方（文字列か `{ "model", … }`）はそのモデルを `tidyNote`・`handoverDraft` に割り当てたもの | なし（ボタンを出さない） | [文章の手伝い](../features/observations.md#文章の手伝い小さな生成-aitextassist) |
| `alertReports` | オブジェクト | 注目の記録の通報。`rules`（条件テーブル）・`geofences`（ポリゴンレイヤー）・`namedAreas`（地名のポリゴンレイヤー、項目 `place`）・`countries`（国のポリゴンレイヤー、項目 `country`）・`table`（通報テーブル、編集可）はそれぞれ URL か `{ "url", "fields", "where" }`。`minConfidence`・`presentOnly`（既定 `true`）、`notifyOnSave`（保存時のお知らせ、既定 `true`）、`live`（通知ドックの「通報」タブの自動更新: `{ "seconds": 30, "days": 7 }`（既定）・秒数・`false`）、`mail`（「通報」タブの「メールを作成」: `to`・`cc`・`bcc`・`subject`・`header`・`footer`・`encoding`・`maxLength`、`true` で宛先なし）、`score`（重要度スコア: `weights`・`rarity`・`minScore`、`false` で付けない）、`formats`（必須。名前付きのフォーマットの配列: `name`・`fields`・`delimiter`・`escape`・`header`・`footer`・`dateFormat`・`timeZone`・`newline`・`clipboard`・`file`・`columns`）、`default`（最初に選ぶフォーマットの名前） | なし（メニューはあるが使用不可） | [通報](../features/observations.md#通報注目の記録alertreports) |
| `observationExports` | オブジェクト | 観測記録のエクスポートのフォーマット。`formats`（名前付きの配列: `name`・`format`（`csv`・`json`）・`columns`・`schema`・`fileName`・`encoding`・`header`・`delimiter`・`newline`・`timeZone`・`dateFormat`・`digits`）、`default`、`namedAreas`・`geofences`（URL か `{ "url", "fields", "where" }`。なければ `alertReports` のもの） | 組み込みの「標準（CSV）」「標準（JSON）」のみ | [観測記録のエクスポート](../features/observations.md#観測記録のエクスポートobservationexports) |
| `assignments` | URL かオブジェクト | 分担（画像ごとの担当）の Esri のテーブル（編集可）。`{ "url", "fields", "where", "claimOnRecord", "mode" }`（`claimOnRecord: false` でクリック記録の開始時に担当を取らない。`mode`: `"shared"` 協業 = 同じ画像を何人でも（既定）、`"exclusive"` 排他 = 1 画像 1 人） | なし（分担は出ない） | [分担と引き継ぎメモ](../features/observations.md#分担と引き継ぎメモassignmentshandover) |
| `imageReviews` | URL かオブジェクト | 画像の評価（画像 × 地名ごとに、判読に使えるか・雲量・理由）の Esri のポリゴンのレイヤー（編集可）。`{ "url", "fields", "where" }`。地名は `namedAreas` から読む | なし（評価は出ない） | [画像の評価](../features/observations.md#画像の評価imagereviews) |
| `handover` | オブジェクト | 引き継ぎメモ。`table`（保存先、編集可）・`hours`・`dueHours`・`limit`・`dateFormat`・`timeZone`・`newline`・`file` | 保存先なしで作成・コピーのみ | [分担と引き継ぎメモ](../features/observations.md#分担と引き継ぎメモassignmentshandover) |
| `events` | オブジェクト | イベント（地震対応など）。`layer`（イベントのポリゴンレイヤーかテーブル、編集可）・`links`（紐付けテーブル、編集可）はそれぞれ URL か `{ "url", "fields", "where" }`。`kinds`（種別の候補、文字列の配列）| なし（ドックはあるが使用不可） | [イベント](../features/observations.md#イベント地震対応などevents) |
| `referenceInfo` | URL かオブジェクト | 出典つきの参考情報。`layer`（参考情報のポイントレイヤー、編集可）・`links`（イベント・モノ・記録との紐付けテーブル、編集可。なければ紐付けなし）はそれぞれ URL か `{ "url", "fields", "where" }`。`days`（観測記録で画像の前後に数える日数、既定 7）。URL だけならレイヤー | なし（ドックはあるが使用不可） | [出典つきの参考情報](../features/observations.md#出典つきの参考情報referenceinfo) |
| `userRoles` | URL かオブジェクト | 担当（いくつでも）を利用者名ごとに置く Esri のテーブル（編集可、1 人 × 1 担当で 1 行）。`{ "url", "fields", "where" }`（役割 `user`・`role`、既定の属性名 `USER_NAME`・`ROLE_NAME`） | なし（担当はこの PC にだけ保存） | [担当と使える機能](../features/screen.md#担当と使える機能userroles) |
| `training` | `true`・`false`・オブジェクト | 練習モード。`false`（か `{ "enabled": false }`）で出さない。`{ "config": "練習用の設定ファイル" }` で、内蔵の見本の代わりに組織の練習用サービスを指す設定ファイルで練習する（パスか URL、相対パスはこの設定ファイルのフォルダから）。`imageCatalogs` の `mode` が `training`・`both` のカタログは、練習データのカタログの後ろに出す（読むだけ）。内蔵の見本では、衛星・分類体系・地名・イベントの種類・備考の単語集をこの設定ファイルから読む（`fromProduction`: `false` で見本だけ、名前の配列でその台帳だけ）。`scoring` で判読の練習の採点のしかた（位置の許容・項目の割合・種別の部分点・誤りの減点・測るの許容・合格点）。`exercises` で判読の練習の組織の問題・正解・結果の表（`{"exercises": URL, "answers": URL, "results": URL}`、[`TBL_TRAINING_EXERCISE` など](esri-tables.md#tbl_training_exercise判読の練習の問題)） | `true`（内蔵の見本: 東京湾・横須賀。台帳はこの設定ファイルのもの） | [練習モード](../features/screen.md#練習モードtraining) |
| `interests` | オブジェクト | 個人の関心地域・地点。`points`（ポイントのレイヤー、編集可）・`areas`（ポリゴンのレイヤー、編集可）はそれぞれ URL か `{ "url", "fields", "where" }`（どちらか 1 つでも可）。`label`（カードの見出し、既定「関心地域・地点」）、`color`（新しく作るときの色、既定 `#f97316`）。`footprintLayers` とは別に 1 回だけ書き、すべてのカタログのカードに出る | なし（ドックはあるが使用不可） | [個人の関心地域・地点](../features/images.md#個人の関心地域地点interests) |
| `namedAreas` | URL かオブジェクト | 組織の関心地域（2026-11-24）。地名のレイヤー（`TBL_NAMED_AREA`、編集可）の URL か `{ "url", "fields", "where", "categories", "countries", "idPrefix", "idDigits", "label" }`。`categories` はカテゴリーの表（`TBL_AREA_CATEGORY`、編集可）、`countries` は国のレイヤー（なければ `alertReports.countries`）、`idPrefix`・`idDigits` は空の管理番号に付ける番号の形（既定 `NA-`・6 桁）、`label` はカードの見出し（既定「組織の関心地域」）。用途（`AREA_USE`）が関心地域・両方の行を新着画像のカードと「関心地域・地点」の「組織」に出し、全員が編集できる | `alertReports.namedAreas`（あれば。なければ「組織」は使用不可） | [組織の関心地域](../features/images.md#組織の関心地域namedareas) |

#### 地図・レイヤー・場所

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `baseMaps` | 配列 | `id`・`label`・`url`・`type`（`xyz`・`mvt`・`style`・`esri`。省略時は URL から判断）・`style`・`maxZoom`・`attributions`。書くと一覧をこれで置き換え | 地理院・OSM | [背景地図](../features/map.md#背景地図) |
| `defaultBaseMap` | 文字列 | 起動時の背景地図の `id` | なし | [背景地図](../features/map.md#背景地図) |
| `layers` | 配列 | 空のプロジェクトで起動したときに追加。`type`（`cog`・`file`・`wms`・`wmts`・`wfs`・`esri`・`ogc-features`・`ogc-tiles`・`wcs`・`ogc-coverage`・`xyz`）、`url`、`layer`（サービスのとき必須）、`matrixSet`・`format`・`bbox`・`filter`・`settings`（`esri`）、`visible`（`false` で最初は非表示。既定 `true`）、`pinned`（`true` で一番上に固定。既定 `false`） | なし | [起動時のレイヤー](../features/map.md#起動時のレイヤー) |
| `geocoders` | 配列 | `type`（`gsi`・`nominatim`・`arcgis`・`features`・`csv`）、`url`、`label`、`field`（`features` のとき必須。`features` は属性名の配列で複数可、先頭が地名） | なし（地名は検索しない） | [座標・地名へ移動](../features/map.md#座標地名へ移動) |
| `shapeReceiver` | `true`・`false`・オブジェクト | AutoMap・リンク・この PC のプログラムから図形を受け取って地図に表示（`127.0.0.1` だけで待ち受け）。`enabled`、`port`（既定 `47631`）、`origins`（受け取る送信元。既定 `["chrome-extension://*"]`） | 有効 | [外部から図形を受け取る](../features/map.md#外部から図形を受け取るautomap-などshapereceiver) |
| `layerTemplates` | 配列 | `name`（必須）・`geometryType`・`fields`（`[{ "name", "type", "alias", "required", "length", "min", "max", "defaultValue", "codes" }]`）・`title`・`description` | 組み込みのテンプレートだけ | [新規スクラッチレイヤー](../features/map.md#新規スクラッチレイヤーテンプレート) |
| `arcgisPortals` | 配列 | `url`（ポータルの https）・`clientId`（必須）・`label` | なし | [ArcGIS Web マップ](../features/map.md#arcgis-web-マップ) |
| `movingFeeds` | 配列 | `type`（`websocket`・`sse`・`poll`）、`url`、`label`、`interval`（秒）、`fields`（`id`・`time`・`lat`・`lon` など）、`kind`（`ship`・`aircraft`） | なし | [移動体](../features/observations.md#移動体aisads-b) |
| `gpServices` | 配列 | `url`（GP サービスか OGC API – Processes）・`label` | なし | [サーバ処理](../features/analysis.md#サーバ処理) |

#### AI

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `aiModels` | 配列 | 検出: `url`（必須）・`task`（`detect`・`obb`・`segment`）・`inputSize`・`classes`（配列か `"coco"`）・`format`・`channels`・`mean`・`std`・`maxDetections`・`strides`・`score`・`iou`（確信度と重なりのしきい値、0 と 1 の間）・`kinds`（クラス → 種別、プラグインだけ）。SAM: `encoder`・`decoder`・`inputSize` | なし | [AI](../features/ai.md#ai物体検出クリック抽出) |
| `detectionOutputs` | 配列 | 検出結果の転記先。`url`、`fields`（`imageId`・`class`・`score`・`status` など）、`statusValues`、`constants`、`aiResults`（`false` で「AI結果」に読まない。既定 `true`） | なし | [AI](../features/ai.md#ai物体検出クリック抽出)・[AI結果](../features/ai.md#ai結果外部aiプラグイン内の物体検出) |
| `aiResultSources` | 配列 | 外部 AI の結果。`url`、`fields`（`imageId`・`id`・`class`・`score`・`model`・`detectedAt`・`name`・`kind`・`activity`・`length`・`heading`・`width`・`note`）、`kinds`、`minScore`（0〜1）、`continuation`（この出どころだけの継続の判定。形は `aiContinuation` と同じで、書いたキーだけ優先）、`continueRadius`・`candidateRadius`（m。古い書き方）、`maxResults`、`activity`・`confidence`（取り込み時の既定）、`where` | なし | [AI結果](../features/ai.md#ai結果外部aiプラグイン内の物体検出) |
| `aiContinuation` | オブジェクトか `false` | AI結果を取り込むときの継続の判定。`rules`（自動継続のルールの配列。ルールの中の条件はすべて満たし、どれか 1 つのルールで自動継続: `label`・`near`（m）・`centerInRecord`・`recordInAi`・`overlap`（0〜1）・`kind`（`same`・`related`・`sibling`・`any`）・`lengthGap`（0〜1）・`headingGap`（°）・`minScore`（0〜1））、`candidateRadius`（m）、`unique`。外部 AI・転記先・プラグイン内の検出すべてに効く。`false` で照合しない | 50 m 以内（か長さの半分）・分類が同じか上下・長さの差 30% 以内、候補は 300 m | [継続の判定](../features/ai.md#継続の判定aicontinuation) |

#### 編集の記録

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `editTracking` | オブジェクトか `false` | 書き込む Esri のレイヤー・テーブルの作成日時・作成者・更新日時・更新者。`userVariable`（ユーザー名の環境変数、既定 `USERNAME`）、`user`（固定のユーザー名）、`fields`（`createdAt`・`createdBy`・`updatedAt`・`updatedBy`。既定 `CREATED_DATE`・`CREATED_USER`・`UPDATED_DATE`・`UPDATED_USER`）、`enabled`。`false` で書かない | 既定の属性名があるレイヤーに書く | [作成者・更新者の記録](operations.md#作成者更新者の記録edittracking) |
| `auditLog` | オブジェクト・文字列（送り先）か `false` | 操作の監査ログと自動送信。`folder`（送り先、`{user}`・`{computer}`・`{yyyy}`・`{mm}`・`{dd}`）、`sendMinutes`（既定 10）、`format`（`jsonl`・`csv`）、`encoding`、`events`（`session`・`config`・`edit`・`offline`・`open`・`export`・`copy`・`error`）、`values`、`keepDays`（既定 90）、`localFolder`、`enabled`。`false` で記録しない | この PC にだけ記録（送らない） | [監査ログ](operations.md#監査ログauditlog) |

#### 日時

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `timeZones` | 配列 | `match`（URL の正規表現、必須）・`timeZone`（必須）・`serviceTimeZone`・`label` | なし | [日時のタイムゾーン](operations.md#日時のタイムゾーンtimezonetimezones) |
| `timeZone`・`serviceTimeZone`（各項目の中） | 文字列 | `layers`・`imageCatalogs`・`observationStores`・`detectionOutputs`・`aiResultSources`・`satelliteCatalogs`・`sensorCatalogs`・`movingFeeds`・`gpServices` の要素に書ける。IANA 名（`Asia/Tokyo`）・時差（`+09:00`）・Windows の名前。`timeZones` より優先 | サービスの申告、なければ UTC | [日時のタイムゾーン](operations.md#日時のタイムゾーンtimezonetimezones) |

#### 一覧

| 項目 | 形 | 主な中身 | 既定 | 詳しく |
| --- | --- | --- | --- | --- |
| `columnDrag` | `true`・`false` | `false` で一覧の列を見出しのドラッグで並べ替えない（見出しの右クリックのメニューだけ） | `true` | [一覧のビュー](../features/screen.md#一覧のビュー列並べ替え絞り込みの保存) |
| `analysisCacheMinutes` | 数 | データ分析で、同じ表への同じ問い合わせにメモリの答えを使う分数（0〜1440、0 で使わない） | 5 | [データ分析](../features/analysis.md#データ分析) |

### 設定ファイルに書かないもの

- 設定ファイルのパス・URL、記録者、サービスごとの認証設定、`pathMappings` で選んだフォルダなどは各自の QGIS に保存する（下の[設定](install.md#設定)の表）
- ZIP に同梱・固定するかどうかは設定ファイルの項目ではなく、ZIP を作るときの `--config`・`--lock-config`（[導入](install.md#導入)）
- 増えたり変わったりする対応表などのデータは、設定ファイルではなく Esri のテーブル・レイヤーに置き、設定ファイルには属性名・テーブルの URL だけを書く（分類体系の `taxonomyTable` など）

ビューアとの違い（プラグインだけの項目、ビューアと読み方が違う項目）は [docs/differences.md](../differences.md)。
