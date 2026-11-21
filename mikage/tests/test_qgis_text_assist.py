"""
文章の手伝い in QGIS (qgis_io/text_assist_io.py, ui/text_assist_ui.py): where
the model is, the real ONNX Runtime GenAI on a tiny model (tests/genai_tiny.py),
and each task on the screens with a model of the test's own (「整える」・
「申し送りの下書き」・「項目を拾う」・「言葉で探す」・「まとめ」・「備考を英訳」).
"""
import json
import os
import threading
import time
import zipfile

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtCore import Qt  # noqa: E402

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


def test_what_is_read_is_kept_for_the_next_conversation_that_begins_the_same(genai, tmp_path):
    """文で操作: the list read once (prime, or the first sentence), then only what follows it (Generator.rewind_to)."""
    writer = text_assist_io.GenAiWriter(write_model(str(tmp_path / "m"), "Op."), threads=1)
    head = [{"role": "system", "content": "操作の一覧"}]
    ask = lambda text: head + [{"role": "user", "content": f"依頼: {text}"}]  # noqa: E731
    plain = writer.write(ask("呉"), 16)
    writer.prime(ask(""))
    kept = writer._kept
    assert kept is not None and writer._held == list(writer.tokenizer.encode(writer.prompt(ask(""))))
    for text in ("呉の画像", "横須賀へ移動して", "呉"):
        assert writer.write(ask(text), 16, keep=True) == plain
        # The same generator, taken back to what the conversations share.
        assert writer._kept is kept
    # A conversation that does not fit what was kept: a new one.
    assert writer.write(ask("あ" * 400), 16, keep=True) == plain and writer._kept is not kept
    # Not kept: what was kept stays as it was.
    kept = writer._kept
    assert writer.write(ask("呉"), 16) == plain and writer._kept is kept


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

    def __init__(self, folder, threads, replies, gate, params=None):
        self.folder, self.threads = folder, threads
        self.replies = replies
        self.gate = gate
        self.asked = []
        #: What it was given to read ahead (prime).
        self.primed_with = []
        #: Its size (billions of parameters), as GenAiWriter reads it from genai_config.json.
        self.params = params

    def prime(self, messages):
        self.primed_with.append(list(messages))

    def write(self, messages, max_tokens, cancelled=lambda: False, wrote=lambda _t: None, one_line=False, until=None, keep=False):
        self.asked.append((list(messages), max_tokens, one_line))
        #: Whether what was read was to be kept (文で操作: GenAiWriter reads only what follows the list next time).
        self.kept = keep
        #: When the screen said the answer was enough (GenAiWriter stops there).
        self.until = until
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
            if until is not None and until(text):
                break
        return text


def _assist(p, tmp_path, replies, text_assist=None, glossary=False, extra=None, params=None):
    """
    The settings with a textAssist (and `extra` keys), and the model replaced
    by FakeWriter (the ones loaded listed; loaded once `gate.load` is set).
    """
    gate = threading.Event()
    gate.set()
    gate.load = threading.Event()
    gate.load.set()
    loaded = []
    a = p.context.text_assist

    def writer_of(folder, threads):
        gate.load.wait(10)
        w = FakeWriter(folder, threads, replies, gate, params)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    _write_config(p, tmp_path, text_assist or {"model": "models/sarashina"}, extra)
    assert wait_for(lambda: a.available)
    # The map drawn first (a test this short would otherwise end while it draws: QGIS 3.34 does not survive it).
    assert wait_for(lambda: not p.iface.mapCanvas().isDrawing())
    return a, loaded, gate


def _write_config(p, tmp_path, text_assist, extra=None):
    config = tmp_path / "config.json"
    value = {"textAssist": text_assist} if text_assist is not None else {}
    config.write_text(json.dumps({**value, **(extra or {})}), encoding="utf-8")
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
    assert wait_for(lambda: a.settings.models[0].threads == 2)
    tidy.tidy()
    assert wait_for(lambda: a.writer is not None)
    assert len(loaded) == 2 and loaded[1].threads == 2
    # Another model: the one loaded is let go.
    _write_config(p, tmp_path, {"model": "models/b"})
    assert wait_for(lambda: a.settings.models[0].path == "models/b") and a.writer is None
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
    assert messages[-1]["content"].startswith("引き継ぎメモ:\n引き継ぎメモ") and "■ 申し送り" not in messages[-1]["content"]
    assert messages[-1]["content"].endswith("書きかけの申し送り（生かして書く）:\n北側は未確認\n\n"
                                          "書くこと: 書きかけの申し送り（言い直すだけ。メモの一覧のことは書かない）")
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
    assert len(loaded[0].asked) == 2 and "北側の 315 隻は未確認" in loaded[0].asked[1][0][-1]["content"]
    dialog.close()


def test_a_handover_draft_of_only_what_the_memo_lacks_is_no_draft(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, _gate = _assist(p, tmp_path, ["通報したものは現時点ではありません。担当中の画像の作業を確認してください。"])
    dialog = dock.open_handover(1)
    draft = dialog.memo_draft
    # Nothing in the memo to tell and nothing written: the model is not asked.
    draft.button.click()
    assert wait_for(lambda: dialog.text_edit.toPlainText().startswith("引き継ぎメモ"))
    assert wait_for(lambda: draft.panel.label.text().startswith("申し送ることがメモにない"))
    assert not loaded and draft.panel.draft is None
    dialog.memo_edit.setPlainText("北側は未確認")
    draft.button.click()
    assert wait_for(lambda: draft.panel.label.text().startswith("案を作れず"))
    assert draft.panel.label.text() == "案を作れず（メモにない内容の文だけだった）" and draft.panel.draft is None
    assert draft.panel.warning.text().startswith("メモにない内容の文を除いた: 「通報したものは現時点ではありません。」")
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


# ---- a model for each task (textAssist.models / tasks; tsudo 2026-11-19) ----------------------------

TWO_MODELS = {"models": {"small": "models/sarashina-1b", "large": {"path": "models/sarashina-3b", "threads": 6}},
              "tasks": {"tidyNote": "small", "noteFields": "large"}}


def _choose(table, rows):
    from qgis.PyQt.QtCore import QItemSelectionModel

    sel = table.selectionModel()
    sel.clearSelection()
    for r in rows:
        sel.select(table.model().index(r, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)


def test_only_the_tasks_given_a_model_show_and_one_model_is_loaded_at_a_time(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["岸壁に停泊中。", "種別: 船舶", "岸壁に停泊中。"], TWO_MODELS)
    tidy = _record_form(dock)
    fields = dock.note_fields
    assert tidy.button.isVisible() and fields.button.isVisible()
    # No model for them: no 申し送りの下書き, no まとめ.
    dialog = dock.open_handover(0)
    assert not dialog.memo_draft.button.isVisible()
    dialog.close()
    dock.note_input.setText("とまってる")
    tidy.tidy()
    assert wait_for(lambda: tidy.panel.draft is not None)
    assert loaded[0].folder.endswith("sarashina-1b") and loaded[0].threads == 0 and a.writer is loaded[0]
    # Another task, another model: the first let go before the second is loaded.
    fields.pick()
    assert wait_for(lambda: len(loaded) == 2 and not a.busy)
    assert loaded[1].folder.endswith("sarashina-3b") and loaded[1].threads == 6 and a.writer is loaded[1]
    dock.note_input.setText("とまってる。")
    tidy.tidy()
    assert wait_for(lambda: len(loaded) == 3 and not a.busy) and a.writer is loaded[2]
    # The task taken from the settings: its button goes.
    _write_config(p, tmp_path, {"models": TWO_MODELS["models"], "tasks": {"tidyNote": "small"}})
    assert wait_for(lambda: fields.button.isHidden()) and not tidy.button.isHidden()
    a.unload()


def test_the_fields_read_from_the_note_fill_only_the_empty_ones(plugin, tmp_path):
    """tsudo 2026-11-19 「この方針で4つとも」: 備考から拾い、空いている欄にだけ入れる案 (and a 種別 made finer)."""
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["種別: 補給艦\n活動: 停泊\n確度: 推定\n長さ: 120\n幅: 30\n向き: なし\nモノ: おうみ"],
                              {"models": {"large": "models/sarashina-3b"}, "tasks": {"noteFields": "large"}}, params=3.4)
    # A class under 船舶 (from a record).
    dock.book.add(139.7, 35.6, object="ましゅう", kind="船舶 > 補給艦", activity="停泊")
    _record_form(dock)
    fields = dock.note_fields
    # 「整える」 has no model: only 「項目を拾う」 beside the 備考.
    assert fields.button.isVisible() and not dock.note_tidy.button.isVisible() and not fields.button.isEnabled()
    # As carried over from the record before.
    dock.choose_kind(["船舶"])
    dock.activity_input.setText("航行")
    dock.confidence_select.setCurrentIndex(dock.confidence_select.findData("確定"))
    dock.note_input.setText("補給艦おうみが岸壁に停泊、全長120m程度と推定")
    fields.button.click()
    assert wait_for(lambda: fields.panel.draft is not None)
    messages, max_tokens, one_line = loaded[0].asked[0]
    asked = messages[1]["content"]
    assert (max_tokens, one_line) == (160, False) and messages[0]["content"].endswith("向き: \nモノ: ")
    # The class the 備考 names first.
    assert asked.startswith("今の種別: 船舶\n種別の候補: 船舶 > 補給艦、船舶、航空機、") and "活動の候補（船舶）: 停泊、航行、入港" in asked
    assert "確度の候補: 確定、推定、不明" in asked and asked.endswith("備考: 補給艦おうみが岸壁に停泊、全長120m程度と推定")
    # It stops once its 7 lines are written.
    assert loaded[0].until is not None and loaded[0].until("種別: a\n活動: b\n確度: c\n長さ: d\n幅: e\n向き: f\nモノ: g\n")
    # The 種別 made finer; the 活動 and 確度 written are kept (shown only); 幅 30 is not in the 備考. A 3B model: no caution.
    assert fields.panel.label.text().endswith("種別 船舶 > 補給艦・長さ 120 m・モノ おうみ")
    assert fields.panel.warning.text() == ("確認: 幅 30 は備考にない数字。活動は入力済みのため変えない（案: 停泊）。"
                                           "確度は入力済みのため変えない（案: 推定）")
    assert fields.panel.replace_button.text() == "欄に入れる"
    fields.panel.replace_button.click()
    assert dock.kind_input.text() == "船舶 > 補給艦" and dock.length_input.text() == "120" and dock.object_input.text() == "おうみ"
    assert dock.activity_input.text() == "航行" and dock.confidence_select.currentData() == "確定" and dock.width_input.text() == ""
    assert not fields.panel.isVisible() and dock.note_input.text() == "補給艦おうみが岸壁に停泊、全長120m程度と推定"
    # All there already: nothing to put in.
    loaded[0].replies.append("種別: 補給艦\nモノ: おうみ")
    fields.pick()
    assert wait_for(lambda: fields.panel.label.text() == "空いている欄に入れる値なし")


def test_a_small_model_for_a_larger_task_is_cautioned(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["活動: 停泊"], {"models": {"m": "models/sarashina-1b"}, "tasks": {"noteFields": "m"}}, params=1.2)
    _record_form(dock)
    dock.note_input.setText("船が停泊")
    dock.note_fields.pick()
    assert wait_for(lambda: dock.note_fields.panel.draft is not None)
    assert dock.note_fields.panel.warning.text() == "確認: このモデル（約 1.2B）では精度が低いことがある（3B 級を推奨）"


PLACES = "https://x/arcgis/rest/services/Alerts/FeatureServer/3"


def test_words_made_into_the_export_conditions(plugin, tmp_path):
    from qgis.PyQt.QtCore import QDate, QTime

    from mikage.core.esri import EsriClient
    from mikage.core.obs.alerts import forget_named_areas
    from mikage.tests.fake_esri import FakeEsri, FakeLayer

    p, iface, dock, layers, _ = plugin
    forget_named_areas()
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeString"}, "esriGeometryPolygon"))
    places.add({"NAME": "東京湾", "KIND": "海域"}, {"rings": [[[139.5, 35.0], [140.1, 35.0], [140.1, 35.7], [139.5, 35.7], [139.5, 35.0]]]})
    places.add({"NAME": "横須賀港", "KIND": "港"}, {"rings": [[[139.65, 35.25], [139.75, 35.25], [139.75, 35.35], [139.65, 35.35], [139.65, 35.25]]]})
    p.context.esri = lambda: EsriClient(fake)
    a, loaded, gate = _assist(p, tmp_path, ["期間: 2026-11-09 〜 2026-11-15\n地名: 横須賀港\n大分類: 船舶、潜水艦\n確度: 推定以上\n有無: あり"],
                              {"models": {"large": "models/sarashina-3b"}, "tasks": {"searchWords": "large"}},
                              extra={"observationExports": {"namedAreas": PLACES}})
    dock.book.add(139.7, 35.3, object="船A", kind="船舶", activity="航行")
    dock.book.add(139.9, 35.5, object="機B", kind="航空機", activity="駐機")
    dialog = dock.open_export()
    words = dialog.search_words
    assert dialog.search_label.isVisible() and words.edit.isVisible() and not words.button.isEnabled()
    assert not dialog.summary_button.isVisible()
    words.edit.setText("11月9日から15日の横須賀港の船舶、確度は推定以上でいたもの")
    words.button.click()
    # The place names read first, then asked.
    assert wait_for(lambda: words.panel.draft is not None)
    asked = loaded[0].asked[0][0][1]["content"].split("\n")
    assert asked[0].startswith("今日: ") and asked[1].startswith("日付の目安: 今日 ")
    assert asked[2:] == ["地名の候補: 東京湾、横須賀港", "大分類の候補: 船舶、航空機、車両、建物、構造物、その他", "確度の候補: 確定、推定、不明",
                         "依頼: 11月9日から15日の横須賀港の船舶、確度は推定以上でいたもの"]
    # The model's dates (the words name a date); the 大分類 the words name (not the model's 潜水艦).
    assert words.panel.label.text().endswith("期間 2026-11-09〜2026-11-15・地名 横須賀港・大分類 船舶・確度 推定・有無 ありのみ")
    assert words.panel.warning.isHidden() and words.panel.replace_button.text() == "条件に入れる"
    assert loaded[0].until is not None
    # Nothing changed until 「条件に入れる」.
    assert not dialog.start_check.isChecked() and dialog.place_select.currentData() != "named"
    words.panel.replace_button.click()
    assert dialog.start_check.isChecked() and dialog.end_check.isChecked()
    assert dialog.start_edit.dateTime().date() == QDate(2026, 11, 9) and dialog.start_edit.dateTime().time() == QTime(0, 0)
    assert dialog.end_edit.dateTime().date() == QDate(2026, 11, 15) and dialog.end_edit.dateTime().time().hour() == 23
    assert dialog.place_select.currentData() == "named" and dialog.named_select.currentData() == "横須賀港"
    checked = [dialog.root_list.item(i).text() for i in range(dialog.root_list.count())
               if dialog.root_list.item(i).checkState() == Qt.CheckState.Checked]
    assert checked == ["船舶"] and not dialog.all_roots.isChecked()
    assert dialog.confidence_select.currentData() == "推定" and dialog.presence_select.currentData() == "present"
    assert not words.panel.isVisible()
    dialog.close()


def test_the_summary_of_the_records_chosen_and_of_the_export(plugin, tmp_path):
    from qgis.PyQt.QtWidgets import QApplication

    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["まとめ: 船舶 2 件が停泊、ほかに 5 隻。", "条件に合う 2 件のまとめ。"],
                              {"models": {"large": "models/sarashina-3b"}, "tasks": {"summary": {"model": "large", "maxTokens": 500}}})
    book = dock.book
    for name, lon in (("あおば", 139.65), ("いぶき", 139.7)):
        book.add(lon, 35.6, object=name, kind="船舶", activity="停泊", note="岸壁")
    dock.render()
    assert dock.table.rowCount() == 2 and not dock.summary_button.isVisible()
    _choose(dock.table, [0, 1])
    assert dock.summary_button.isVisible()
    dialog = dock.summarize()
    assert wait_for(lambda: dialog.copy_button.isEnabled())
    messages, max_tokens, _one = loaded[0].asked[0]
    assert max_tokens == 500 and messages[1]["content"].startswith("観測記録:\n記録: 2 件（あり 2・消失 0）\n船舶: 2 件（停泊 2）")
    assert dialog.text_edit.toPlainText() == "船舶 2 件が停泊、ほかに 5 隻。" and "選んだ記録: 2 件" in dialog.head.text()
    assert dialog.panel.warning.text() == "確認: 元にない数字: 5"
    dialog.copy()
    assert QApplication.clipboard().text() == "船舶 2 件が停泊、ほかに 5 隻。"
    dialog.close()
    # From the export: the records that meet its conditions.
    export = dock.open_export()
    assert export.summary_button.isVisible() and export.summary_button.isEnabled() and not export.search_label.isVisible()
    dialog = export.summarize()
    assert wait_for(lambda: dialog.copy_button.isEnabled())
    assert dialog.text_edit.toPlainText() == "条件に合う 2 件のまとめ。" and "条件に合う記録: 2 件" in dialog.head.text()
    dialog.close()
    export.close()


def test_the_notes_in_english_for_a_format_that_writes_them(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    a, loaded, gate = _assist(p, tmp_path, ["English: Moored at the quay.", "Two vessels sailing north, 3 tugs."],
                              {"models": {"large": "models/sarashina-3b"}, "tasks": {"translate": "large"}},
                              extra={"alertReports": {"default": "英文", "formats": [
                                  {"name": "英文", "fields": ["objectName", {"source": "note.en", "fallback": {"source": "note"}}]},
                                  {"name": "和文", "fields": ["objectName", "note"]}]}})
    book = dock.book
    records = [book.add(139.7, 35.6, object="船A", kind="船舶", activity="停泊", note="岸壁に停泊中"),
               book.add(139.7, 35.6, object="船B", kind="船舶", activity="航行", note="2 隻が北上"),
               book.add(139.7, 35.6, object="船C", kind="船舶", activity="停泊", note="岸壁に停泊中"),
               book.add(139.7, 35.6, object="船D", kind="船舶", activity="停泊")]
    dialog = dock.add_to_alerts(records)
    assert wait_for(lambda: dialog.lines_table.rowCount() == 4)
    button = dialog.translate_button
    assert button.isVisible() and button.isEnabled()

    def lines():
        return sorted(dialog.lines_table.item(r, 1).text() for r in range(dialog.lines_table.rowCount()))

    # Not translated yet: the format's fallback (the 備考).
    assert lines() == ["船A|岸壁に停泊中", "船B|2 隻が北上", "船C|岸壁に停泊中", "船D|"]
    button.click()
    assert wait_for(lambda: "件の備考を英訳" in dialog.status.text())
    # Each 備考 once, one line each.
    assert [c[0][1]["content"] for c in loaded[0].asked] == ["Note: 岸壁に停泊中\nTerms: 岸壁 = quay; 停泊 = moored",
                                                            "Note: 2 隻が北上\nTerms: 北上 = heading north"]
    assert all(one_line for _m, _n, one_line in loaded[0].asked)
    assert lines() == ["船A|Moored at the quay.", "船B|Two vessels sailing north, 3 tugs.", "船C|Moored at the quay.", "船D|"]
    row = next(r for r in range(4) if dialog.lines_table.item(r, 1).text().startswith("船B"))
    assert "元にない数字: 3" in dialog.lines_table.item(row, 1).toolTip()
    # All translated: nothing more to do.
    assert not button.isEnabled() and button.text() == "備考を英訳"
    # A format without note.en: no button.
    dialog.format_select.setCurrentIndex(dialog.format_select.findData("和文"))
    assert wait_for(lambda: not button.isVisible())
    dialog.close()
