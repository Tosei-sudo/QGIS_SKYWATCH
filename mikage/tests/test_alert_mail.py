"""通報をメールで: alertReports.mail, the mailto: link, its subject's places and the body cut to maxLength."""
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urlsplit

from mikage.core.obs.alert_mail import build_mail, left_out_note, mail_order
from mikage.core.obs.alerts import DEFAULT_MAIL_LENGTH, DEFAULT_MAIL_SUBJECT, LogRow, Moment, alert_settings_of, fill

T0 = 1_763_190_000_000  # 2025-11-15T07:00:00Z = 16:00 JST
LOG = "https://x/arcgis/rest/services/Alerts/FeatureServer/2"


def settings(mail, problems=None, **extra):
    problems = [] if problems is None else problems
    return alert_settings_of({"table": LOG, "formats": [{"name": "速報", "timeZone": "Asia/Tokyo"}, {"name": "English", "timeZone": "UTC"}],
                              "mail": mail, **extra}, problems)


def row(i, text=None, at=T0, fmt="速報"):
    return LogRow(obs_id=f"r{i}", format=fmt, text=text or f"船{i}|停泊|横須賀", reported_at=at, oid=i)


def parts(url):
    """The link's addresses and its fields, decoded."""
    split = urlsplit(url)
    assert split.scheme == "mailto"
    fields = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
    return unquote(split.path), fields


def test_settings_addresses_subject_and_problems():
    problems = []
    s = settings({"to": "a@x.jp; b@x.jp, a@x.jp", "cc": ["c@x.jp", "d@x.jp e@x.jp"], "bcc": "f@x.jp",
                  "subject": "【通報】{now:%Y/%m/%d} {count}件", "header": ["各位", ""], "footer": "以上", "encoding": "shift_jis",
                  "maxLength": 3000}, problems)
    assert problems == []
    m = s.mail
    assert (m.to, m.cc, m.bcc) == (["a@x.jp", "b@x.jp"], ["c@x.jp", "d@x.jp", "e@x.jp"], ["f@x.jp"])
    assert (m.subject, m.header, m.footer, m.encoding, m.max_length) == ("【通報】{now:%Y/%m/%d} {count}件", "各位\n", "以上", "cp932", 3000)
    # true: no address, the default subject; none / false: no mail.
    assert settings(True).mail.to == [] and settings(True).mail.subject == DEFAULT_MAIL_SUBJECT
    assert settings(True).mail.max_length == DEFAULT_MAIL_LENGTH
    assert settings(False).mail is None and alert_settings_of({"formats": [{"name": "a"}]}, []).mail is None
    problems = []
    s = settings({"to": ["a@x.jp", "田中", "b@"], "cc": 3, "subject": "{now@Mars} {cnt} {date}", "encoding": "euc-jp", "maxLength": 10}, problems)
    assert s.mail.to == ["a@x.jp"] and s.mail.cc == [] and s.mail.max_length == DEFAULT_MAIL_LENGTH and s.mail.encoding == "utf-8"
    assert any("to の「田中」" in x for x in problems) and any("to の「b@」" in x for x in problems)
    assert any("cc はメールアドレス" in x for x in problems)
    assert any("{now@Mars} のタイムゾーン" in x for x in problems) and any("{cnt} は使えない" in x for x in problems)
    assert not any("{date} は使えない" in x for x in problems)
    assert any("encoding" in x for x in problems) and any("maxLength" in x for x in problems)
    problems = []
    assert settings("a@x.jp", problems).mail is None and any("alertReports.mail は true か" in x for x in problems)


def test_link_subject_places_and_body_as_reported():
    s = settings({"to": ["a@x.jp", "b+alerts@x.jp"], "cc": "c@x.jp", "bcc": "d@x.jp",
                  "subject": "【通報】{now:%Y/%m/%d@UTC} {date} {count}件 {name} {first:%H:%M}〜{last:%H:%M@UTC} {user}",
                  "header": ["各位", "{user} です（{count} 件）"], "footer": "以上"})
    rows = [row(2, at=T0 + 60_000), row(1, "船1|停泊 & 港=横須賀 + 100% #1", at=T0), row(3, at=T0 + 60_000, fmt="English")]
    draft = build_mail(s, rows, Moment(now=T0 + 120_000, user="田中"))
    to, fields = parts(draft.url)
    assert to == "a@x.jp,b+alerts@x.jp" and fields["cc"] == "c@x.jp" and fields["bcc"] == "d@x.jp"
    assert draft.subject == fields["subject"] == "【通報】2025/11/15 2025-11-15 16:02 3件 速報・English 16:00〜07:01 田中"
    # As reported: the oldest first, then by object id (each report's lines in its order); CRLF between lines.
    assert fields["body"] == draft.text == "\r\n".join(
        ["各位", "田中 です（3 件）", "船1|停泊 & 港=横須賀 + 100% #1", "船2|停泊|横須賀", "船3|停泊|横須賀", "以上"])
    assert "%0D%0A" in draft.url and "%2B" in draft.url and "%26" in draft.url and "+" not in draft.url.split("?", 1)[1]
    assert (draft.shown, draft.total, draft.cut, draft.too_long, draft.replaced) == (3, 3, False, False, False)
    assert draft.url.isascii()
    assert [r.oid for r in mail_order(rows)] == [1, 2, 3]


def test_no_address_and_rows_without_a_time():
    s = settings(True)
    draft = build_mail(s, [LogRow(obs_id="r1", text="船A")], Moment(now=T0))
    assert draft.url.startswith("mailto:?subject=")
    assert parts(draft.url)[1] == {"subject": "通報 2025/11/15 16:00 1件", "body": "船A"}
    # {first}, {last} with no 通報日時: nothing.
    s = settings({"subject": "[{first:%H:%M}]{name}"})
    assert build_mail(s, [LogRow(obs_id="r1", text="船A", format="")], Moment(now=T0)).subject == "[]速報"


def test_a_long_body_is_cut_by_rows_with_a_note():
    s = settings({"to": "a@x.jp", "header": "各位", "footer": "以上", "maxLength": 1200})
    rows = [row(i, f"2026-11-15 09:{i:02d}|船舶|貨物船{i}|停泊|確定|35.4418|139.6417|横須賀港") for i in range(1, 21)]
    draft = build_mail(s, rows, Moment(now=T0))
    assert draft.cut and 0 < draft.shown < 20 and not draft.too_long
    assert len(draft.url) <= 1200
    body = parts(draft.url)[1]["body"].split("\r\n")
    assert body[0] == "各位" and body[-1] == "以上" and body[-2] == left_out_note(20 - draft.shown)
    assert body[1:-2] == [r.text for r in rows[:draft.shown]]
    # One more row would not have fitted.
    longer = build_mail(settings({"to": "a@x.jp", "header": "各位", "footer": "以上", "maxLength": len(draft.url) + 50}), rows, Moment(now=T0))
    assert longer.shown == draft.shown
    # The whole text is kept for the clipboard.
    assert draft.text.split("\r\n")[1:-1] == [r.text for r in rows]


def test_cp932_and_when_nothing_fits():
    s = settings({"to": "a@x.jp", "encoding": "cp932", "subject": "通報 {count}件"})
    draft = build_mail(s, [row(1, "船A ⚓ é")], Moment(now=T0))
    query = draft.url.split("?", 1)[1]
    raw = {k: unquote_to_bytes(v) for k, v in (x.split("=", 1) for x in query.split("&"))}
    assert raw["subject"].decode("cp932") == "通報 1件" and raw["body"].decode("cp932") == "船A ? ?"
    assert draft.replaced and draft.text == "船A ⚓ é"
    # The subject alone too long: the note only, and said.
    s = settings({"to": "a@x.jp", "subject": "あ" * 30, "header": "各位", "maxLength": 200})
    draft = build_mail(s, [row(1)], Moment(now=T0))
    assert draft.shown == 0 and draft.too_long and parts(draft.url)[1]["body"] == left_out_note(1)


def test_fill_times_other_places():
    from mikage.core.obs.alerts import AlertFormat
    from mikage.core.time_zone import UTC

    report = AlertFormat(name="速報", time_zone=UTC, date_format="%H:%M")
    at = Moment(now=T0, user="田中", count=2)
    assert fill("{first}-{last:%d@+09:00}-{now}-{other}", report, at, times={"first": T0 + 60_000, "last": None}) == "07:01--07:00-{other}"
