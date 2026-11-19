"""
文章の手伝い in QGIS (qgis_io/text_assist_io.py, ui/text_assist_ui.py): where
the model is, the real ONNX Runtime GenAI on a tiny model (tests/genai_tiny.py),
and 「整える」・「申し送りの下書き」 on the screens with a model of the test's own.
"""
import json
import os
import threading
import time
import zipfile

import pytest

pytest.importorskip("qgis.core")

from mikage.core.text_assist import plain_prompt  # noqa: E402
from mikage.qgis_io import settings, text_assist_io  # noqa: E402
from mikage.qgis_io.ai_runtime import AiError  # noqa: E402
from mikage.qgis_io.tasks import Cancelled  # noqa: E402
from mikage.tests.genai_tiny import write_model  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import plugin  # noqa: E402,F401 (the fixture)

MESSAGES = [{"role": "system", "content": "備考を整える"}, {"role": "user", "content": "備考: 岸壁にとまってる"}]


# ---- where the model is ----------------------------------------------------------------------

def _config(folder):
    os.makedirs(folder, exist_ok=True)
    (folder / "genai_config.json").write_text("{}", encoding="utf-8")
    (folder / "model.onnx").write_bytes(b"onnx")
    return folder


def test_a_model_folder_is_used_where_it_is_and_relative_to_the_settings_file(tmp_path):
    model = _config(tmp_path / "models" / "sarashina")
    assert text_assist_io.model_folder_of(str(model)) == str(model)
    # Its folder given (one model folder in it), and relative to the settings file.
    assert text_assist_io.model_folder_of(str(tmp_path / "models")) == str(model)
    assert text_assist_io.model_folder_of("models", str(tmp_path / "config.json")) == str(model)
    _config(tmp_path / "models" / "other")
    with pytest.raises(AiError, match="genai_config.json なし"):
        text_assist_io.model_folder_of(str(tmp_path / "models"))
    with pytest.raises(AiError, match="モデルなし"):
        text_assist_io.model_folder_of(str(tmp_path / "nothing"))
    with pytest.raises(AiError, match=r"\.zip で指定"):
        text_assist_io.model_folder_of("https://example.com/models/sarashina")


def test_a_model_on_a_network_share_is_copied_once(tmp_path, monkeypatch):
    model = _config(tmp_path / "share" / "sarashina")
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setattr(text_assist_io, "is_network_path", lambda path, system=None: True)
    first = text_assist_io.model_folder_of(str(model), root=str(root))
    assert os.path.dirname(first) == str(root) and sorted(os.listdir(first)) == ["genai_config.json", "model.onnx"]
    assert text_assist_io.model_folder_of(str(model), root=str(root)) == first
    # Changed: a new copy, the old one gone.
    (model / "model.onnx").write_bytes(b"onnx, newer")
    second = text_assist_io.model_folder_of(str(model), root=str(root))
    assert second != first and not os.path.exists(first) and open(os.path.join(second, "model.onnx"), "rb").read() == b"onnx, newer"
    assert text_assist_io.is_network_path("\\\\server\\share\\m") and text_assist_io.is_network_path("//server/share/m")
    monkeypatch.undo()
    assert not text_assist_io.is_network_path("C:\\models\\m", "Linux") and not text_assist_io.is_network_path("/home/m")


def test_a_zipped_model_is_unpacked_once_and_a_bad_one_refused(tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    zipped = tmp_path / "sarashina-1b-int4.zip"
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr("sarashina-1b-int4/genai_config.json", "{}")
        z.writestr("sarashina-1b-int4/model.onnx", "onnx")
    folder = text_assist_io.model_folder_of(str(zipped), root=str(root))
    assert os.path.basename(folder) == "sarashina-1b-int4" and os.path.isfile(os.path.join(folder, "model.onnx"))
    assert text_assist_io.model_folder_of(str(zipped), root=str(root)) == folder
    assert not [n for n in os.listdir(root) if n.startswith(".")]  # no unpacking left half done
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("m/genai_config.json", "{}")
        z.writestr("../outside.txt", "x")
    with pytest.raises(AiError, match="不正なパス"):
        text_assist_io.model_folder_of(str(evil), root=str(root))
    assert not (tmp_path / "outside.txt").exists()
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(AiError, match="開けず"):
        text_assist_io.model_folder_of(str(broken), root=str(root))
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(AiError, match="genai_config.json なし"):
        text_assist_io.model_folder_of(str(empty), root=str(root))


# ---- the real runtime on a tiny model -------------------------------------------------------------

@pytest.fixture
def genai():
    return pytest.importorskip("onnxruntime_genai")


def test_the_runtime_writes_the_reply_as_it_grows(genai, tmp_path):
    writer = text_assist_io.GenAiWriter(write_model(str(tmp_path / "m"), "Tidy note."), threads=1)
    prompt = writer.prompt(MESSAGES)
    assert prompt == "<|system|>備考を整える<|endoftext|><|user|>備考: 岸壁にとまってる<|endoftext|><|assistant|>"
    seen = []
    text = writer.write(MESSAGES, 32, wrote=seen.append)
    assert text.strip() == "Tidy note." and seen[-1] == text and len(seen) > 3
    # The same every time (greedy).
    assert writer.write(MESSAGES, 32) == text
    # At most max_tokens.
    assert len(writer.write(MESSAGES, 4).strip()) <= 4


def test_one_line_cancel_and_too_long(genai, tmp_path):
    writer = text_assist_io.GenAiWriter(write_model(str(tmp_path / "m"), "Tidy.\nMore"), threads=1)
    assert writer.write(MESSAGES, 32).strip() == "Tidy.\nMore"
    assert writer.write(MESSAGES, 32, one_line=True).strip() == "Tidy."
    with pytest.raises(Cancelled):
        writer.write(MESSAGES, 32, cancelled=lambda: True)
    with pytest.raises(AiError, match="入力が長すぎる"):
        writer.write([{"role": "user", "content": "a" * 5000}], 32)


def test_a_model_without_a_chat_template_is_given_plain_text(genai, tmp_path):
    writer = text_assist_io.GenAiWriter(write_model(str(tmp_path / "m"), "Tidy.", chat_template=False), threads=1)
    assert writer.prompt(MESSAGES) == plain_prompt(MESSAGES)
    # (The tiny model answers only after its assistant token, which plain text has not: it ends at once.)
    assert writer.write(MESSAGES, 16) == ""


def test_a_folder_that_is_no_model_is_said(genai, tmp_path):
    folder = tmp_path / "m"
    write_model(str(folder))
    (folder / "model.onnx").write_bytes(b"broken")
    with pytest.raises(AiError, match="モデルを読み込めず"):
        text_assist_io.GenAiWriter(str(folder))


# ---- on the screens ---------------------------------------------------------------------------------

class FakeWriter:
    """A model of the test's own: the replies in turn, letter by letter (held at `gate` until it is set)."""

    def __init__(self, folder, threads, replies, gate):
        self.folder, self.threads = folder, threads
        self.replies = replies
        self.gate = gate
        self.asked = []

    def write(self, messages, max_tokens, cancelled=lambda: False, wrote=lambda _t: None, one_line=False):
        self.asked.append((list(messages), max_tokens, one_line))
        reply = self.replies.pop(0) if self.replies else ""
        text = ""
        for ch in reply:
            while not self.gate.is_set():
                if cancelled():
                    raise Cancelled()
                time.sleep(0.005)
            if cancelled():
                raise Cancelled()
            text += ch
            wrote(text)
            if one_line and "\n" in text.lstrip():
                break
        return text


def _assist(p, tmp_path, replies, text_assist=None, glossary=False):
    """The settings with a textAssist, and the model replaced by FakeWriter (the ones loaded listed; loaded once `gate.load` is set)."""
    gate = threading.Event()
    gate.set()
    gate.load = threading.Event()
    gate.load.set()
    loaded = []
    a = p.context.text_assist

    def writer_of(folder, threads):
        gate.load.wait(10)
        w = FakeWriter(folder, threads, replies, gate)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    _write_config(p, tmp_path, text_assist or {"model": "models/sarashina"})
    assert wait_for(lambda: a.available)
    # The map drawn first (a test this short would otherwise end while it draws: QGIS 3.34 does not survive it).
    assert wait_for(lambda: not p.iface.mapCanvas().isDrawing())
    return a, loaded, gate


def _write_config(p, tmp_path, text_assist):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"textAssist": text_assist} if text_assist is not None else {}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.reload()


def _record_form(dock):
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.form.isVisible()
    return dock.note_tidy


def test_without_the_settings_nothing_shows(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    assert wait_for(lambda: not p.iface.mapCanvas().isDrawing())
    tidy = _record_form(dock)
    assert not tidy.button.isVisible() and not tidy.panel.isVisible()
    assert not dock.area_note_tidy.button.isVisibleTo(dock)
    dialog = dock.open_handover(0)
    assert not dialog.memo_draft.button.isVisible()
    dialog.close()


def test_tidy_offers_a_draft_and_replace_takes_it(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["備考: 岸壁に入港、停泊中。3 隻\n### 入力", "岸壁に停泊中"])
    tidy = _record_form(dock)
    assert tidy.button.isVisible() and not tidy.button.isEnabled()
    dock.choose_kind(["船舶"])
    dock.note_input.setText("岸壁に入航してとまってる")
    assert tidy.button.isEnabled()
    gate.load.clear()
    tidy.button.click()
    assert tidy.panel.isVisible() and "モデルを読み込み中" in tidy.panel.label.text() and tidy.panel.cancel_button.isVisible()
    gate.load.set()
    assert wait_for(lambda: tidy.panel.draft is not None)
    assert tidy.panel.draft.text == "岸壁に入港、停泊中。3 隻"
    assert tidy.panel.label.text().startswith("案") and "岸壁に入港、停泊中。3 隻" in tidy.panel.label.text()
    assert tidy.panel.warning.isVisible() and tidy.panel.warning.text() == "確認: 元にない数字: 3"
    assert tidy.panel.replace_button.isVisible() and not tidy.panel.append_button.isVisible()
    messages, max_tokens, one_line = loaded[0].asked[0]
    assert messages[-1]["content"].startswith("種別: 船舶\n") and messages[-1]["content"].endswith("備考: 岸壁に入航してとまってる")
    assert (max_tokens, one_line) == (160, True) and loaded[0].folder == str(tmp_path / "models" / "sarashina")
    # Nothing changed until 「置き換える」; then as typed (Ctrl+Z takes it back), not saved.
    assert dock.note_input.text() == "岸壁に入航してとまってる"
    tidy.panel.replace_button.click()
    assert dock.note_input.text() == "岸壁に入港、停泊中。3 隻" and not tidy.panel.isVisible() and dock.form.isVisible()
    dock.note_input.undo()
    assert dock.note_input.text() == "岸壁に入航してとまってる"
    # Again: the model kept loaded, and an answer the same as the 備考 says so.
    dock.note_input.setText("岸壁に停泊中")
    tidy.tidy()
    assert tidy.panel.label.text().startswith("作成中")
    assert wait_for(lambda: tidy.panel.label.text() == "直すところなし")
    assert len(loaded) == 1 and a.writer is loaded[0]


def test_another_note_typed_drops_the_proposal_and_one_request_at_a_time(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["整えた備考。", "もう一つ"])
    tidy = _record_form(dock)
    dock.note_input.setText("とまってる")
    gate.clear()
    tidy.tidy()
    assert wait_for(lambda: a.busy and loaded)
    # The 確認範囲's 備考 meanwhile: refused, and said.
    dock.area_note.setText("北側は雲")
    assert not dock.area_note_tidy.tidy()
    assert "ほかの文章を作成中" in dock.area_note_tidy.panel.label.text()
    # The 備考 changed while it writes: cancelled, the panel gone.
    dock.note_input.setText("とまってる、2 隻")
    assert not tidy.panel.isVisible()
    gate.set()
    assert wait_for(lambda: not a.busy)
    assert not tidy.panel.isVisible() and dock.note_input.text() == "とまってる、2 隻"
    # 取消 while it writes.
    gate.clear()
    tidy.tidy()
    assert wait_for(lambda: a.busy)
    tidy.panel.cancel_button.click()
    assert wait_for(lambda: not a.busy) and not tidy.panel.isVisible()
    gate.set()


def test_the_model_is_let_go_after_a_while_and_when_the_settings_change(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, _gate = _assist(p, tmp_path, ["案。", "案。", "案。"], {"model": "models/a", "keepMinutes": 0, "maxTokens": 40})
    tidy = _record_form(dock)
    dock.note_input.setText("とまってる")
    tidy.tidy()
    assert wait_for(lambda: tidy.panel.draft is not None)
    assert a.writer is None and loaded[0].asked[0][1] == 40  # keepMinutes 0: let go at once
    _write_config(p, tmp_path, {"model": "models/a", "threads": 2})
    assert wait_for(lambda: a.settings.threads == 2)
    tidy.tidy()
    assert wait_for(lambda: a.writer is not None)
    assert len(loaded) == 2 and loaded[1].threads == 2
    # Another model: the one loaded is let go.
    _write_config(p, tmp_path, {"model": "models/b"})
    assert wait_for(lambda: a.settings.model == "models/b") and a.writer is None
    # No textAssist any more: the buttons go.
    _write_config(p, tmp_path, None)
    assert wait_for(lambda: not a.available) and not tidy.button.isVisible() and not tidy.panel.isVisible()
    a._idle.stop()


def test_a_model_that_fails_is_said_under_the_field(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, _gate = _assist(p, tmp_path, [])

    def broken(folder, threads):
        raise AiError("モデルを読み込めず（sarashina: 壊れている）")

    a._writer_of = broken
    tidy = _record_form(dock)
    dock.note_input.setText("とまってる")
    tidy.tidy()
    assert wait_for(lambda: tidy.panel.label.text().startswith("作成できず"))
    assert "壊れている" in tidy.panel.label.text() and tidy.panel.close_button.isVisible() and not a.busy
    tidy.panel.close_button.click()
    assert not tidy.panel.isVisible()


def test_the_handover_draft_is_made_from_the_memo_made_first(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, _gate = _assist(p, tmp_path, ["申し送り: 北側の 315 隻は未確認。次は港内から。", "入れ替え案。"])
    dock.book.add(139.7, 35.6, object="船A", kind="船舶", activity="航行")
    dialog = dock.open_handover(0)
    draft = dialog.memo_draft
    assert draft.button.isVisible()
    dialog.memo_edit.setPlainText("北側は未確認")
    draft.button.click()
    # No memo yet: made first, then the draft.
    assert "引き継ぎメモを作成中" in draft.panel.label.text()
    assert wait_for(lambda: draft.panel.draft is not None)
    assert dialog.text_edit.toPlainText().startswith("引き継ぎメモ")
    messages, max_tokens, one_line = loaded[0].asked[0]
    assert (max_tokens, one_line) == (320, False)
    assert messages[1]["content"].startswith("引き継ぎメモ:\n引き継ぎメモ") and "■ 申し送り" not in messages[1]["content"]
    assert messages[1]["content"].endswith("書きかけの申し送り（生かして書く）:\n北側は未確認")
    assert draft.panel.draft.text == "北側の 315 隻は未確認。次は港内から。"
    assert draft.panel.warning.text() == "確認: 元にない数字: 315"
    assert draft.panel.append_button.isVisible() and draft.panel.replace_button.isVisible()
    draft.panel.append_button.click()
    assert dialog.memo_edit.toPlainText() == "北側は未確認\n北側の 315 隻は未確認。次は港内から。" and not draft.panel.isVisible()
    # Again (the memo there now), replaced this time.
    draft.draft()
    assert wait_for(lambda: draft.panel.draft is not None)
    draft.panel.replace_button.click()
    assert dialog.memo_edit.toPlainText() == "入れ替え案。"
    assert len(loaded[0].asked) == 2 and "北側の 315 隻は未確認" in loaded[0].asked[1][0][1]["content"]
    dialog.close()


def test_cancel_while_the_memo_is_made_drops_the_draft(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, _gate = _assist(p, tmp_path, ["案。"])
    dialog = dock.open_handover(0)
    draft = dialog.memo_draft
    draft.button.click()
    assert draft.waiting and draft.panel.cancel_button.isVisible()
    draft.panel.cancel_button.click()
    assert not draft.waiting and not draft.panel.isVisible()
    assert wait_for(lambda: dialog.text_edit.toPlainText().startswith("引き継ぎメモ"))
    assert wait_for(lambda: dialog.make_button.isEnabled())
    assert not loaded and not draft.panel.isVisible()
    dialog.close()


def test_the_real_runtime_from_the_settings_to_the_proposal(genai, plugin, tmp_path):
    """The settings' model folder, ONNX Runtime GenAI and the tiny model: 「整える」 as on a PC (the folder used where it is)."""
    p, iface, dock, layers, _ = plugin
    write_model(str(tmp_path / "models" / "tiny"), "Tidy note.")
    _write_config(p, tmp_path, {"model": "models", "threads": 1})
    a = p.context.text_assist
    assert wait_for(lambda: a.available)
    assert wait_for(lambda: not p.iface.mapCanvas().isDrawing())
    tidy = _record_form(dock)
    dock.note_input.setText("とまってる")
    tidy.tidy()
    assert wait_for(lambda: tidy.panel.draft is not None or tidy.panel.label.text().startswith("作成できず"), 60000)
    assert tidy.panel.draft is not None, tidy.panel.label.text()
    assert tidy.panel.draft.text == "Tidy note." and isinstance(a.writer, text_assist_io.GenAiWriter)
    assert a.writer.folder == str(tmp_path / "models" / "tiny")
    a.unload()
