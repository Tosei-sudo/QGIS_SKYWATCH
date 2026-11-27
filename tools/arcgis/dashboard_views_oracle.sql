-- =============================================================================================
-- MIKAGE ダッシュボード用のビュー（Oracle 19c 以降・ArcSDE（エンタープライズ ジオデータベース）・ST_Geometry）
--
-- ArcGIS Dashboards だけでは数えられないもの（依頼単位の件数・撮像の年間上限の残り・定点の
-- 未撮像の日数・モノごとの最新の記録・分担ごとの記録の件数・地名ごとの平常との差）を、
-- プラグインが書くテーブルから作る。作り方・ダッシュボードの構成は docs/arcgis-dashboards.md。
--
-- 地名（7 の地名の列と 10〜13）は、観測記録の NAMED_AREA_OBJECTID（属性ルールが入れる、記録を含む
-- いちばん小さい地名の OBJECTID。docs/arcgis-setup.md「属性ルール」）で TBL_NAMED_AREA を引く。
-- 空間の結合はしない。ルールを入れる前の記録は地名なし（地名のビューでは数えない）。
--
-- 実行: データ所有者（create_tables.py でテーブルを作ったユーザー、例 MIKAGE）で接続し、
--       SQL*Plus か SQL Developer でこのファイルを流す（何度流してもよい: CREATE OR REPLACE）。
--       ビューはビューを読むので、上から順に作る。
--
-- 決まりごと:
--   * 名前は VW_ で始め、ほかは TBL_ と同じ（大文字・_ 区切り・30 文字以内）。書き込めない表だと
--     名前で分かるようにするため
--   * 日時は UTC で保存されている前提（フィーチャサービスの日付のタイムゾーンが既定の UTC）。
--     日本時間は UTC+9（夏時間なし）。「日」の列（..._DATE）は、その日の日本時間 0 時を UTC で持つ
--     （出力履歴の TARGET_DATE と同じ）。ダッシュボードはブラウザーの日本時間で表示するので、
--     そのまま日本の日になる
--   * 形状のあるビューは、形状と同じテーブルの OBJECTID をそのまま OBJECTID にする（ArcGIS の決まり）。
--     形状のないビューの OBJECTID も、元の行の OBJECTID から作る（重ならず、更新しても変わらない）
--   * 「今日」はデータベースの時計（SYSTIMESTAMP を UTC にして +9 時間）
--   * 列の型: 日付は DATE、数は NUMBER（ArcGIS では Double か Integer）
--   * 文字コード: データベースは AL32UTF8 の前提。文字の列（Oracle のジオデータベースの既定で NVARCHAR2）を
--     文字の定数と CASE・LISTAGG で混ぜるところは TO_CHAR で揃えている（ORA-12704 を避けるため）
--
-- 元のテーブルを形状なしで作った場合（create_tables.py --geometry TBL_REQUEST_HISTORY=NONE など）は、
-- そのビューの SHAPE の行を消す。属性が足りない（古い create_tables.py で作った）ときは、
-- create_tables.py をもう一度実行して属性を足してから流す。
--
-- 権限（閲覧用のロール・ユーザーに）:
--   GRANT SELECT ON VW_PLAN_HISTORY TO MIKAGE_VIEWER;  ... 下の 13 本すべて
-- 消すとき:
--   DROP VIEW VW_NAMED_AREA_STATUS; DROP VIEW VW_NAMED_AREA_THING; DROP VIEW VW_NAMED_AREA_WEEK;
--   DROP VIEW VW_ALERT_LOG;
--   DROP VIEW VW_OBS_IMAGE_WORK; DROP VIEW VW_OBS_THING; DROP VIEW VW_OBS_RECORD;
--   DROP VIEW VW_PLAN_FIXED_POINT; DROP VIEW VW_PLAN_QUOTA_MONTH; DROP VIEW VW_PLAN_QUOTA_USAGE;
--   DROP VIEW VW_PLAN_SHOT; DROP VIEW VW_PLAN_REQUEST_DAY; DROP VIEW VW_PLAN_HISTORY;
-- =============================================================================================


-- ---------------------------------------------------------------------------------------------
-- 1. VW_PLAN_HISTORY（撮像予定の行）: 出力履歴 1 行 = 1 行・ポリゴン
--    プラグインの「撮像予定」ドックと同じ状態（PLAN_STATE: 採用・不明・不採用・見送り・要求外。
--    core/request/store.py の state_of）と、今日から何日先か（DAY_OFFSET: 0 = 今日、1 = 明日）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_HISTORY AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
shot AS (
    SELECT SHOT_ID, MIN(PASS_TIME) AS PASS_TIME, MAX(REQ_COUNT) AS REQ_COUNT
    FROM TBL_REQUEST_SHOT
    WHERE SHOT_ID IS NOT NULL
    GROUP BY SHOT_ID
)
SELECT
    h.OBJECTID,
    h.SHAPE,
    h.TARGET_DAY,
    h.JST_DAY - 9 / 24                                         AS TARGET_DATE,
    h.JST_DAY - c.TODAY                                        AS DAY_OFFSET,
    h.REQ_ID,
    h.REQ_NAME,
    h.MANAGEMENT_NO,
    h.REQUESTER,
    h.REQUEST_GROUP,
    NVL(h.REQUEST_GROUP, '（グループなし）')                   AS GROUP_LABEL,
    h.SYSTEM,
    h.SATELLITE,
    h.PRIORITY_OUT,
    h.CLOUD,
    h.SCENES,
    NVL(h.RESULT, '採用')                                      AS RESULT,
    h.ADOPTION,
    h.CAPTURED,
    CASE
        WHEN h.RESULT = '要求外'                 THEN '要求外'
        WHEN NVL(h.RESULT, '採用') <> '採用'     THEN '見送り'
        WHEN h.CAPTURED = '撮像済み'             THEN '採用'
        WHEN h.ADOPTION IN ('採用', '不採用')    THEN TO_CHAR(h.ADOPTION)
        ELSE '不明'
    END                                                        AS PLAN_STATE,
    CASE
        WHEN h.RESULT = '要求外' THEN 1
        WHEN NVL(h.RESULT, '採用') = '採用' AND (h.CAPTURED = '撮像済み' OR h.ADOPTION = '採用') THEN 1
        ELSE 0
    END                                                        AS IN_PLAN,
    h.REASON,
    h.ADOPTION_REASON,
    h.PLANNED_AT,
    h.EXTERNAL_ID,
    h.ADOPTION_SOURCE,
    h.IMAGE_ID,
    h.MERGED_INTO,
    h.SHOT_ID,
    h.OCCURRENCE,
    h.PART_NO,
    h.LINK_ID,
    h.RUN_ID,
    h.RUN_DATE,
    h.OUTPUT_FILE,
    s.PASS_TIME,
    s.REQ_COUNT                                                AS SHOT_REQ_COUNT
FROM (
    SELECT x.*, TO_DATE(SUBSTR(x.TARGET_DAY, 1, 10) DEFAULT NULL ON CONVERSION ERROR, 'YYYY-MM-DD') AS JST_DAY
    FROM TBL_REQUEST_HISTORY x
) h
CROSS JOIN clock c
LEFT JOIN shot s ON s.SHOT_ID = h.SHOT_ID;


-- ---------------------------------------------------------------------------------------------
-- 2. VW_PLAN_REQUEST_DAY（依頼 × 対象日）: 1 依頼・1 日 1 行・形状なし
--    1 日に何回も撮る依頼（OCCURRENCE）・衛星で分担した依頼（PART_NO）の行を 1 つにまとめ、
--    「依頼が何件計画に入ったか」を数えられるようにする。要求外は 1 計画 1 行のまま。
--    PLAN_STATE は行の中でいちばん良いもの（要求外 > 採用 > 不明 > 不採用 > 見送り）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_REQUEST_DAY AS
SELECT
    CAST(MIN(v.OBJECTID) AS NUMBER(38))                        AS OBJECTID,
    v.TARGET_DAY,
    MIN(v.TARGET_DATE)                                         AS TARGET_DATE,
    MIN(v.DAY_OFFSET)                                          AS DAY_OFFSET,
    MAX(v.REQ_ID)                                              AS REQ_ID,
    MAX(v.REQ_NAME)                                            AS REQ_NAME,
    MAX(v.MANAGEMENT_NO)                                       AS MANAGEMENT_NO,
    MAX(v.REQUESTER)                                           AS REQUESTER,
    MAX(v.REQUEST_GROUP)                                       AS REQUEST_GROUP,
    NVL(MAX(v.REQUEST_GROUP), '（グループなし）')              AS GROUP_LABEL,
    DECODE(MIN(DECODE(v.PLAN_STATE, '要求外', 0, '採用', 1, '不明', 2, '不採用', 3, 4)),
           0, '要求外', 1, '採用', 2, '不明', 3, '不採用', '見送り') AS PLAN_STATE,
    MAX(v.IN_PLAN)                                             AS IN_PLAN,
    CASE
        WHEN SUM(CASE WHEN v.CAPTURED = '撮像済み' THEN 1 ELSE 0 END) > 0 THEN '撮像済み'
        WHEN SUM(CASE WHEN v.RESULT IN ('採用', '要求外') THEN 1 ELSE 0 END) = 0 THEN NULL
        WHEN SUM(CASE WHEN v.RESULT IN ('採用', '要求外') AND NVL(v.CAPTURED, '未判定') <> '未撮像' THEN 1 ELSE 0 END) = 0
            THEN '未撮像'
        ELSE '未判定'
    END                                                        AS CAPTURED,
    COUNT(*)                                                   AS N_ROWS,
    SUM(v.IN_PLAN)                                             AS N_IN_PLAN,
    SUM(CASE WHEN v.CAPTURED = '撮像済み' THEN 1 ELSE 0 END)   AS N_CAPTURED,
    LISTAGG(DISTINCT TO_CHAR(v.SYSTEM), '、' ON OVERFLOW TRUNCATE '…' WITHOUT COUNT)
        WITHIN GROUP (ORDER BY TO_CHAR(v.SYSTEM))                     AS SYSTEMS,
    LISTAGG(DISTINCT TO_CHAR(v.SATELLITE), '、' ON OVERFLOW TRUNCATE '…' WITHOUT COUNT)
        WITHIN GROUP (ORDER BY TO_CHAR(v.SATELLITE))                  AS SATELLITES,
    MIN(v.PLANNED_AT)                                          AS PLANNED_AT,
    MAX(v.ADOPTION_REASON)                                     AS ADOPTION_REASON,
    MAX(v.REASON)                                              AS REASON,
    MAX(v.IMAGE_ID)                                            AS IMAGE_ID
FROM VW_PLAN_HISTORY v
GROUP BY
    v.TARGET_DAY,
    CASE WHEN v.RESULT = '要求外' OR v.REQ_ID IS NULL THEN 'OID:' || v.OBJECTID ELSE TO_CHAR(v.REQ_ID) END;


-- ---------------------------------------------------------------------------------------------
-- 3. VW_PLAN_SHOT（撮像）: 撮像の範囲 1 行 = 1 行・ポリゴン
--    年間撮像上限の数え方（core/request/quota.py の shot_state）での状態 QUOTA_STATE:
--      撮像済み（数える）・見込み（出力して結果待ち。残りから引く）・未撮像・不採用・履歴なし（数えない）。
--    SENSOR_KEY などは上限の照合用（英数字だけ・小文字。プラグインの plain() と同じ考え方）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_SHOT AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
r AS (
    SELECT
        SHOT_ID,
        COUNT(*)                                                                         AS N_ROWS,
        SUM(CASE WHEN RESULT = '採用' THEN 1 ELSE 0 END)                                 AS N_TAKEN,
        SUM(CASE WHEN RESULT = '採用' AND CAPTURED = '撮像済み' THEN 1 ELSE 0 END)       AS N_CAPTURED,
        SUM(CASE WHEN RESULT = '採用' AND CAPTURED = '未撮像' THEN 1 ELSE 0 END)         AS N_MISSED,
        SUM(CASE WHEN RESULT = '採用' AND (CAPTURED = '未撮像' OR ADOPTION = '不採用') THEN 1 ELSE 0 END) AS N_GONE
    FROM TBL_REQUEST_HISTORY
    WHERE SHOT_ID IS NOT NULL
    GROUP BY SHOT_ID
)
SELECT
    s.OBJECTID,
    s.SHAPE,
    s.SHOT_ID,
    s.RUN_ID,
    s.TARGET_DAY,
    s.JST_DAY - 9 / 24                                         AS TARGET_DATE,
    s.JST_DAY - c.TODAY                                        AS DAY_OFFSET,
    s.SYSTEM,
    s.SATELLITE,
    s.SENSOR,
    s.SENSOR_MODE,
    NVL(s.SENSOR, s.SATELLITE) || '（' || NVL(s.SENSOR_MODE, 'モードなし') || '）' AS SENSOR_LABEL,
    s.PASS_TIME,
    s.ORBIT_DIR,
    s.LOOK_SIDE,
    s.OFF_NADIR,
    s.INCIDENCE,
    s.REQ_ID,
    s.REQ_COUNT,
    s.REQ_NAMES,
    s.CAPTURED,
    s.IMAGE_ID,
    GREATEST(NVL(s.SCENES, 0), 0)                              AS SCENES,
    LEAST(GREATEST(NVL(s.SCENES, 0), 0), GREATEST(NVL(s.UNCOUNTED_SCENES, 0), 0)) AS UNCOUNTED_SCENES,
    s.UNCOUNTED_REASON,
    CASE
        WHEN s.CAPTURED = '撮像済み'       THEN '撮像済み'
        WHEN s.CAPTURED = '未撮像'         THEN '未撮像'
        WHEN r.N_ROWS IS NULL              THEN '履歴なし'
        WHEN r.N_TAKEN = 0                 THEN '不採用'
        WHEN r.N_CAPTURED > 0              THEN '撮像済み'
        WHEN r.N_GONE = r.N_TAKEN          THEN CASE WHEN r.N_MISSED > 0 THEN '未撮像' ELSE '不採用' END
        ELSE '見込み'
    END                                                        AS QUOTA_STATE,
    LOWER(REGEXP_REPLACE(s.SENSOR, '[^[:alnum:]]', ''))        AS SENSOR_KEY,
    LOWER(REGEXP_REPLACE(s.SATELLITE, '[^[:alnum:]]', ''))     AS SATELLITE_KEY,
    LOWER(REGEXP_REPLACE(s.SENSOR_MODE, '[^[:alnum:]]', ''))   AS MODE_KEY
FROM (
    SELECT x.*, TO_DATE(SUBSTR(x.TARGET_DAY, 1, 10) DEFAULT NULL ON CONVERSION ERROR, 'YYYY-MM-DD') AS JST_DAY
    FROM TBL_REQUEST_SHOT x
) s
CROSS JOIN clock c
LEFT JOIN r ON r.SHOT_ID = s.SHOT_ID;


-- ---------------------------------------------------------------------------------------------
-- 4. VW_PLAN_QUOTA_USAGE（年間撮像上限の今年度）: 上限 1 行 = 1 行・形状なし
--    今日が入る年（YEAR_START_MONTH の 1 日から 1 年）の 消費・見込み・残り・ペース。
--    プラグインの「年間撮像上限」の画面（core/request/quota.py の usage_of・Usage）と同じ数え方:
--      消費 = 当初の消費枚数（COUNT_FROM がこの年に入るときだけ）+ 撮像済みの枚数 − 数えない枚数
--      見込み = 出力して結果がまだの撮像の枚数（− 数えない枚数）。残り = 上限 − 消費 − 見込み
--      ペース = 上限に対する（消費 + 見込み）の割合 ÷ 年の過ぎた割合（1/12 以上）。1.1 を超えたら超過
--    撮像は SENSOR（なければ衛星名）と SENSOR_MODE（上限の行が空なら全モード）で照合する。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_QUOTA_USAGE AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
q AS (
    SELECT
        q.OBJECTID,
        q.SENSOR,
        q.SENSOR_MODE,
        q.ANNUAL_LIMIT,
        q.NOTE,
        NVL(q.INITIAL_USED, 0)                                       AS INITIAL_RAW,
        TRUNC(CAST(q.COUNT_FROM AS DATE) + 9 / 24)                   AS COUNT_FROM_DAY,
        CASE WHEN q.YEAR_START_MONTH BETWEEN 1 AND 12 THEN q.YEAR_START_MONTH ELSE 1 END AS START_MONTH,
        LOWER(REGEXP_REPLACE(q.SENSOR, '[^[:alnum:]]', ''))          AS SENSOR_KEY,
        LOWER(REGEXP_REPLACE(q.SENSOR_MODE, '[^[:alnum:]]', ''))     AS MODE_KEY,
        c.TODAY
    FROM TBL_IMAGING_QUOTA q
    CROSS JOIN clock c
    WHERE q.SENSOR IS NOT NULL
),
w AS (
    SELECT
        q.*,
        ADD_MONTHS(TRUNC(q.TODAY, 'YYYY'),
                   q.START_MONTH - 1 - CASE WHEN EXTRACT(MONTH FROM q.TODAY) >= q.START_MONTH THEN 0 ELSE 12 END)
                                                                     AS YEAR_FIRST
    FROM q
),
y AS (
    SELECT
        w.*,
        ADD_MONTHS(w.YEAR_FIRST, 12)                                 AS YEAR_END,
        CASE WHEN w.COUNT_FROM_DAY >= w.YEAR_FIRST AND w.COUNT_FROM_DAY < ADD_MONTHS(w.YEAR_FIRST, 12)
             THEN w.COUNT_FROM_DAY END                               AS COUNT_START
    FROM w
),
u AS (
    SELECT
        y.OBJECTID,
        SUM(CASE WHEN s.QUOTA_STATE = '撮像済み' THEN s.SCENES - s.UNCOUNTED_SCENES ELSE 0 END) AS USED,
        SUM(CASE WHEN s.QUOTA_STATE = '撮像済み' THEN s.UNCOUNTED_SCENES ELSE 0 END)            AS UNCOUNTED,
        SUM(CASE WHEN s.QUOTA_STATE = '見込み' THEN s.SCENES - s.UNCOUNTED_SCENES ELSE 0 END)   AS PENDING,
        COUNT(CASE WHEN s.QUOTA_STATE = '撮像済み' THEN 1 END)                                  AS N_USED_SHOTS,
        COUNT(CASE WHEN s.QUOTA_STATE = '見込み' THEN 1 END)                                    AS N_PENDING_SHOTS
    FROM y
    LEFT JOIN VW_PLAN_SHOT s
        ON y.SENSOR_KEY IN (s.SENSOR_KEY, s.SATELLITE_KEY)
       AND (y.MODE_KEY IS NULL OR y.MODE_KEY = s.MODE_KEY)
       AND s.TARGET_DATE >= y.YEAR_FIRST - 9 / 24
       AND s.TARGET_DATE <  y.YEAR_END - 9 / 24
       AND (y.COUNT_START IS NULL OR s.TARGET_DATE >= y.COUNT_START - 9 / 24)
    GROUP BY y.OBJECTID
),
n AS (
    SELECT
        y.OBJECTID,
        y.SENSOR,
        y.SENSOR_MODE,
        y.SENSOR || '（' || NVL(y.SENSOR_MODE, '全モード') || '）'    AS QUOTA_LABEL,
        y.ANNUAL_LIMIT,
        y.START_MONTH,
        y.NOTE,
        y.TODAY,
        y.YEAR_FIRST,
        y.YEAR_END,
        CASE WHEN y.COUNT_START IS NOT NULL THEN y.INITIAL_RAW ELSE 0 END AS INITIAL_USED,
        NVL(u.USED, 0)                                               AS USED_SCENES,
        NVL(u.UNCOUNTED, 0)                                          AS UNCOUNTED_SCENES,
        NVL(u.PENDING, 0)                                            AS PENDING_SCENES,
        NVL(u.N_USED_SHOTS, 0)                                       AS N_USED_SHOTS,
        NVL(u.N_PENDING_SHOTS, 0)                                    AS N_PENDING_SHOTS,
        LEAST(1, GREATEST(0, (y.TODAY - y.YEAR_FIRST + 1) / (y.YEAR_END - y.YEAR_FIRST))) AS ELAPSED
    FROM y
    LEFT JOIN u ON u.OBJECTID = y.OBJECTID
),
m AS (
    SELECT
        n.*,
        n.INITIAL_USED + n.USED_SCENES                               AS CONSUMED,
        n.ANNUAL_LIMIT - (n.INITIAL_USED + n.USED_SCENES) - n.PENDING_SCENES AS REMAINING,
        (n.INITIAL_USED + n.USED_SCENES + n.PENDING_SCENES) / NULLIF(n.ANNUAL_LIMIT, 0) AS USED_SHARE
    FROM n
)
SELECT
    m.OBJECTID,
    m.SENSOR,
    m.SENSOR_MODE,
    m.QUOTA_LABEL,
    m.ANNUAL_LIMIT,
    m.START_MONTH,
    m.YEAR_FIRST - 9 / 24                                            AS YEAR_START_DATE,
    m.YEAR_END - 1 - 9 / 24                                          AS YEAR_LAST_DATE,
    TO_CHAR(m.YEAR_FIRST, 'FMYYYY/MM/DD') || '〜' || TO_CHAR(m.YEAR_END - 1, 'FMYYYY/MM/DD') AS YEAR_TEXT,
    m.INITIAL_USED,
    m.USED_SCENES,
    m.UNCOUNTED_SCENES,
    m.PENDING_SCENES,
    m.N_USED_SHOTS,
    m.N_PENDING_SHOTS,
    m.CONSUMED,
    m.REMAINING,
    ROUND(100 * m.USED_SHARE, 1)                                     AS USED_PCT,
    ROUND(100 * m.ELAPSED, 1)                                        AS YEAR_PCT,
    ROUND(m.USED_SHARE / GREATEST(m.ELAPSED, 1 / 12), 2)             AS PACE,
    CASE
        WHEN m.ANNUAL_LIMIT IS NULL                                  THEN '上限なし'
        WHEN m.REMAINING < 0                                         THEN '上限超過'
        WHEN m.REMAINING <= m.ANNUAL_LIMIT * 0.1                     THEN '残りわずか'
        WHEN m.USED_SHARE / GREATEST(m.ELAPSED, 1 / 12) > 1.1        THEN 'ペース超過'
        ELSE '順調'
    END                                                              AS QUOTA_STATE,
    CASE
        WHEN m.ANNUAL_LIMIT > 0 AND m.CONSUMED + m.PENDING_SCENES > 0
         AND m.YEAR_FIRST + CEIL(m.ANNUAL_LIMIT / ((m.CONSUMED + m.PENDING_SCENES)
                                 / GREATEST(1, m.TODAY - m.YEAR_FIRST + 1))) - 1 < m.YEAR_END
        THEN m.YEAR_FIRST + CEIL(m.ANNUAL_LIMIT / ((m.CONSUMED + m.PENDING_SCENES)
                                 / GREATEST(1, m.TODAY - m.YEAR_FIRST + 1))) - 1 - 9 / 24
    END                                                              AS RUNS_OUT_DATE,
    m.NOTE
FROM m;


-- ---------------------------------------------------------------------------------------------
-- 5. VW_PLAN_QUOTA_MONTH（年間撮像上限の月ごと）: 上限 × 今年度の 12 か月・形状なし
--    月ごとの消費と、累計（CUM_CONSUMED: 当初の消費枚数から始まる。先の月は空）と、
--    1 年を均等に使ったときの線（PACE_LINE）。系列グラフ（フィーチャごと）でそのまま描ける。
--    OBJECTID = 上限の OBJECTID × 100 + 月の番号（1〜12）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_QUOTA_MONTH AS
WITH months AS (
    SELECT LEVEL AS MONTH_NO FROM DUAL CONNECT BY LEVEL <= 12
),
qm AS (
    SELECT
        q.OBJECTID                                                   AS QUOTA_OBJECTID,
        q.QUOTA_LABEL,
        q.SENSOR,
        q.SENSOR_MODE,
        q.ANNUAL_LIMIT,
        q.INITIAL_USED,
        k.MONTH_NO,
        q.YEAR_START_DATE + 9 / 24                                   AS YEAR_FIRST,
        ADD_MONTHS(q.YEAR_START_DATE + 9 / 24, k.MONTH_NO - 1)       AS MONTH_FIRST,
        ADD_MONTHS(q.YEAR_START_DATE + 9 / 24, k.MONTH_NO)           AS MONTH_END,
        TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24)  AS TODAY,
        LOWER(REGEXP_REPLACE(q.SENSOR, '[^[:alnum:]]', ''))          AS SENSOR_KEY,
        LOWER(REGEXP_REPLACE(q.SENSOR_MODE, '[^[:alnum:]]', ''))     AS MODE_KEY,
        TRUNC(CAST(t.COUNT_FROM AS DATE) + 9 / 24)                   AS COUNT_FROM_DAY
    FROM VW_PLAN_QUOTA_USAGE q
    JOIN TBL_IMAGING_QUOTA t ON t.OBJECTID = q.OBJECTID
    CROSS JOIN months k
),
u AS (
    SELECT
        qm.QUOTA_OBJECTID,
        qm.MONTH_NO,
        SUM(CASE WHEN s.QUOTA_STATE = '撮像済み' THEN s.SCENES - s.UNCOUNTED_SCENES ELSE 0 END) AS USED,
        SUM(CASE WHEN s.QUOTA_STATE = '見込み' THEN s.SCENES - s.UNCOUNTED_SCENES ELSE 0 END)   AS PENDING
    FROM qm
    LEFT JOIN VW_PLAN_SHOT s
        ON qm.SENSOR_KEY IN (s.SENSOR_KEY, s.SATELLITE_KEY)
       AND (qm.MODE_KEY IS NULL OR qm.MODE_KEY = s.MODE_KEY)
       AND s.TARGET_DATE >= qm.MONTH_FIRST - 9 / 24
       AND s.TARGET_DATE <  qm.MONTH_END - 9 / 24
       AND (qm.COUNT_FROM_DAY IS NULL
            OR qm.COUNT_FROM_DAY < qm.YEAR_FIRST
            OR qm.COUNT_FROM_DAY >= ADD_MONTHS(qm.YEAR_FIRST, 12)
            OR s.TARGET_DATE >= qm.COUNT_FROM_DAY - 9 / 24)
    GROUP BY qm.QUOTA_OBJECTID, qm.MONTH_NO
)
SELECT
    CAST(qm.QUOTA_OBJECTID * 100 + qm.MONTH_NO AS NUMBER(38))        AS OBJECTID,
    qm.QUOTA_OBJECTID,
    qm.QUOTA_LABEL,
    qm.SENSOR,
    qm.SENSOR_MODE,
    qm.ANNUAL_LIMIT,
    qm.MONTH_NO,
    qm.MONTH_FIRST - 9 / 24                                          AS MONTH_DATE,
    TO_CHAR(qm.MONTH_FIRST, 'YYYY-MM')                               AS MONTH_TEXT,
    NVL(u.USED, 0)                                                   AS USED_SCENES,
    NVL(u.PENDING, 0)                                                AS PENDING_SCENES,
    CASE WHEN qm.MONTH_FIRST <= qm.TODAY THEN
        qm.INITIAL_USED + SUM(NVL(u.USED, 0)) OVER (PARTITION BY qm.QUOTA_OBJECTID ORDER BY qm.MONTH_NO)
    END                                                              AS CUM_CONSUMED,
    CASE WHEN qm.MONTH_FIRST <= qm.TODAY THEN
        qm.INITIAL_USED + SUM(NVL(u.USED, 0) + NVL(u.PENDING, 0)) OVER (PARTITION BY qm.QUOTA_OBJECTID ORDER BY qm.MONTH_NO)
    END                                                              AS CUM_WITH_PENDING,
    ROUND(qm.ANNUAL_LIMIT * (qm.MONTH_END - qm.YEAR_FIRST) / (ADD_MONTHS(qm.YEAR_FIRST, 12) - qm.YEAR_FIRST)) AS PACE_LINE,
    CASE WHEN qm.MONTH_FIRST > qm.TODAY THEN 1 ELSE 0 END            AS IS_FUTURE
FROM qm
LEFT JOIN u ON u.QUOTA_OBJECTID = qm.QUOTA_OBJECTID AND u.MONTH_NO = qm.MONTH_NO;


-- ---------------------------------------------------------------------------------------------
-- 6. VW_PLAN_FIXED_POINT（定点の撮れ具合）: 有効な定点の依頼 1 件 = 1 行・ポリゴン（台帳の範囲）
--    台帳は計画立案の材料なので属性は足さず、出力履歴から「最後に撮れた日」と直近 30 日を数える。
--    EVERY_DAYS: 繰り返し（REPEAT）から見た撮る間隔の目安（毎日 1、間隔:3 は 3、曜日:月水金 は 7/3）。
--    LATE_TURNS: 最後に撮れてから、間隔で数えて何回分あいたか（前日に撮れた毎日の依頼は 0）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_PLAN_FIXED_POINT AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY,
           CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE)                AS NOW_UTC
    FROM DUAL
),
h AS (
    SELECT v.REQ_ID, v.TARGET_DATE + 9 / 24 AS JST_DAY, v.PLAN_STATE, v.CAPTURED
    FROM VW_PLAN_HISTORY v
    WHERE v.REQ_ID IS NOT NULL AND v.RESULT <> '要求外'
),
x AS (
    SELECT
        h.REQ_ID,
        MAX(CASE WHEN h.CAPTURED = '撮像済み' THEN h.JST_DAY END)                         AS LAST_CAPTURED,
        MAX(CASE WHEN h.PLAN_STATE = '採用' THEN h.JST_DAY END)                           AS LAST_IN_PLAN,
        COUNT(DISTINCT CASE WHEN h.JST_DAY > c.TODAY - 30 AND h.JST_DAY <= c.TODAY
                             AND h.PLAN_STATE <> '見送り' THEN h.JST_DAY END)             AS HANDED_30,
        COUNT(DISTINCT CASE WHEN h.JST_DAY > c.TODAY - 30 AND h.JST_DAY <= c.TODAY
                             AND h.PLAN_STATE = '採用' THEN h.JST_DAY END)                AS IN_PLAN_30,
        COUNT(DISTINCT CASE WHEN h.JST_DAY > c.TODAY - 30 AND h.JST_DAY <= c.TODAY
                             AND h.PLAN_STATE = '不採用' THEN h.JST_DAY END)              AS DECLINED_30,
        COUNT(DISTINCT CASE WHEN h.JST_DAY > c.TODAY - 30 AND h.JST_DAY <= c.TODAY
                             AND h.PLAN_STATE = '見送り' THEN h.JST_DAY END)              AS HELD_30,
        COUNT(DISTINCT CASE WHEN h.JST_DAY > c.TODAY - 30 AND h.JST_DAY <= c.TODAY
                             AND h.CAPTURED = '撮像済み' THEN h.JST_DAY END)              AS CAPTURED_30
    FROM h
    CROSS JOIN clock c
    GROUP BY h.REQ_ID
),
r AS (
    SELECT
        q.*,
        CASE
            WHEN REGEXP_LIKE(q.REPEAT, '^\s*曜日\s*[:：]')
             AND LENGTH(REGEXP_REPLACE(REGEXP_REPLACE(q.REPEAT, '^\s*曜日\s*[:：]', ''), '[^月火水木金土日]', '')) > 0
                THEN ROUND(7 / LENGTH(REGEXP_REPLACE(REGEXP_REPLACE(q.REPEAT, '^\s*曜日\s*[:：]', ''),
                                                     '[^月火水木金土日]', '')), 2)
            WHEN REGEXP_LIKE(q.REPEAT, '^\s*間隔\s*[:：]\s*[0-9]+\s*$')
                THEN GREATEST(1, TO_NUMBER(REGEXP_SUBSTR(q.REPEAT, '[0-9]+')))
            ELSE 1
        END                                                                AS EVERY_DAYS,
        TRUNC(CAST(q.START_AT AS DATE) + 9 / 24)                           AS START_DAY
    FROM TBL_IMAGING_REQUEST q
    WHERE q.CATEGORY = '定点' AND NVL(q.STATUS, '有効') = '有効'
),
g AS (
    SELECT
        r.*,
        x.LAST_CAPTURED,
        x.LAST_IN_PLAN,
        x.HANDED_30, x.IN_PLAN_30, x.DECLINED_30, x.HELD_30, x.CAPTURED_30,
        c.TODAY,
        c.NOW_UTC,
        c.TODAY - NVL(x.LAST_CAPTURED, r.START_DAY)                        AS DAYS_SINCE
    FROM r
    CROSS JOIN clock c
    LEFT JOIN x ON x.REQ_ID = r.REQ_ID
)
SELECT
    g.OBJECTID,
    g.SHAPE,
    g.REQ_ID,
    g.REQ_NAME,
    g.MANAGEMENT_NO,
    g.REQUEST_GROUP,
    NVL(g.REQUEST_GROUP, '（グループなし）')                           AS GROUP_LABEL,
    g.REQUESTER,
    g.PRIORITY,
    g.KIND,
    g.REPEAT,
    g.START_AT,
    g.END_AT,
    CASE WHEN (g.START_AT IS NULL OR CAST(g.START_AT AS DATE) <= g.NOW_UTC)
          AND (g.END_AT IS NULL OR CAST(g.END_AT AS DATE) >= g.NOW_UTC) THEN 1 ELSE 0 END AS IN_PERIOD,
    g.EVERY_DAYS,
    g.LAST_CAPTURED - 9 / 24                                           AS LAST_CAPTURED_DATE,
    g.LAST_IN_PLAN - 9 / 24                                            AS LAST_IN_PLAN_DATE,
    g.DAYS_SINCE                                                       AS DAYS_SINCE_CAPTURED,
    CASE WHEN g.DAYS_SINCE IS NOT NULL
         THEN GREATEST(0, FLOOR(g.DAYS_SINCE / g.EVERY_DAYS) - 1) END  AS LATE_TURNS,
    CASE
        WHEN g.LAST_CAPTURED IS NULL                                   THEN 'まだ撮れていない'
        WHEN GREATEST(0, FLOOR(g.DAYS_SINCE / g.EVERY_DAYS) - 1) = 0  THEN '間隔どおり'
        WHEN GREATEST(0, FLOOR(g.DAYS_SINCE / g.EVERY_DAYS) - 1) <= 2 THEN '1〜2 回分あき'
        ELSE '3 回分以上あき'
    END                                                                AS GAP_CLASS,
    NVL(g.HANDED_30, 0)                                                AS HANDED_30,
    NVL(g.IN_PLAN_30, 0)                                               AS IN_PLAN_30,
    NVL(g.DECLINED_30, 0)                                              AS DECLINED_30,
    NVL(g.HELD_30, 0)                                                  AS HELD_30,
    NVL(g.CAPTURED_30, 0)                                              AS CAPTURED_30
FROM g;


-- ---------------------------------------------------------------------------------------------
-- 7. VW_OBS_RECORD（観測記録）: 削除していない観測記録 1 件 = 1 行・ポイント
--    分類のパス（船舶 > 貨物船 > コンテナ船）を大分類・2 段目・いちばん下に分け、
--    見た日（画像の撮像日時、なければ記録した日時。プラグインの「流れ」と同じ）と地名を付ける。
--    地名は観測記録の NAMED_AREA_OBJECTID（属性ルールで入れる）で TBL_NAMED_AREA を引く。
--    PLACE_STATE（地名の点検用）: 地名あり・地名なし（値が空: どの地名にも入らないか、ルールを入れる前の記録）・
--    地名が見つからない（値の OBJECTID の地名がない: 地名を入れ直した）・無効な地名（後から ACTIVE を 0 にした）。
--    不可（2026-11-19）: 雲などで見えなかった記録は UNSEEN_REASON に理由が入り PRESENT は空。PRESENT_LABEL は
--    「不可」、LOOKED は 0（見て在・消失を記録したものは 1）。TBL_OBSERVATION に UNSEEN_REASON を足してから作る。
--    衛星名（2026-11-20）: SATELLITE は画像を撮った衛星（画像カタログのセンサーの書き方。カタログ外の画像も）。
--    SATELLITE_LABEL は空なら「（衛星不明）」（足す前の記録。docs/arcgis-setup.md「前からある記録に衛星名を入れる」）。
--    数量（2026-11-24）: QUANTITY はその記録が表す見えたモノの数。在の記録は TBL_OBSERVATION の QUANTITY（空は 1:
--    1 つずつの記録と、足す前の記録）、消失・不可の記録は 0。台数・隻数などは COUNT(*) でなく SUM(QUANTITY) で数える。
--    TBL_OBSERVATION に QUANTITY を足してから作る（docs/arcgis-setup.md「観測記録に数量の列を足す」）。
--    出どころ（2026-11-27）: SOURCE は AI の結果から取り込んだ記録の出どころ（AI検出・外部AI）、人の記録は空。
--    SOURCE_LABEL は空なら「人」。AI_LAYER・AI_RESULT_ID でどの AI の結果（転記先・外部 AI の表の行）かが分かり、
--    AI_SCORE・AI_MODEL・AI_DETECTED_AT はその結果の確信度・モデル・検出日時、AI_CONTINUATION は継続の判定。
--    TBL_OBSERVATION に AI の列を足してから作る（docs/arcgis-setup.md「観測記録に AI の出どころの列を足す」）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_OBS_RECORD AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
)
SELECT
    o.OBJECTID,
    o.SHAPE,
    o.OBS_ID,
    o.OBJECT_ID,
    o.OBJECT_NAME,
    o.KIND,
    NVL(TRIM(REGEXP_SUBSTR(o.KIND, '[^>]+', 1, 1)), '（分類なし）')    AS ROOT_KIND,
    TRIM(REGEXP_SUBSTR(o.KIND, '[^>]+', 1, 2))                         AS KIND_2,
    TRIM(REGEXP_SUBSTR(o.KIND, '[^>]+$'))                              AS LEAF_KIND,
    o.ACTIVITY,
    o.PRESENT,
    CASE
        WHEN o.UNSEEN_REASON IS NOT NULL THEN '不可'
        WHEN o.PRESENT = 1               THEN '在'
        WHEN o.PRESENT = 0               THEN '消失'
    END                                                                AS PRESENT_LABEL,
    o.UNSEEN_REASON,
    CASE WHEN o.UNSEEN_REASON IS NULL THEN 1 ELSE 0 END               AS LOOKED,
    CASE WHEN o.UNSEEN_REASON IS NULL AND NVL(o.PRESENT, 1) <> 0
         THEN NVL(o.QUANTITY, 1) ELSE 0 END                            AS QUANTITY,
    o.CONFIDENCE,
    o.NOTE,
    o.LENGTH_M,
    o.WIDTH_M,
    o.HEADING,
    o.IMAGE_ID,
    o.IMAGE_NAME,
    o.IMAGE_TIME,
    o.SATELLITE,
    NVL(TO_CHAR(o.SATELLITE), '（衛星不明）')                          AS SATELLITE_LABEL,
    o.SOURCE,
    NVL(TO_CHAR(o.SOURCE), '人')                                       AS SOURCE_LABEL,
    o.AI_LAYER,
    o.AI_RESULT_ID,
    o.AI_SCORE,
    o.AI_MODEL,
    o.AI_DETECTED_AT,
    o.AI_CONTINUATION,
    o.SEEN_AT,
    o.JST_DAY - 9 / 24                                                 AS SEEN_DATE,
    TO_CHAR(o.JST_DAY, 'YYYY-MM')                                      AS SEEN_MONTH,
    c.TODAY - o.JST_DAY                                                AS DAYS_AGO,
    o.OBSERVER,
    o.CREATED_USER,
    o.CREATED_AT,
    o.UPDATED_AT,
    o.NAMED_AREA_OBJECTID,
    a.NAME                                                             AS PLACE_NAME,
    a.KIND                                                             AS PLACE_KIND,
    a.NAMED_AREA_ID,
    NVL(a.NAME, '（地名なし）')                                        AS PLACE_LABEL,
    CASE
        WHEN o.NAMED_AREA_OBJECTID IS NULL THEN '地名なし'
        WHEN a.OBJECTID IS NULL            THEN '地名が見つからない'
        WHEN a.ACTIVE = 0                  THEN '無効な地名'
        ELSE '地名あり'
    END                                                                AS PLACE_STATE
FROM (
    SELECT x.*,
           CAST(NVL(x.IMAGE_TIME, x.CREATED_AT) AS DATE)                    AS SEEN_AT,
           TRUNC(CAST(NVL(x.IMAGE_TIME, x.CREATED_AT) AS DATE) + 9 / 24)    AS JST_DAY
    FROM TBL_OBSERVATION x
    WHERE x.DELETED_AT IS NULL
) o
CROSS JOIN clock c
LEFT JOIN TBL_NAMED_AREA a ON a.OBJECTID = o.NAMED_AREA_OBJECTID;


-- ---------------------------------------------------------------------------------------------
-- 8. VW_OBS_THING（モノの台帳）: モノ（OBJECT_ID）1 つ = 1 行・ポイント（最後の記録の位置）
--    観測記録ドックにモノの一覧はない（一覧は開いている画像の記録だけ）ので、モノ全体を最後にいつ・どこで
--    見たかで並べる。見た日時は画像の撮像日時、なければ記録した日時（core/obs/flows.py の time_key）。
--    最後の記録 = 見た日時の新しい順（同じなら記録した日時・OBJECTID）。名前・分類は最後に書かれたもの
--    （プラグインは同じ系統でより細かい分類があればそれを使う。ここは最後に書かれたものだけ）。
--    IN_LAST_MONTH: 直近 1 か月（今日の 1 か月前の同じ日から）に見ていれば 1。
--    LAST_NAMED_AREA_OBJECTID: 最後の記録の地名（地名の台帳 VW_NAMED_AREA_THING・VW_NAMED_AREA_STATUS が使う）。
--    不可の記録（雲など、LOOKED = 0）は「最後の記録」にしない（見て記録した最後のもの。プラグインの流れの「最新」と同じ）。
--    LAST_UNSEEN_AT・LAST_UNSEEN_REASON: それより後に見えなかった画像があれば、その最後の日時と理由（なければ空）。
--    UNSEEN_COUNT: 不可の記録の数。
--    LAST_QUANTITY（2026-11-24）: 最後の記録の数量（まとめて記録したモノの数。1 つずつの記録は 1、最後が消失なら 0）。
--    MAX_QUANTITY: これまでの記録でいちばん多かった数量。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_OBS_THING AS
WITH clock AS (
    SELECT
        d.TODAY,
        TRUNC(ADD_MONTHS(d.TODAY, -1), 'MM')
            + LEAST(EXTRACT(DAY FROM d.TODAY), EXTRACT(DAY FROM LAST_DAY(ADD_MONTHS(d.TODAY, -1)))) - 1 AS MONTH_AGO
    FROM (SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL) d
),
r AS (
    SELECT
        v.OBJECTID,
        v.SHAPE,
        v.OBJECT_ID,
        v.SEEN_AT,
        v.SEEN_DATE,
        v.ACTIVITY,
        v.PRESENT,
        v.PRESENT_LABEL,
        v.QUANTITY,
        v.PLACE_NAME,
        v.PLACE_LABEL,
        v.NAMED_AREA_OBJECTID,
        v.NAMED_AREA_ID,
        v.IMAGE_ID,
        v.IMAGE_NAME,
        v.OBSERVER,
        ROW_NUMBER() OVER (PARTITION BY v.OBJECT_ID
                           ORDER BY v.LOOKED DESC, v.SEEN_AT DESC, v.CREATED_AT DESC, v.OBJECTID DESC) AS RN,
        MAX(v.QUANTITY) OVER (PARTITION BY v.OBJECT_ID)                                       AS MAX_QUANTITY,
        MAX(CASE WHEN v.LOOKED = 0 THEN v.SEEN_AT END) OVER (PARTITION BY v.OBJECT_ID)         AS UNSEEN_AT,
        FIRST_VALUE(CASE WHEN v.LOOKED = 0 THEN v.UNSEEN_REASON END IGNORE NULLS) OVER (
            PARTITION BY v.OBJECT_ID ORDER BY v.SEEN_AT DESC, v.CREATED_AT DESC, v.OBJECTID DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)                         AS UNSEEN_REASON,
        SUM(1 - v.LOOKED) OVER (PARTITION BY v.OBJECT_ID)                                     AS UNSEEN_COUNT,
        FIRST_VALUE(v.OBJECT_NAME IGNORE NULLS) OVER (
            PARTITION BY v.OBJECT_ID ORDER BY v.SEEN_AT DESC, v.CREATED_AT DESC, v.OBJECTID DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)                         AS LAST_NAME,
        FIRST_VALUE(v.KIND IGNORE NULLS) OVER (
            PARTITION BY v.OBJECT_ID ORDER BY v.SEEN_AT DESC, v.CREATED_AT DESC, v.OBJECTID DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)                         AS LAST_KIND,
        MIN(v.SEEN_AT) OVER (PARTITION BY v.OBJECT_ID)                                        AS FIRST_SEEN_AT,
        COUNT(*) OVER (PARTITION BY v.OBJECT_ID)                                              AS RECORD_COUNT,
        SUM(CASE WHEN v.PRESENT = 1 THEN 1 ELSE 0 END) OVER (PARTITION BY v.OBJECT_ID)        AS SEEN_COUNT,
        COUNT(DISTINCT v.IMAGE_ID) OVER (PARTITION BY v.OBJECT_ID)                            AS IMAGE_COUNT
    FROM VW_OBS_RECORD v
    WHERE v.OBJECT_ID IS NOT NULL
)
SELECT
    r.OBJECTID,
    r.SHAPE,
    r.OBJECT_ID,
    NVL(r.LAST_NAME, r.OBJECT_ID)                                      AS THING_NAME,
    r.LAST_KIND                                                        AS KIND,
    NVL(TRIM(REGEXP_SUBSTR(r.LAST_KIND, '[^>]+', 1, 1)), '（分類なし）') AS ROOT_KIND,
    TRIM(REGEXP_SUBSTR(r.LAST_KIND, '[^>]+$'))                         AS LEAF_KIND,
    r.ACTIVITY                                                         AS LAST_ACTIVITY,
    r.PRESENT                                                          AS LAST_PRESENT,
    r.PRESENT_LABEL                                                    AS LAST_PRESENT_LABEL,
    r.QUANTITY                                                         AS LAST_QUANTITY,
    r.MAX_QUANTITY,
    r.SEEN_AT                                                          AS LAST_SEEN_AT,
    r.SEEN_DATE                                                        AS LAST_SEEN_DATE,
    r.FIRST_SEEN_AT,
    TRUNC(r.FIRST_SEEN_AT + 9 / 24) - 9 / 24                           AS FIRST_SEEN_DATE,
    r.RECORD_COUNT,
    r.SEEN_COUNT,
    r.IMAGE_COUNT,
    r.PLACE_NAME                                                       AS LAST_PLACE_NAME,
    r.PLACE_LABEL                                                      AS LAST_PLACE_LABEL,
    r.NAMED_AREA_OBJECTID                                              AS LAST_NAMED_AREA_OBJECTID,
    r.NAMED_AREA_ID                                                    AS LAST_NAMED_AREA_ID,
    r.IMAGE_ID                                                         AS LAST_IMAGE_ID,
    r.IMAGE_NAME                                                       AS LAST_IMAGE_NAME,
    r.OBSERVER                                                         AS LAST_OBSERVER,
    CASE WHEN r.UNSEEN_AT > r.SEEN_AT THEN r.UNSEEN_AT END             AS LAST_UNSEEN_AT,
    CASE WHEN r.UNSEEN_AT > r.SEEN_AT THEN r.UNSEEN_REASON END         AS LAST_UNSEEN_REASON,
    r.UNSEEN_COUNT,
    c.TODAY - (r.SEEN_DATE + 9 / 24)                                   AS DAYS_SINCE_SEEN,
    CASE WHEN r.SEEN_DATE + 9 / 24 >= c.MONTH_AGO THEN 1 ELSE 0 END    AS IN_LAST_MONTH,
    CASE
        WHEN r.SEEN_DATE + 9 / 24 >= c.MONTH_AGO                 THEN '1 か月以内'
        WHEN r.SEEN_DATE + 9 / 24 >= ADD_MONTHS(c.TODAY, -3)     THEN '1〜3 か月前'
        WHEN r.SEEN_DATE + 9 / 24 >= ADD_MONTHS(c.TODAY, -12)    THEN '3〜12 か月前'
        ELSE '1 年以上前'
    END                                                                AS AGE_CLASS
FROM r
CROSS JOIN clock c
WHERE r.RN = 1;


-- ---------------------------------------------------------------------------------------------
-- 9. VW_OBS_IMAGE_WORK（判読の進み具合）: 分担 1 行 = 1 行・形状なし
--    分担（だれがどの画像を担当したか）に、その画像の記録・確認範囲の件数と、担当者が書いた記録の
--    件数（作成者 CREATED_USER か記録者 OBSERVER が担当者と同じ名前。大文字・小文字は区別しない）を付ける。
--    DONE_EMPTY: 完了なのに記録も確認範囲・不可範囲も 0 件（何もなかったのか、付け忘れかの確認用）。
--    IMAGE_AREAS は確認範囲、UNSEEN_AREAS は不可範囲（雲など。TBL_OBSERVATION_AREA の UNSEEN_REASON あり）の件数。
--    SATELLITE: その画像の記録・確認範囲・不可範囲に書かれた衛星名（どれにもなければ空）。衛星ごとの不可範囲の
--    割合（UNSEEN_AREAS を SATELLITE で集計）などに使う。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_OBS_IMAGE_WORK AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY,
           CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE)                AS NOW_UTC
    FROM DUAL
),
img AS (
    SELECT IMAGE_ID, COUNT(*) AS N, MAX(CREATED_AT) AS LAST_AT, MAX(TO_CHAR(SATELLITE)) AS SATELLITE
    FROM VW_OBS_RECORD
    WHERE IMAGE_ID IS NOT NULL
    GROUP BY IMAGE_ID
),
area AS (
    SELECT IMAGE_ID,
           SUM(CASE WHEN UNSEEN_REASON IS NULL THEN 1 ELSE 0 END)     AS N,
           SUM(CASE WHEN UNSEEN_REASON IS NOT NULL THEN 1 ELSE 0 END) AS UNSEEN_N,
           MAX(TO_CHAR(SATELLITE))                                    AS SATELLITE
    FROM TBL_OBSERVATION_AREA
    WHERE DELETED_AT IS NULL AND IMAGE_ID IS NOT NULL
    GROUP BY IMAGE_ID
),
mine AS (
    SELECT a.OBJECTID, COUNT(v.OBJECTID) AS N
    FROM TBL_ASSIGNMENT a
    JOIN VW_OBS_RECORD v
      ON v.IMAGE_ID = a.IMAGE_ID
     AND (UPPER(v.CREATED_USER) = UPPER(a.ASSIGNEE) OR UPPER(v.OBSERVER) = UPPER(a.ASSIGNEE))
    GROUP BY a.OBJECTID
)
SELECT
    a.OBJECTID,
    a.IMAGE_ID,
    a.IMAGE_NAME,
    a.IMAGE_TIME,
    TRUNC(CAST(a.IMAGE_TIME AS DATE) + 9 / 24) - 9 / 24                AS IMAGE_DATE,
    a.ASSIGNEE,
    NVL(a.STATUS, '作業中')                                            AS STATUS,
    a.NOTE,
    a.TAKEN_AT,
    a.DONE_AT,
    TRUNC(CAST(a.TAKEN_AT AS DATE) + 9 / 24) - 9 / 24                  AS TAKEN_DATE,
    TRUNC(CAST(a.DONE_AT AS DATE) + 9 / 24) - 9 / 24                   AS DONE_DATE,
    ROUND((NVL(CAST(a.DONE_AT AS DATE), c.NOW_UTC) - CAST(a.TAKEN_AT AS DATE)) * 24, 1) AS HOURS,
    c.TODAY - TRUNC(CAST(a.TAKEN_AT AS DATE) + 9 / 24)                 AS DAYS_SINCE_TAKEN,
    NVL(mine.N, 0)                                                     AS MY_RECORDS,
    NVL(img.N, 0)                                                      AS IMAGE_RECORDS,
    NVL(area.N, 0)                                                     AS IMAGE_AREAS,
    NVL(area.UNSEEN_N, 0)                                              AS UNSEEN_AREAS,
    NVL(img.SATELLITE, area.SATELLITE)                                 AS SATELLITE,
    img.LAST_AT                                                        AS LAST_RECORD_AT,
    CASE WHEN a.STATUS = '完了' AND NVL(img.N, 0) = 0 AND NVL(area.N, 0) + NVL(area.UNSEEN_N, 0) = 0 THEN 1 ELSE 0 END AS DONE_EMPTY
FROM TBL_ASSIGNMENT a
CROSS JOIN clock c
LEFT JOIN img  ON img.IMAGE_ID = a.IMAGE_ID
LEFT JOIN area ON area.IMAGE_ID = a.IMAGE_ID
LEFT JOIN mine ON mine.OBJECTID = a.OBJECTID;


-- ---------------------------------------------------------------------------------------------
-- 10. VW_ALERT_LOG（通報記録）: 通報記録 1 行 = 1 行・形状なし
--     通報した記録（OBS_ID）の地名・モノ・分類を付ける。記録を後で消したものは地名なし。
--     1 つの記録は通報記録に 1 回だけ入る（プラグインは通報済みの記録を選び直さない）ので、
--     件数 = 通報した記録の数。BATCH_ID は 1 回の送信（まとめて送った記録は同じ）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_ALERT_LOG AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
)
SELECT
    l.OBJECTID,
    l.OBS_ID,
    l.REPORT_NAME,
    l.TEXT                                                             AS REPORT_TEXT,
    l.REPORTED_AT,
    TRUNC(CAST(l.REPORTED_AT AS DATE) + 9 / 24) - 9 / 24               AS REPORTED_DATE,
    c.TODAY - TRUNC(CAST(l.REPORTED_AT AS DATE) + 9 / 24)              AS DAYS_AGO,
    l.REPORTED_BY,
    l.BATCH_ID,
    l.MANUAL,
    CASE WHEN l.MANUAL = 1 THEN '手動' ELSE '条件' END                 AS MANUAL_LABEL,
    v.OBJECT_ID,
    v.OBJECT_NAME,
    v.ROOT_KIND,
    v.LEAF_KIND,
    v.ACTIVITY,
    v.CONFIDENCE,
    v.SEEN_AT,
    v.IMAGE_NAME,
    v.NAMED_AREA_OBJECTID,
    v.NAMED_AREA_ID,
    v.PLACE_NAME,
    v.PLACE_KIND,
    NVL(v.PLACE_LABEL, '（記録なし）')                                 AS PLACE_LABEL
FROM TBL_ALERT_LOG l
CROSS JOIN clock c
LEFT JOIN VW_OBS_RECORD v ON v.OBS_ID = l.OBS_ID;


-- ---------------------------------------------------------------------------------------------
-- 11. VW_NAMED_AREA_WEEK（地名 × 週）: 地名 1 つ・7 日 1 区間 = 1 行（直近 26 区間）・形状なし
--     区間は今日で終わる 7 日ずつ（WEEKS_AGO 0 = 今日までの 7 日、1 = その前の 7 日 …）。暦の週にしないのは、
--     月曜の朝に「今週」が少なく見えないようにするため。
--     数えるのは「消失」以外の記録（RECORDS）と、その中のモノの数（THINGS）。記録は 1 つの地名
--     （含むうちいちばん小さいもの）にだけ数える: 湾の中の港の記録は港に数え、湾には足さない。
--     平常との差はプラグインの「活動の推移」（core/dashboard.py の trends）と同じ決め方:
--       NORMAL = 前の 8 区間の平均、ばらつき = 標準偏差（平均の平方根・1 より小さければそちら）、
--       SCORE = (RECORDS - NORMAL) / ばらつき、急に増えた = SCORE が 2 以上・3 件以上・平常より多い。
--     急に減った（SCORE が -2 以下・平常が 3 件以上）はダッシュボード用に足したもの（プラグインにはない）。
--     UPPER_LINE: これを超えると「急に増えた」になる目安（NORMAL + 2 × ばらつき。系列グラフの線用）。
--     USUAL_RECORDS・SURGE_RECORDS: RECORDS を「急に増えた」区間とそれ以外に分けたもの（積み上げると、
--     急に増えた区間の棒だけ別の色になる）。
--     OBJECTID = 地名の OBJECTID × 100 + WEEKS_AGO。
--     QUANTITY_TOTAL（2026-11-24）: その区間に見えたモノの数量の合計（まとめて記録したモノは数量、1 つずつの記録は 1。
--     同じモノを区間内に何度か見たときは、いちばん多かった数量）。急に増えた・減ったの判定は今まで通り RECORDS で決める。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_NAMED_AREA_WEEK AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
weeks AS (
    SELECT LEVEL - 1 AS WEEKS_AGO FROM DUAL CONNECT BY LEVEL <= 26 + 8
),
one AS (
    SELECT
        v.NAMED_AREA_OBJECTID,
        FLOOR(v.DAYS_AGO / 7)                                          AS WEEKS_AGO,
        v.OBJECT_ID,
        COUNT(*)                                                       AS RECORDS,
        MAX(v.QUANTITY)                                                AS MOST
    FROM VW_OBS_RECORD v
    WHERE v.NAMED_AREA_OBJECTID IS NOT NULL
      AND NVL(v.PRESENT, 1) <> 0
      AND v.LOOKED = 1
      AND v.DAYS_AGO BETWEEN 0 AND (26 + 8) * 7 - 1
    GROUP BY v.NAMED_AREA_OBJECTID, FLOOR(v.DAYS_AGO / 7), v.OBJECT_ID
),
n AS (
    SELECT
        one.NAMED_AREA_OBJECTID,
        one.WEEKS_AGO,
        SUM(one.RECORDS)                                               AS RECORDS,
        COUNT(one.OBJECT_ID)                                           AS THINGS,
        SUM(one.MOST)                                                  AS QUANTITY_TOTAL
    FROM one
    GROUP BY one.NAMED_AREA_OBJECTID, one.WEEKS_AGO
),
g AS (
    SELECT
        a.OBJECTID                                                     AS AREA_OBJECTID,
        a.NAMED_AREA_ID,
        a.NAME,
        a.KIND,
        a.ACTIVE,
        w.WEEKS_AGO,
        c.TODAY - 7 * w.WEEKS_AGO                                      AS LAST_DAY,
        NVL(n.RECORDS, 0)                                              AS RECORDS,
        NVL(n.THINGS, 0)                                               AS THINGS,
        NVL(n.QUANTITY_TOTAL, 0)                                       AS QUANTITY_TOTAL,
        AVG(NVL(n.RECORDS, 0)) OVER (
            PARTITION BY a.OBJECTID ORDER BY w.WEEKS_AGO DESC ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING) AS NORMAL,
        VARIANCE(NVL(n.RECORDS, 0)) OVER (
            PARTITION BY a.OBJECTID ORDER BY w.WEEKS_AGO DESC ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING) AS VAR
    FROM TBL_NAMED_AREA a
    CROSS JOIN weeks w
    CROSS JOIN clock c
    LEFT JOIN n ON n.NAMED_AREA_OBJECTID = a.OBJECTID AND n.WEEKS_AGO = w.WEEKS_AGO
),
s AS (
    SELECT
        g.AREA_OBJECTID,
        g.NAMED_AREA_ID,
        g.NAME,
        g.KIND,
        g.ACTIVE,
        g.WEEKS_AGO,
        g.LAST_DAY,
        g.RECORDS,
        g.THINGS,
        g.QUANTITY_TOTAL,
        g.NORMAL,
        SQRT(GREATEST(NVL(g.VAR, 0), g.NORMAL, 1))                     AS SPREAD
    FROM g
    WHERE g.WEEKS_AGO < 26
),
t AS (
    SELECT
        s.*,
        (s.RECORDS - s.NORMAL) / s.SPREAD                              AS SCORE,
        CASE WHEN s.RECORDS >= 3 AND s.RECORDS > s.NORMAL
                  AND (s.RECORDS - s.NORMAL) / s.SPREAD >= 2 THEN 1 ELSE 0 END AS IS_SURGE
    FROM s
)
SELECT
    CAST(t.AREA_OBJECTID * 100 + t.WEEKS_AGO AS NUMBER(38))            AS OBJECTID,
    t.AREA_OBJECTID                                                    AS NAMED_AREA_OBJECTID,
    t.NAMED_AREA_ID,
    t.NAME                                                             AS PLACE_NAME,
    t.KIND                                                             AS PLACE_KIND,
    t.ACTIVE,
    t.WEEKS_AGO,
    t.LAST_DAY - 6 - 9 / 24                                            AS WEEK_START_DATE,
    t.LAST_DAY - 9 / 24                                                AS WEEK_END_DATE,
    TO_CHAR(t.LAST_DAY - 6, 'MM/DD') || '〜' || TO_CHAR(t.LAST_DAY, 'MM/DD') AS WEEK_TEXT,
    t.RECORDS,
    t.THINGS,
    t.QUANTITY_TOTAL,
    CASE WHEN t.IS_SURGE = 1 THEN 0 ELSE t.RECORDS END                 AS USUAL_RECORDS,
    CASE WHEN t.IS_SURGE = 1 THEN t.RECORDS ELSE 0 END                 AS SURGE_RECORDS,
    ROUND(t.NORMAL, 1)                                                 AS NORMAL,
    ROUND(t.NORMAL + 2 * t.SPREAD, 1)                                  AS UPPER_LINE,
    ROUND(t.RECORDS - t.NORMAL, 1)                                     AS CHANGE,
    ROUND(t.SCORE, 2)                                                  AS SCORE,
    CASE
        WHEN t.RECORDS = 0 AND t.NORMAL = 0          THEN '記録なし'
        WHEN t.IS_SURGE = 1                          THEN '急に増えた'
        WHEN t.NORMAL >= 3 AND t.SCORE <= -2         THEN '急に減った'
        ELSE '平常'
    END                                                                AS TREND_CLASS
FROM t;


-- ---------------------------------------------------------------------------------------------
-- 12. VW_NAMED_AREA_THING（地名 × モノ）: 地名 1 つ・モノ 1 つ = 1 行・ポイント（その地名での最後の記録）
--     その地名で見たモノを、そこで最初・最後に見た日時と回数で並べる。モノの名前・分類は VW_OBS_THING
--     （最後に書かれたもの）。WHERE_NOW: モノの最後の記録（どの地名でも）がこの地名か。
--       ここ（1 か月以内）・ここ（1 か月より前）・消失を確認（最後の記録がここの「消失」）・その後ほかで見た
--     （「ほか」には地名の外も入る）。
--     LAST_QUANTITY（2026-11-24）: その地名での最後の記録の数量（1 つずつの記録は 1、消失は 0）。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_NAMED_AREA_THING AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
r AS (
    SELECT
        v.OBJECTID,
        v.SHAPE,
        v.NAMED_AREA_OBJECTID,
        v.OBJECT_ID,
        v.SEEN_AT,
        v.SEEN_DATE,
        v.ACTIVITY,
        v.PRESENT_LABEL,
        v.QUANTITY,
        ROW_NUMBER() OVER (PARTITION BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID
                           ORDER BY v.LOOKED DESC, v.SEEN_AT DESC, v.CREATED_AT DESC, v.OBJECTID DESC) AS RN,
        MIN(v.SEEN_AT) OVER (PARTITION BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID)                 AS FIRST_SEEN_AT,
        COUNT(*) OVER (PARTITION BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID)                       AS RECORD_COUNT,
        SUM(CASE WHEN v.PRESENT = 1 THEN 1 ELSE 0 END)
            OVER (PARTITION BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID)                            AS SEEN_COUNT,
        COUNT(DISTINCT CASE WHEN v.PRESENT = 1 THEN v.SEEN_DATE END)
            OVER (PARTITION BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID)                            AS SEEN_DAYS
    FROM VW_OBS_RECORD v
    WHERE v.NAMED_AREA_OBJECTID IS NOT NULL
      AND v.OBJECT_ID IS NOT NULL
)
SELECT
    r.OBJECTID,
    r.SHAPE,
    r.NAMED_AREA_OBJECTID,
    a.NAMED_AREA_ID,
    a.NAME                                                             AS PLACE_NAME,
    a.KIND                                                             AS PLACE_KIND,
    r.OBJECT_ID,
    t.THING_NAME,
    t.ROOT_KIND,
    t.LEAF_KIND,
    r.FIRST_SEEN_AT,
    TRUNC(r.FIRST_SEEN_AT + 9 / 24) - 9 / 24                           AS FIRST_SEEN_DATE,
    r.SEEN_AT                                                          AS LAST_SEEN_AT,
    r.SEEN_DATE                                                        AS LAST_SEEN_DATE,
    c.TODAY - (r.SEEN_DATE + 9 / 24)                                   AS DAYS_SINCE_SEEN,
    r.RECORD_COUNT,
    r.SEEN_COUNT,
    r.SEEN_DAYS,
    r.ACTIVITY                                                         AS LAST_ACTIVITY,
    r.PRESENT_LABEL                                                    AS LAST_PRESENT_LABEL,
    r.QUANTITY                                                         AS LAST_QUANTITY,
    t.LAST_SEEN_AT                                                     AS THING_LAST_SEEN_AT,
    t.LAST_PLACE_LABEL                                                 AS THING_LAST_PLACE,
    CASE WHEN t.LAST_NAMED_AREA_OBJECTID = r.NAMED_AREA_OBJECTID THEN 1 ELSE 0 END AS IS_LAST_PLACE,
    CASE
        WHEN NVL(t.LAST_NAMED_AREA_OBJECTID, -1) <> r.NAMED_AREA_OBJECTID THEN 'その後ほかで見た'
        WHEN t.LAST_PRESENT = 0                                            THEN '消失を確認'
        WHEN t.IN_LAST_MONTH = 1                                           THEN 'ここ（1 か月以内）'
        ELSE 'ここ（1 か月より前）'
    END                                                                AS WHERE_NOW
FROM r
JOIN TBL_NAMED_AREA a ON a.OBJECTID = r.NAMED_AREA_OBJECTID
JOIN VW_OBS_THING t ON t.OBJECT_ID = r.OBJECT_ID
CROSS JOIN clock c
WHERE r.RN = 1;


-- ---------------------------------------------------------------------------------------------
-- 13. VW_NAMED_AREA_STATUS（地名の様子）: 地名 1 つ = 1 行・ポリゴン（TBL_NAMED_AREA の範囲）
--     直近 7 日と平常との差（VW_NAMED_AREA_WEEK の WEEKS_AGO = 0）、30 日の件数・モノの数・多い大分類、
--     最後に見た日、いまいそうなモノの数（THINGS_LAST_HERE: 最後に見たのがここで 1 か月以内・消失でない）、
--     30 日の通報（通報した記録の数）。無効の地名（ACTIVE = 0）も出す（ACTIVE_LABEL で絞る）。
--     PLACE_LABEL は「地名（管理番号）」: 同じ名前の地名があっても選び分けられるように。
--     数量の合計（2026-11-24）: QUANTITY_7D・QUANTITY_30D は直近 7 日・30 日に見えたモノの数量の合計（同じモノは期間内で
--     いちばん多かった数量）、QUANTITY_LAST_HERE はいまいそうなモノの最後の数量の合計。件数・モノの数は今まで通り。
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_NAMED_AREA_STATUS AS
WITH clock AS (
    SELECT TRUNC(CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) + 9 / 24) AS TODAY FROM DUAL
),
rec AS (
    SELECT
        v.NAMED_AREA_OBJECTID,
        COUNT(*)                                                       AS RECORDS_TOTAL,
        SUM(CASE WHEN v.DAYS_AGO BETWEEN 0 AND 29 AND NVL(v.PRESENT, 1) <> 0 AND v.LOOKED = 1 THEN 1 ELSE 0 END) AS RECORDS_30D,
        COUNT(DISTINCT CASE WHEN v.DAYS_AGO BETWEEN 0 AND 29 AND NVL(v.PRESENT, 1) <> 0 AND v.LOOKED = 1 THEN v.OBJECT_ID END) AS THINGS_30D,
        STATS_MODE(CASE WHEN v.DAYS_AGO BETWEEN 0 AND 29 AND NVL(v.PRESENT, 1) <> 0 AND v.LOOKED = 1 THEN v.ROOT_KIND END) AS TOP_KIND_30D,
        MIN(CASE WHEN v.LOOKED = 1 THEN v.SEEN_AT END)                 AS FIRST_SEEN_AT,
        MAX(CASE WHEN v.LOOKED = 1 THEN v.SEEN_AT END)                 AS LAST_SEEN_AT
    FROM VW_OBS_RECORD v
    WHERE v.NAMED_AREA_OBJECTID IS NOT NULL
    GROUP BY v.NAMED_AREA_OBJECTID
),
q30 AS (
    SELECT x.NAMED_AREA_OBJECTID, SUM(x.MOST) AS QUANTITY_30D
    FROM (
        SELECT v.NAMED_AREA_OBJECTID, v.OBJECT_ID, MAX(v.QUANTITY) AS MOST
        FROM VW_OBS_RECORD v
        WHERE v.NAMED_AREA_OBJECTID IS NOT NULL
          AND v.DAYS_AGO BETWEEN 0 AND 29 AND NVL(v.PRESENT, 1) <> 0 AND v.LOOKED = 1
        GROUP BY v.NAMED_AREA_OBJECTID, v.OBJECT_ID
    ) x
    GROUP BY x.NAMED_AREA_OBJECTID
),
here AS (
    SELECT t.LAST_NAMED_AREA_OBJECTID AS NAMED_AREA_OBJECTID, COUNT(*) AS THINGS_LAST_HERE,
           SUM(t.LAST_QUANTITY) AS QUANTITY_LAST_HERE
    FROM VW_OBS_THING t
    WHERE t.LAST_NAMED_AREA_OBJECTID IS NOT NULL
      AND t.IN_LAST_MONTH = 1
      AND NVL(t.LAST_PRESENT, 1) <> 0
    GROUP BY t.LAST_NAMED_AREA_OBJECTID
),
alert AS (
    SELECT
        l.NAMED_AREA_OBJECTID,
        COUNT(DISTINCT CASE WHEN l.DAYS_AGO BETWEEN 0 AND 29 THEN l.OBS_ID END) AS ALERTS_30D,
        MAX(l.REPORTED_AT)                                             AS LAST_ALERT_AT
    FROM VW_ALERT_LOG l
    WHERE l.NAMED_AREA_OBJECTID IS NOT NULL
    GROUP BY l.NAMED_AREA_OBJECTID
),
wk AS (
    SELECT w.NAMED_AREA_OBJECTID, w.RECORDS, w.THINGS, w.QUANTITY_TOTAL, w.NORMAL, w.UPPER_LINE, w.CHANGE, w.SCORE, w.TREND_CLASS
    FROM VW_NAMED_AREA_WEEK w
    WHERE w.WEEKS_AGO = 0
)
SELECT
    a.OBJECTID,
    a.SHAPE,
    a.NAMED_AREA_ID,
    a.NAME                                                             AS PLACE_NAME,
    a.NAME_EN,
    a.KIND                                                             AS PLACE_KIND,
    TO_CHAR(a.NAME) || CASE WHEN a.NAMED_AREA_ID IS NOT NULL
                            THEN '（' || TO_CHAR(a.NAMED_AREA_ID) || '）' END AS PLACE_LABEL,
    a.ACTIVE,
    CASE WHEN a.ACTIVE = 0 THEN '無効' ELSE '有効' END                 AS ACTIVE_LABEL,
    NVL(wk.RECORDS, 0)                                                 AS LAST_7D,
    NVL(wk.THINGS, 0)                                                  AS THINGS_7D,
    NVL(wk.QUANTITY_TOTAL, 0)                                          AS QUANTITY_7D,
    NVL(wk.NORMAL, 0)                                                  AS NORMAL_7D,
    wk.UPPER_LINE                                                      AS UPPER_7D,
    NVL(wk.CHANGE, 0)                                                  AS CHANGE_7D,
    NVL(wk.SCORE, 0)                                                   AS TREND_SCORE,
    NVL(wk.TREND_CLASS, '記録なし')                                    AS TREND_CLASS,
    NVL(rec.RECORDS_TOTAL, 0)                                          AS RECORDS_TOTAL,
    NVL(rec.RECORDS_30D, 0)                                            AS RECORDS_30D,
    NVL(rec.THINGS_30D, 0)                                             AS THINGS_30D,
    NVL(q30.QUANTITY_30D, 0)                                           AS QUANTITY_30D,
    rec.TOP_KIND_30D,
    NVL(here.THINGS_LAST_HERE, 0)                                      AS THINGS_LAST_HERE,
    NVL(here.QUANTITY_LAST_HERE, 0)                                    AS QUANTITY_LAST_HERE,
    rec.FIRST_SEEN_AT,
    rec.LAST_SEEN_AT,
    TRUNC(rec.LAST_SEEN_AT + 9 / 24) - 9 / 24                          AS LAST_SEEN_DATE,
    c.TODAY - TRUNC(rec.LAST_SEEN_AT + 9 / 24)                         AS DAYS_SINCE_SEEN,
    NVL(alert.ALERTS_30D, 0)                                           AS ALERTS_30D,
    alert.LAST_ALERT_AT
FROM TBL_NAMED_AREA a
CROSS JOIN clock c
LEFT JOIN wk    ON wk.NAMED_AREA_OBJECTID = a.OBJECTID
LEFT JOIN rec   ON rec.NAMED_AREA_OBJECTID = a.OBJECTID
LEFT JOIN q30   ON q30.NAMED_AREA_OBJECTID = a.OBJECTID
LEFT JOIN here  ON here.NAMED_AREA_OBJECTID = a.OBJECTID
LEFT JOIN alert ON alert.NAMED_AREA_OBJECTID = a.OBJECTID;
