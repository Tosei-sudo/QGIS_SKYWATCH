"""
通報をメールで (tsudo, 2026-11-15: 「通報の結果を送りたい。宛先、件名はコンフィグ
設定、通知ドックから作成する感じ。件名は日付等の埋め込みとかできたらしたい」):
rows of 通知 ▸ 通報 made into a mailto: link that the default mail program
opens as a new mail (nothing is sent from QGIS: the person sends it there).

The addresses, the subject and the lines around the rows are
`alertReports.mail`; the subject, header and footer take the same places as
a format's header (`{now:%Y/%m/%d@UTC}`, `{date}`, `{count}`, `{name}`,
`{user}`) and the oldest and newest row's 通報日時 (`{first:%H:%M}`,
`{last:…}`), in the default format's time zone and date format.

A mailto: link cannot be long: Windows (ShellExecute) and Outlook refuse or
cut one longer than about 2,000 characters, and a Japanese character is 9
of them once percent-encoded (6 in cp932). A body that does not fit
`maxLength` is cut by whole rows, a note saying how many were left out; the
whole text then goes to the clipboard to paste. No attachments: mailto:
cannot carry files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import quote

from ..util import encodable
from .alerts import AlertFormat, AlertSettings, LogRow, MailSettings, Moment, fill

#: Between the lines of the body (RFC 6068: CRLF, %0D%0A).
NEWLINE = "\r\n"


@dataclass
class MailDraft:
    """A mail made from rows: its link, and the whole text (for the clipboard when the link holds only some rows)."""
    url: str
    subject: str
    text: str
    #: Rows in the link's body, of `total`.
    shown: int
    total: int
    #: Characters the encoding cannot hold (cp932: emoji, é…) became ?.
    replaced: bool = False
    #: Even without the rows the link is longer than `maxLength` (a long subject, many addresses).
    too_long: bool = False

    @property
    def cut(self) -> bool:
        return self.shown < self.total


def mail_order(rows: Sequence[LogRow]) -> list[LogRow]:
    """Rows as they were reported: older reports first, each report's lines in its own order."""
    return sorted(rows, key=lambda r: (r.reported_at or 0, r.oid or 0))


def left_out_note(left: int) -> str:
    """The line ending a body cut short."""
    return f"（ほか {left} 件省略。全文はクリップボードから貼り付け）"


def _encoded(text: str, encoding: str) -> str:
    return quote(text.encode(encoding, errors="replace"), safe="")


def _addresses(addresses: Sequence[str]) -> str:
    return ",".join(quote(a, safe="@+") for a in addresses)


def build_mail(settings: AlertSettings, rows: Sequence[LogRow], at: Moment) -> MailDraft:
    """The mailto: link of the rows (as reported), its subject and header filled, cut to `maxLength` by rows."""
    mail = settings.mail or MailSettings()
    report = settings.format() or AlertFormat(name="")
    rows = mail_order(rows)
    names = "・".join(dict.fromkeys(r.format for r in rows if r.format)) or report.name
    reported = [r.reported_at for r in rows if r.reported_at is not None]
    times = {"first": min(reported) if reported else None, "last": max(reported) if reported else None}
    at = Moment(now=at.now, user=at.user, count=len(rows))

    def filled(template: str) -> list[str]:
        return fill(template, report, at, {"name": names}, times).split("\n") if template else []

    subject = re.sub(r"\s*[\r\n]+\s*", " ", "\n".join(filled(mail.subject))).strip()
    head, foot = filled(mail.header), filled(mail.footer)
    lines = [r.text for r in rows]
    text = NEWLINE.join(head + lines + foot)
    enc = mail.encoding
    query = [f"{key}={_addresses(v)}" for key, v in (("cc", mail.cc), ("bcc", mail.bcc)) if v]
    if subject:
        query.append("subject=" + _encoded(subject, enc))
    base = "mailto:" + _addresses(mail.to) + "?" + "&".join(query + ["body="])
    url = base + _encoded(text, enc)
    shown, too_long = len(rows), False
    if len(url) > mail.max_length:
        # The longest body of whole rows that fits, with the note (lengths add up: percent-encoding is per character).
        size = [len(_encoded(x, enc)) for x in lines]
        fixed = len(base) + sum(len(_encoded(x, enc)) for x in head + foot)
        step = len(_encoded(NEWLINE, enc))
        shown, used = None, 0
        sums = [0]
        for n in size:
            used += n
            sums.append(used)
        for k in range(len(lines) - 1, -1, -1):
            parts = len(head) + k + 1 + len(foot)
            if fixed + sums[k] + len(_encoded(left_out_note(len(lines) - k), enc)) + step * (parts - 1) <= mail.max_length:
                shown = k
                break
        if shown is None:
            shown, body = 0, left_out_note(len(lines))
        else:
            body = NEWLINE.join(head + lines[:shown] + [left_out_note(len(lines) - shown)] + foot)
        url = base + _encoded(body, enc)
        too_long = len(url) > mail.max_length
    return MailDraft(url=url, subject=subject, text=text, shown=shown, total=len(rows),
                     replaced=encodable(subject + text, enc)[1], too_long=too_long)

