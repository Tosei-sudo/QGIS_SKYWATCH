"""
担当と使える機能 (tsudo, 2026-11-15: 「バックエンドで認証認可制御するのは大前提なんだけど、
プラグイン側でもある程度コントロールしたい」「権限管理は Esri の表とユーザー名。インストール時の
設定ダイアログで自分の権限を任意に選べるように（ツール運用のコスト低減のため）、今の役割選択を
チェックボックスにしたうえで、好きなだけ権限をとれるように」).

Each person ticks the roles (担当) they work in, as many as they like; a
feature of the plugin no role of theirs uses is not shown (menu, toolbar,
the buttons in the docks). It is not a lock: the server's own permissions
stay what really allows or refuses a change. It keeps out of sight what the
person's work does not need (an observer does not write the plan files,
someone who asks for images does not check the tables' connections).

* No role ticked: everything but the administrators' features.
* The choice is kept in Esri (`userRoles`, TBL_USER_ROLE): one row per role
  of a person, the person being the user name `editTracking` writes (the
  environment variable USERNAME by default), so it follows them to any PC;
  without the table, on this PC only.

A feature is named by its menu entry's text, or by a key of the buttons of a
dock (FEATURES); one not listed is for everyone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .esri import EsriClient, EsriError, fit_value, quote
from .sources import EsriSource, esri_source_of

#: What a role may do (a feature needs one of them).
REQUEST, PLAN, OBSERVE, TRAIN, ADMIN = "request", "plan", "observe", "train", "admin"


@dataclass(frozen=True)
class Role:
    #: Kept in the table (TBL_USER_ROLE.ROLE_NAME) and in the profile.
    code: str
    #: What the person sees (also the name of its example of 「ドックのレイアウト」).
    name: str
    permissions: frozenset
    #: A line under the check box: what the role is for.
    hint: str


#: The roles, in the order they are offered.
ROLES: tuple[Role, ...] = (
    Role("PLANNER", "撮像計画の担当", frozenset({REQUEST, PLAN}), "撮像計画・撮像依頼・計画ファイルの出力と採否の入力・年間撮像上限・衛星の障害情報"),
    Role("REQUESTER", "撮像を依頼する人", frozenset({REQUEST}), "撮像依頼の登録と、撮像予定（採否）の確認"),
    Role("OBSERVER", "観測記録の担当", frozenset({OBSERVE}), "観測記録の記録・通報・分担・引き継ぎ・分類体系の編集・AI結果の取り込み"),
    Role("INTERPRETER", "画像判読", frozenset({OBSERVE}), "画像補正・影から高さ・スペクトルで判読して観測記録へ"),
    Role("TRAINER", "AI 学習の担当", frozenset({TRAIN}), "教師データセットの作成・アノテーション変換・AI 学習"),
    Role("ADMIN", "管理者", frozenset({ADMIN}), "接続と表の点検・監査ログ・設定ファイルの切り替え・ローカル記録の消去など"),
)
BY_CODE = {r.code: r for r in ROLES}
BY_NAME = {r.name: r for r in ROLES}

#: What each permission is called (in the messages: 「…の機能」).
PERMISSION_LABELS = {REQUEST: "撮像依頼", PLAN: "撮像計画", OBSERVE: "観測記録", TRAIN: "AI 学習", ADMIN: "管理"}

#: Features some roles only use: menu entry text or a dock's key → the permissions, any of which allows it.
FEATURES: dict[str, frozenset] = {
    # 撮像計画・撮像依頼
    "撮像計画ファイル出力": frozenset({PLAN}),
    "年間撮像上限…": frozenset({PLAN}),
    #: 衛星の障害情報: the list for those who ask for pictures too (why theirs wait); 新規・編集・復旧・削除 for those who plan.
    "衛星の障害情報…": frozenset({REQUEST, PLAN}),
    "outages.edit": frozenset({PLAN}),
    #: 撮像予定: 採用・不採用・不明に戻す・結果ファイルの取り込み・要求外を追加・削除.
    "schedule.answer": frozenset({PLAN}),
    #: 撮像依頼: 新規・編集・削除・一括編集・レイヤーから取り込み, and 「臨時依頼にする」 of the plans.
    "requests.edit": frozenset({REQUEST, PLAN}),
    # 観測記録
    "観測記録": frozenset({OBSERVE}),
    "自分の記録…": frozenset({OBSERVE}),
    "通報文の作成…": frozenset({OBSERVE}),
    "通報記録の確認…": frozenset({OBSERVE}),
    "観測記録のエクスポート…": frozenset({OBSERVE}),
    "観測記録の一括取込み…": frozenset({OBSERVE}),
    "分担…": frozenset({OBSERVE}),
    "引き継ぎメモ…": frozenset({OBSERVE}),
    "検出結果を DB へ転記…": frozenset({OBSERVE}),
    #: The classes of the records: changed by those who record (also from 観測記録's 「詳細」), not the administrators' only.
    "分類体系の編集": frozenset({OBSERVE}),
    "AI結果": frozenset({OBSERVE, TRAIN}),
    #: AI結果: 観測記録へ取り込み・正・誤検出.
    "ai_results.import": frozenset({OBSERVE}),
    #: 画像カタログ: 「ファイルなしで記録」.
    "catalog.record": frozenset({OBSERVE}),
    # AI 学習
    "教師データセットの作成…": frozenset({TRAIN}),
    "アノテーション変換（点→矩形）…": frozenset({TRAIN}),
    "アノテーション変換（教師データセット）…": frozenset({TRAIN}),
    "AI 学習（物体検出）…": frozenset({TRAIN}),
    #: AI結果: 「確認結果で追加学習…」.
    "ai_results.train": frozenset({TRAIN}),
    # 管理
    #: 観測記録: 「ローカル記録の消去…」 (cannot be undone).
    "observations.clear": frozenset({ADMIN}),
    "接続と表の点検…": frozenset({ADMIN}),
    "監査ログ…": frozenset({ADMIN}),
    "Python API リファレンス…": frozenset({ADMIN}),
    "サーバ処理…": frozenset({ADMIN}),
    #: ArcGIS Web マップとして保存: 「ポータルに保存…」 (the JSON file is for everyone).
    "webmap.portal": frozenset({ADMIN}),
    #: 設定…: the settings file's field (once one is set: choosing the first one is for everyone).
    "settings.config": frozenset({ADMIN}),
}

#: The Processing tools that are features too: algorithm name → feature.
ALGORITHM_FEATURES = {
    "train_detector": "AI 学習（物体検出）…",
    "make_dataset": "教師データセットの作成…",
    "points_to_boxes": "アノテーション変換（点→矩形）…",
    "dataset_points_to_boxes": "アノテーション変換（教師データセット）…",
}

#: The attributes of TBL_USER_ROLE by role (one row per role of a person).
DEFAULT_USER_ROLE_FIELDS = {"user": "USER_NAME", "role": "ROLE_NAME"}
#: More rows than this for one person is an error (a person has a handful).
LIMIT = 1000


def clean_codes(codes: Iterable[Any]) -> list[str]:
    """The known role codes among `codes` (any case), each once, in ROLES order."""
    wanted = {str(c).strip().upper() for c in codes if isinstance(c, str)}
    return [r.code for r in ROLES if r.code in wanted]


def names_of(codes: Iterable[str]) -> list[str]:
    return [BY_CODE[c].name for c in clean_codes(codes)]


def codes_of(names: Iterable[str]) -> list[str]:
    """The codes of roles named (unknown names left out), in ROLES order."""
    return clean_codes(BY_NAME[n].code for n in names if n in BY_NAME)


def permissions_of(codes: Iterable[str]) -> frozenset:
    out: set = set()
    for c in clean_codes(codes):
        out |= BY_CODE[c].permissions
    return frozenset(out)


def allows(feature: str, codes: Iterable[str]) -> bool:
    """
    Whether the person with roles `codes` sees `feature`: a feature not listed
    always; without any role, everything but the administrators' features.
    """
    needed = FEATURES.get(feature)
    if needed is None:
        return True
    codes = clean_codes(codes)
    if not codes:
        return bool(needed - {ADMIN})
    return bool(needed & permissions_of(codes))


def needed_roles(feature: str) -> list[str]:
    """The names of the roles that give `feature` (for 「担当『…』の機能です」)."""
    needed = FEATURES.get(feature) or frozenset()
    return [r.name for r in ROLES if r.permissions & needed]


def refusal(feature: str) -> str:
    """Why a feature is not offered: the roles to tick for it."""
    names = needed_roles(feature)
    return (f"担当「{'」「'.join(names)}」の機能です（メニュー「担当と個人設定…」で担当を選ぶと使えます）" if names else "")


# ---- the settings file and the table --------------------------------------------------------

def user_roles_settings_of(value: Any, problems: list[str]) -> Optional[EsriSource]:
    """`userRoles`: the table of the people's roles (URL or `{ "url", "fields", "where", "token" }`)."""
    return esri_source_of(value, "userRoles", DEFAULT_USER_ROLE_FIELDS, problems, editable=True)


def user_where(source: EsriSource, user: str) -> str:
    """The rows of `user` (compared in capitals, as Windows user names are), within the settings file's own `where`."""
    mine = f"UPPER({source.fields['user']}) = {quote(user.strip().upper())}"
    return f"({source.where}) AND ({mine})" if source.where else mine


def _rows(client: EsriClient, source: EsriSource, user: str, token: Optional[str]) -> tuple[list[tuple[Optional[int], str]], Any]:
    if not user.strip():
        raise EsriError("利用者名が分からない（環境変数 USERNAME がない。設定ファイルの editTracking で指定）")
    info = client.layer(source.url, token or source.token, refresh=True)
    missing = [source.fields[k] for k in ("user", "role") if source.fields[k] not in info.field_types]
    if missing:
        raise EsriError(f"担当の表に属性 {'・'.join(missing)} なし")
    oid_field = info.object_id_field
    features = client.query_all(source.url, user_where(source, user), token or source.token,
                                out_fields=f"{oid_field},{source.fields['role']}", geometry=False, limit=LIMIT)
    rows = []
    for f in features:
        a = f.get("attributes") or {}
        oid = a.get(oid_field)
        rows.append((int(oid) if isinstance(oid, (int, float)) and not isinstance(oid, bool) else None,
                     str(a.get(source.fields["role"]) or "").strip().upper()))
    return rows, info


def load_user_roles(client: EsriClient, source: EsriSource, user: str, token: Optional[str] = None) -> list[str]:
    """`user`'s roles as the table has them (unknown values left out)."""
    rows, _info = _rows(client, source, user, token)
    return clean_codes(code for _oid, code in rows)


def save_user_roles(client: EsriClient, source: EsriSource, user: str, codes: Sequence[str],
                    token: Optional[str] = None) -> list[str]:
    """
    Makes the table hold exactly `codes` for `user`: the rows of roles no
    longer chosen (and repeated ones) deleted, the new ones added. Rows of
    values the plugin does not know are left as they are (another version's).
    """
    codes = clean_codes(codes)
    rows, info = _rows(client, source, user, token)
    token = token or source.token
    kept: set = set()
    deletes = []
    for oid, code in rows:
        if code not in BY_CODE:
            continue
        if code in codes and code not in kept:
            kept.add(code)
        elif oid is not None:
            deletes.append(oid)
    types, lengths = info.field_types, info.field_lengths
    f = source.fields

    def value(name: str, v: str) -> Any:
        if lengths.get(name):
            v = v[:lengths[name]]
        return fit_value(v, types.get(name))

    adds = [{"attributes": {f["user"]: value(f["user"], user.strip()), f["role"]: value(f["role"], code)}}
            for code in codes if code not in kept]
    if not adds and not deletes:
        return codes
    result = client.apply_edits(source.url, adds=adds, deletes=deletes, token=token)
    failures = result.failures()
    if failures:
        raise EsriError("担当の表への保存失敗（" + "、".join(failures[:3]) + "）")
    return codes
