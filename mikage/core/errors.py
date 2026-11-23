"""
エラーコード: one code per cause of a failure, the same on every screen
(tsudo 2026-11-23, design doc 「エラーコードの設計方針（案）」).

A failure reads 「〇〇に失敗: 理由［E101］」: what was being done (the
screen's own words), why (the code's text, the same everywhere) and the
code, which the help's 「エラーコード一覧」 explains with what to do
(scripts/build_help.py makes that section, and docs/admin/error-codes.md,
from CODES). The hundreds digit is the kind of cause:

    E1xx 通信・サーバー         E5xx 画像
    E2xx サインイン・権限・担当  E6xx AI・計算
    E3xx ほかの人の作業・件数    E7xx 設定・保存先の表（管理者が直すもの）
    E4xx ファイル・フォルダ      E9xx 想定外（不具合）

Rules (tests/test_error_codes.py checks them):

* A code is never reused for another cause: one no longer used stays in
  RETIRED (old screenshots and notes keep meaning what they meant).
* Every code in CODES is used somewhere in the plugin, and every code the
  plugin uses is in CODES.
* No code for what is not a failure: guidance that says what to enter
  (「名前を入力」), done notices, an empty list, a question, the person's own
  取消, nor the texts for developers (the Python API, the command line,
  AutoMap's request details, the practice server's inside). The settings
  file's own problems are one code (E701): the 「設定…」 window lists them.
* The screen shows the code's text (and, for the administrator, a key or an
  attribute name in 「（管理者に連絡: …）」); a server's address, its own
  message and Python's go to the log (`detail`), never to the screen
  (wording rules 8 and 9, 2026-11-23).

This module imports nothing of the plugin's (it is read by
scripts/build_help.py on its own, without QGIS).
"""
from __future__ import annotations

import errno
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Code:
    code: str
    #: What the screen says (言い切り, the wording rules of 2026-11-23).
    text: str
    #: Why it happens, for the help (です・ます).
    cause: str
    #: What the person can do, then what to ask the administrator (です・ます).
    remedy: str
    #: For the administrator (docs/admin/error-codes.md): where to look.
    admin: str = ""


#: The kinds of cause, by the code's hundreds digit (the help's subsections).
KINDS = {
    "1": ("errors-net", "通信・サーバー（E1xx）"),
    "2": ("errors-signin", "サインイン・権限・担当（E2xx）"),
    "3": ("errors-others", "ほかの人の作業・件数（E3xx）"),
    "4": ("errors-files", "ファイル・フォルダ（E4xx）"),
    "5": ("errors-images", "画像（E5xx）"),
    "6": ("errors-ai", "AI・計算（E6xx）"),
    "7": ("errors-settings", "設定・保存先の表（E7xx）"),
    "9": ("errors-unexpected", "想定外（E9xx）"),
}

_ADMIN = "続くときは、コードと時刻を管理者に伝えてください。"
_LOG = "ログメッセージの「MIKAGE」タブと監査ログに、相手の URL と詳しい理由がある。"

_CODES = (
    # 1xx 通信・サーバー
    Code("E101", "サーバーに接続できない",
         "この PC からサーバーにつながりません。ネットワークが切れているか、サーバーが止まっています。",
         "ネットワーク（有線・Wi-Fi・VPN）を確かめ、しばらくしてからやり直してください。サーバーが止まっている間も、"
         "右下に「⚠ オフライン作業中」と出ていれば作業を続けられます（保存したものは送信待ちになります）。" + _ADMIN,
         _LOG + "サーバー・プロキシ・ファイアウォールを確かめる。"),
    Code("E102", "サーバーの応答がない（時間切れ）",
         "決まった時間のうちにサーバーが答えませんでした。混んでいるだけのこともあります（朝に大勢が一度に始めたときなど）。",
         "1〜2 分待ってから、もう一度やり直してください。" + _ADMIN,
         _LOG + "ArcGIS Server のインスタンス数と応答時間（docs/admin/operations.md の「裏の処理中の画面のなめらかさ」）。"),
    Code("E103", "サーバーの中でエラー",
         "サーバーが要求を受け取りましたが、サーバーの中で処理に失敗しました。",
         "しばらくしてから、もう一度やり直してください。" + _ADMIN,
         _LOG + "HTTP 5xx。ArcGIS Server のログを確かめる。"),
    Code("E104", "サーバーが要求を受け付けない",
         "サーバーが要求を断りました。条件の書き方、表の作り、サーバーの設定が合っていないときに起きます。",
         "条件を入れたときは、条件を見直してやり直してください。" + _ADMIN,
         _LOG + "サーバーが返した code と message もそこにある。"),
    Code("E105", "サーバーの応答を読めない",
         "サーバーの答えが、MIKAGE が読める形ではありませんでした。設定の URL が別のページを指しているか、"
         "途中の装置（プロキシのサインイン画面など）が別のページを返しています。",
         "ブラウザで一度サインインしてから、やり直してください。" + _ADMIN,
         _LOG + "設定ファイルの URL とプロキシを確かめる。"),
    Code("E106", "サーバーの応答が大きすぎる",
         "サーバーの答えが、MIKAGE が一度に受け取れる大きさを超えました。",
         "期間や範囲を狭めて、やり直してください。",
         _LOG),
    Code("E107", "オフライン中で控えなし",
         "サーバーにつながらないため、オフライン作業中です。まだ一度も開いていない一覧は、この PC に控えがありません。",
         "サーバーにつながってから開き直してください。止まるのが分かっているときは、使う一覧を先に一度開いておくと、"
         "止まっている間も控えで見られます。",
         "docs/admin/operations.md の「サーバー障害時のオフライン作業」。"),
    Code("E108", "処理サービスで失敗",
         "サーバーの処理サービス（ArcGIS の GP サービス・OGC API の処理）が、サーバーの中で失敗しました。",
         "入力（図形・値）を見直して、もう一度やり直してください。" + _ADMIN,
         _LOG + "サービスのジョブのメッセージもそこにある。"),
    Code("E109", "処理サービスとして使えない URL",
         "入力した URL が、MIKAGE の使える処理サービス（ArcGIS の GP サービス・GP タスク・OGC API – Processes）ではありません。",
         "URL を確かめてください。分からないときは管理者に聞いてください。",
         _LOG),
    Code("E110", "この PC の中の通信を始められない",
         "この PC の中で使う通信の口（ポート）を開けませんでした。ほかのソフトか、もう 1 つの QGIS が使っています。",
         "MIKAGE の入った QGIS を 2 つ開いていたら、片方を閉じてください。" + _ADMIN,
         "練習モードの練習用サーバー・AutoMap からの受け取り（shapeReceiver.port）。"),
    # 2xx サインイン・権限・担当
    Code("E201", "サインインの期限切れ",
         "サーバーへのサインインの期限が切れたか、サインインが取り消されました。",
         "もう一度やり直してください（MIKAGE がサインインし直します）。続くときは QGIS を再起動して、サインインし直してください。",
         "トークンの期限（498・499）。認証設定（authcfg）の OAuth2 の更新を確かめる。"),
    Code("E202", "使う権限がない",
         "サインインした利用者に、この表を読む・書く権限がありません。",
         "管理者に権限を頼んでください。",
         "ポータルのグループと、フィーチャ サービスの共有・編集の設定。"),
    Code("E203", "サインインが必要",
         "このサーバーはサインインが必要ですが、サインインの設定がありません。",
         "管理者に、サインインの設定（認証設定）を頼んでください。",
         "設定ファイルの authcfg と、QGIS の認証設定。"),
    Code("E204", "サインインの設定を使えない",
         "QGIS のサインインの設定（認証設定）が見つからないか、QGIS のマスターパスワードが入っていません。",
         "QGIS がマスターパスワードを聞いてきたら入れてください。続くときは管理者に連絡してください。",
         "QGIS の 設定 ▸ オプション ▸ 認証。設定ファイルの authcfg の ID。"),
    Code("E205", "利用者名が分からない",
         "記録に残す利用者名を、この PC から得られませんでした。",
         "管理者に連絡してください。",
         "設定ファイルの editTracking.user（docs/admin/operations.md の「作成者・更新者の記録」）。"),
    Code("E206", "この担当では使えない",
         "ほかの担当の機能か、設定ファイルで使わないことにした機能です。",
         "自分の作業なら、メニュー「担当と個人設定…」でその担当にもチェックしてください。",
         "設定ファイルと、利用者の担当（TBL_USER_ROLE）。"),
    # 3xx ほかの人の作業・件数
    Code("E301", "ほかの人が先に変更",
         "読み込んだあとに、ほかの人が同じものを変更しました。上書きしないように、保存を止めています。",
         "「最新を取得」を押してから、もう一度変更してください。",
         "編集の記録（editTracking）の UPDATED_DATE で比べている。"),
    Code("E302", "ほかの人が削除済み",
         "読み込んだあとに、ほかの人が同じものを削除しました。",
         "「最新を取得」を押して、確かめてください。",
         ""),
    Code("E303", "ほかの人が担当中",
         "1 画像 1 人の決まりのとき、ほかの人がこの画像の担当になっています。",
         "別の画像にするか、担当の人に「ほかの人に渡す」を頼んでください。",
         "設定ファイルの分担（assignments）。"),
    Code("E304", "件数が上限を超えた",
         "一度に取得できる件数（上限）を超えたため、途中で止めました。一部だけを見せて間違えないように、何も取得していません。",
         "期間・範囲・条件で絞ってから、やり直してください。古い行は管理者に整理を頼んでください。",
         "上限は機能ごと（観測記録 observationStores の limit など）。"),
    Code("E305", "サーバーが保存を受け付けない",
         "サーバーが行の追加・変更・削除を断りました（値の長さや形、権限、ほかの人が先に削除したなど）。"
         "何件かのうちの一部のときは、ほかの行は保存されています。",
         "「最新を取得」を押してから、残りをもう一度保存してください。" + _ADMIN,
         _LOG + "サーバーが行ごとに返した理由もそこにある。"),
    # 4xx ファイル・フォルダ
    Code("E401", "ファイル・フォルダがない",
         "指定した場所にファイルかフォルダがありません。移動・削除されたか、共有フォルダにつながっていません。",
         "場所を確かめてください。共有フォルダなら、エクスプローラーで開けるか確かめてください。",
         "ログに場所がある。"),
    Code("E402", "ファイルに書き込めない（使用中・権限）",
         "ファイルかフォルダに書き込めません。ほかのソフトで開いているか、書き込む権限がありません。",
         "Excel などで開いていたら閉じてから、やり直してください。直らないときは、別の場所に保存してください。",
         "ログに場所がある。フォルダのアクセス権。"),
    Code("E403", "ディスクの空きがない",
         "保存先のディスクがいっぱいです。",
         "不要なファイルを消すか、別の場所に保存してください。",
         ""),
    Code("E404", "ファイルを読めない（形式が違う・壊れている）",
         "ファイルの中身が、期待した形式ではありません。別の種類のファイルか、途中で切れたファイルです。",
         "選んだファイルが正しいか確かめてください。作り直せるなら作り直してください。",
         "ログに理由（何行目か など）がある。"),
    Code("E405", "文字コードを読めない",
         "ファイルの文字コードが、UTF-8 でも Shift_JIS でもありません。",
         "表計算ソフトで「UTF-8」か「Shift_JIS」を選んで保存し直してから、取り込んでください。",
         ""),
    Code("E406", "ファイルが大きすぎる",
         "ファイルが、この操作で扱える大きさを超えています。",
         "小さく分けるか、範囲を狭めてください。",
         ""),
    Code("E407", "ファイルを開けない",
         "ファイルかフォルダを開けませんでした（上のどれにも当たらない理由）。",
         "場所と権限を確かめてから、やり直してください。" + _ADMIN,
         "ログに OS の理由がある。"),
    # 5xx 画像
    Code("E501", "画像の場所が分からない",
         "画像カタログのこの行に、画像ファイルの場所が書かれていません。",
         "管理者に連絡してください。ファイルのない画像として記録はできます。",
         "画像カタログの表の場所の属性と、設定ファイルの catalog。"),
    Code("E502", "画像のフォルダが見つからない",
         "画像のある共有フォルダが、この PC から見つかりません。",
         "「フォルダを選んで」と出たら、画像の共有フォルダを選んでください。違うフォルダを覚えてしまったら、"
         "メニュー「設定…」の「画像のフォルダの対応」で「対応を消去」してください。",
         "設定ファイルの pathMappings。"),
    Code("E503", "画像を開けない",
         "画像ファイルを開けませんでした。壊れているか、書き込みの途中か、QGIS が読めない形式です。",
         "しばらくしてから開き直してください。ほかの画像が開けるなら、その画像だけの問題です。" + _ADMIN,
         "ログに GDAL の理由がある。"),
    Code("E504", "画像にそのバンドがない",
         "使うバンド（色の層）が、この画像にありません。",
         "バンドの番号か、別の画像を選んでください。",
         "画像補正の設定（enhance のルール）や AI のモデルのバンドの指定。"),
    Code("E505", "向きを直していない画像には使えない",
         "北が上になっていない（回転のある）画像には、この処理を使えません。",
         "北が上の画像（オルソ画像など）で、やり直してください。",
         ""),
    Code("E506", "画像が重ならない",
         "2 つの画像の重なる所がないか、小さすぎるか、値のある画素がありません。",
         "同じ場所を写した画像を選んでください。",
         ""),
    Code("E507", "画像の位置を合わせられない",
         "2 つの画像に、位置合わせに使える同じ模様が見つかりませんでした。",
         "重なりの大きい画像か、模様（建物・道路など）のある範囲で、やり直してください。",
         ""),
    Code("E508", "画像の座標変換に失敗",
         "画像の座標を、別の座標系や格子に直せませんでした。",
         "画像の座標系を確かめてください。" + _ADMIN,
         "ログに理由がある。"),
    Code("E509", "標高データを使えない",
         "標高データ（ファイルかイメージサーバー）を開けない・読めないか、画像の範囲に標高データがありません。",
         "画像の範囲を含む標高データを選んでください。クイック幾何補正では、地形の補正を外せば目印の点だけで直せます。" + _ADMIN,
         "設定ファイルの dem（イメージサーバーの URL・トークン、標高ファイルのパス）。ログにサーバーの答えがある。"),
    Code("E510", "ピラミッドを作れない",
         "大きな画像を速く表示するための縮小版（ピラミッド）を作れませんでした。画像はそのまま見られますが、表示が遅いままです。",
         "そのまま作業を続けられます。" + _ADMIN,
         "画像の隣・キャッシュフォルダ（overviews）への書き込み権限。"),
    # 6xx AI・計算
    Code("E601", "AI の部品がない",
         "AI の実行に使う部品（onnxruntime）が、この QGIS に入っていません。",
         "Windows 版のプラグイン（名前が -win64 の zip）を入れてもらってください。管理者に相談してください。",
         "-win64 の zip に同梱。Python の版が QGIS と合っているか。"),
    Code("E602", "文章の手伝いの部品がない",
         "文章の手伝い（生成 AI）に使う部品（onnxruntime-genai）が、この QGIS に入っていません。",
         "Windows 版のプラグイン（名前が -win64 の zip）を入れてもらってください。管理者に相談してください。",
         "-win64 の zip に同梱。"),
    Code("E603", "AI のモデルがない",
         "使うモデルのファイルが見つかりません。",
         "モデルを選び直してください。設定ファイルのモデルなら管理者に連絡してください。",
         "設定ファイルの aiModels・textAssist。"),
    Code("E604", "AI のモデルを読めない",
         "モデルのファイルが壊れているか、MIKAGE が読めない形式です。",
         "別のモデルを選んでください。" + _ADMIN,
         "ログに理由がある。"),
    Code("E605", "モデルと画像が合わない",
         "モデルが学習に使ったバンドの数や値と、この画像が合いません。",
         "そのモデルを学習したときと同じ種類の画像で、やり直してください。",
         ""),
    Code("E606", "学習に使えるものがない",
         "学習に使える物体・範囲・画素がありませんでした。",
         "種別のある記録や注釈がある画像を選んでください。観測記録なら、記録した画像（同じ画像 ID）を選んでください。",
         ""),
    Code("E607", "教師データを読めない",
         "教師データセットや注釈のファイルを読めませんでした（形式が違う・新しい版で作った・ファイルがない）。",
         "ファイルとフォルダの指定を確かめてください。新しい版で作ったときは、プラグインを更新してください。",
         "ログに理由がある。"),
    Code("E608", "文章が長すぎる",
         "文章の手伝いに渡す文が、モデルが一度に読める長さを超えました。",
         "メモを短くするか、分けてやり直してください。",
         ""),
    Code("E609", "計算に失敗",
         "計算の途中で失敗しました（分類・変化の検出・パンシャープンなど）。",
         "入力（画像・範囲・値）を見直して、やり直してください。" + _ADMIN,
         "ログに理由がある。"),
    Code("E610", "軌道の計算に失敗",
         "衛星の軌道要素（TLE）から、通る時刻や軌跡を計算できませんでした。",
         "衛星カタログの軌道要素が新しいか確かめて、やり直してください。" + _ADMIN,
         "衛星カタログ（TLE）の取得元と更新日。"),
    # 7xx 設定・保存先の表
    Code("E701", "設定ファイルに問題がある",
         "設定ファイル（config.json）に、使えない値や書き間違いがあります。問題のある所だけを使わずに動いています。",
         "黄色い帯の「設定…」で中身を見られます。直せないときは、その画面を管理者に見せてください。",
         "「設定…」に問題の一覧。docs/admin/config-file.md。"),
    Code("E702", "設定ファイルを読めない",
         "設定ファイル（config.json）が、JSON として読めません。カンマやカッコの抜けなどです。",
         "管理者に連絡してください。",
         "ログに何行目・何列目かがある。tools/config_builder.html で作り直せる。"),
    Code("E703", "保存先が未設定",
         "この機能の保存先（Esri の表やフォルダ）が、設定ファイルにありません。",
         "管理者に連絡してください（メッセージのカッコの中が、設定する項目です）。",
         "メッセージの「（管理者に連絡: キー）」が設定ファイルのキー。"),
    Code("E704", "保存先の表に必要な項目がない",
         "保存先の表に、この機能が使う項目（属性）がありません。",
         "管理者に連絡してください（メッセージのカッコの中が、ない項目です）。",
         "docs/admin/esri-tables.md の表の定義。メニュー「接続と表の点検…」で一度に確かめられる。"),
    Code("E705", "保存先の表が見つからない",
         "設定ファイルの保存先に、表やレイヤーがありません。",
         "管理者に連絡してください。",
         "設定ファイルの URL と、メニュー「接続と表の点検…」。"),
    # 9xx 想定外
    Code("E901", "想定外のエラー",
         "MIKAGE の不具合です。",
         "もう一度やり直してください。続くときは、何をしたら出たかと、コードと時刻を管理者に伝えてください。",
         "ログメッセージの「MIKAGE」タブと監査ログに、Python のエラーの全文（トレースバック）がある。"),
    Code("E902", "プラグインの部品が足りない",
         "MIKAGE のファイルの一部が見つからないか、読み込めません。入れ直しが途中で止まったときなどに起きます。",
         "QGIS を再起動してください。続くときは、管理者にプラグインを入れ直してもらってください。",
         "docs/admin/install.md。Windows 版は DLL の写し（%LOCALAPPDATA%）も確かめる。"),
)

CODES: dict[str, Code] = {c.code: c for c in _CODES}
#: Codes no longer used, with what they meant: never given to another cause.
RETIRED: dict[str, str] = {}

_TAG = re.compile(r"［(E\d{3})］|\[(E\d{3})\]")
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿！-～]")


def tagged(text: str, code: str) -> str:
    """The text with its code at the end: 「サーバーに接続できない［E101］」 (as it is without a code)."""
    if not code or code_of(text) == code:
        return text
    return f"{text}［{code}］"


def text(code: str, extra: str = "") -> str:
    """The code's text, with `extra` for the administrator in 「（管理者に連絡: …）」 or a short supplement."""
    return CODES[code].text + (extra or "")


def code_of(text: Optional[str]) -> Optional[str]:
    """The first code in a text (「…［E101］」), or None."""
    m = _TAG.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


def untagged(text: str) -> str:
    """The text without its codes."""
    return _TAG.sub("", text or "")


def normalized(text: str) -> Optional[str]:
    """
    A code someone typed, as it is written ("E101"), from "e101", "Ｅ１０１",
    "E-101", "[E101]" or "E 101"; None when it is not a code's shape.
    """
    t = unicodedata.normalize("NFKC", text or "").strip().upper()
    t = re.sub(r"[\s\-‐－_［］\[\]（）()]", "", t)
    return t if re.fullmatch(r"E\d{3}", t) else None


class Coded(Exception):
    """
    An error with a code: str() is the screen's text and its code
    (「サーバーに接続できない［E101］」); `detail` (a server's address, its
    own message) goes to the log only. A class sets its own `code`; a raise
    may give another, or none for guidance (「名前を入力」) and 「取消」.
    """

    code: str = ""

    def __init__(self, message: str = "", *, code: Optional[str] = None, detail: str = ""):
        chosen = type(self).code if code is None else code
        if not message and chosen in CODES:
            message = CODES[chosen].text
        self.code = chosen or ""
        self.message = untagged(message) if chosen else message
        self.detail = detail
        super().__init__(tagged(self.message, self.code))


class Failed(Coded, ValueError):
    """A coded failure where callers catch ValueError (a file's contents, a computation)."""


class FileFailed(Coded, OSError):
    """A coded failure where callers catch OSError (a file or a folder)."""


def has_japanese(text: str) -> bool:
    """Whether a message is the plugin's own (Japanese) rather than Python's or a server's (English)."""
    return bool(_JAPANESE.search(text or ""))


def os_code(error: OSError) -> str:
    """The code of a file system error by its kind."""
    if isinstance(error, (FileNotFoundError, NotADirectoryError)) or error.errno == errno.ENOENT:
        return "E401"
    if isinstance(error, PermissionError) or error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY):
        return "E402"
    if error.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return "E403"
    return "E407"


def describe(error: BaseException) -> tuple[str, str]:
    """
    What the screen shows for an error (its text, with a code when it is a
    failure) and what goes to the log only (its detail; "" when none):

    * a Coded error: its text and code, its detail;
    * a file system error (OSError) of Python's: the code of its kind, the
      path and the system's reason in the detail;
    * any other error whose message is the plugin's own (Japanese): as it is
      (guidance that says what to do needs no code);
    * anything else (a Python error in the plugin, an English message from a
      library): E901, the error in the detail.
    """
    if isinstance(error, Coded):
        return str(error), error.detail
    message = str(error)
    if has_japanese(message):
        return message, ""
    if isinstance(error, OSError):
        path = getattr(error, "filename", None)
        reason = error.strerror or message or type(error).__name__
        return tagged(CODES[os_code(error)].text, os_code(error)), f"{path}: {reason}" if path else reason
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return tagged(CODES["E902"].text, "E902"), f"{type(error).__name__}: {message}"
    return tagged(CODES["E901"].text, "E901"), f"{type(error).__name__}: {message}" if message else type(error).__name__


def detailed(error: BaseException) -> str:
    """The screen's text with its detail, for the log and the administrators' screens (「接続と表の点検」)."""
    shown, detail = describe(error)
    if not detail:
        return shown
    code = code_of(shown)
    return tagged(f"{untagged(shown)}（{detail}）", code) if code else f"{shown}（{detail}）"
