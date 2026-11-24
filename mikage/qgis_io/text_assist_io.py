"""
文章の手伝い (core/text_assist.py) on this computer: the settings' model run
by ONNX Runtime GenAI, off the main thread, one request at a time.

ONNX Runtime GenAI is not part of QGIS. It is used when the Python of QGIS
has it (`pip install onnxruntime-genai`); the Windows zip carries one for
QGIS's Python 3.12 (ext/win64, scripts/build_zip.py --win64), loaded from a
copy outside the plugin's folder (core/win64.add_package) like ONNX Runtime
itself. ONNX Runtime is loaded first (qgis_io/ai_runtime.load_ort, its
telemetry off): GenAI then runs on that onnxruntime.dll. GenAI's own
telemetry is switched off as well.

The model is a folder (genai_config.json, model.onnx, its data and the
tokenizer's files), a .zip of one, or the URL of such a .zip:

- a folder on this computer is used where it is;
- a folder on a network share (\\\\server\\share, a mapped network drive) is
  copied once into the QGIS profile (reading a 1 GB model over the network
  each time QGIS starts would be slow); a new copy is made when its files
  change;
- a .zip (a path or a URL) is unpacked once into the profile (a URL is
  downloaded first, through QGIS's network).

A model on a server (`url`, データに質問 on an internal GPU server: an
OpenAI-compatible chat API such as vLLM's or llama.cpp's) is asked through
QGIS's network (ServerWriter): nothing is loaded on this computer.

Each task is given one of the settings' models (`textAssist.tasks`). One
model is loaded at a time: a task given another model lets the loaded one
go first (a few seconds to load the other). The model stays loaded
`keepMinutes` after its last use and is then let go (its memory given
back). Generation releases the GIL, so QGIS keeps answering while it
writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core.text_assist import (CHAT_MODEL, CHAT_TASKS, LARGE_MODEL, LARGE_TASKS, Message, TextAssistSettings, TextModel,
                                 plain_prompt)
from ..core.win64 import NotCopied, add_package, keep_off_path, warn_in_place
from .ai_runtime import (NOT_COPIED, VENDORED, VENDORED_PYTHON, AiError, RuntimeMissing, cache_path, download, is_remote,
                         load_ort, resolve_source, threads as default_threads)
from .tasks import Cancelled, run_task

#: The file that makes a folder a GenAI model.
CONFIG_FILE = "genai_config.json"
#: How often the screen shows the text written so far (ms).
SHOW_MS = 120


def install_hint(system: Optional[str] = None) -> str:
    """How to add ONNX Runtime GenAI to QGIS's Python, on this system."""
    system = system or platform.system()
    if system == "Windows":
        return "Windows 版の zip（onnxruntime-genai 同梱）を使うか、OSGeo4W Shell で「python -m pip install onnxruntime-genai」を実行"
    if system == "Darwin":
        return "ターミナルで「/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install onnxruntime-genai」を実行"
    return "QGIS が使う Python に「python3 -m pip install onnxruntime-genai」で追加"


_og: Any = None


def load_genai(vendored: str = VENDORED, system: Optional[str] = None) -> Any:
    """ONNX Runtime GenAI, imported once (after ONNX Runtime): QGIS's own, else the one carried by the Windows zip."""
    global _og
    if _og is not None:
        return _og
    load_ort(vendored, system)
    keep_off_path(vendored)  # As load_ort: QGIS's own looked for without the Windows zip's folder (core/win64).
    try:
        import onnxruntime_genai as og  # noqa: PLC0415 (large, and most sessions never need it)
    except ImportError:
        og = None
    if og is not None:
        warn_in_place(og, vendored)
    if og is None and os.path.isdir(os.path.join(vendored, "onnxruntime_genai")):
        if sys.version_info[:2] != VENDORED_PYTHON:
            raise RuntimeMissing(code="E602", message=f"同梱の onnxruntime-genai は Python {VENDORED_PYTHON[0]}.{VENDORED_PYTHON[1]} 用"
                                 f"（この QGIS は {sys.version_info[0]}.{sys.version_info[1]}）。{install_hint(system)}")
        try:
            add_package("onnxruntime_genai", vendored, first=True)  # From a copy outside the plugin's folder (core/win64).
        except NotCopied as error:
            raise RuntimeMissing(f"{error}。{NOT_COPIED}", code="E602") from error
        try:
            import onnxruntime_genai as og  # noqa: PLC0415
        except ImportError as error:
            raise RuntimeMissing(f"同梱の onnxruntime-genai を読み込めない。{install_hint(system)}", code="E602", detail=str(error)) from error
    if og is None:
        raise RuntimeMissing(f"文章の手伝いに必要な onnxruntime-genai なし。{install_hint(system)}", code="E602")
    try:
        og.disable_telemetry_events()
    except Exception:  # noqa: BLE001 (a version without it)
        pass
    _og = og
    return og


# ---- where the model is ----------------------------------------------------------------------

def models_folder() -> str:
    """The profile's folder of models copied or unpacked for 文章の手伝い."""
    from . import settings

    folder = os.path.join(settings.profile_folder(), "text_models")
    os.makedirs(folder, exist_ok=True)
    return folder


def is_network_path(path: str, system: Optional[str] = None) -> bool:
    """Whether `path` is on a network share (UNC, or a mapped network drive on Windows)."""
    if path.startswith(("\\\\", "//")):
        return True
    if (system or platform.system()) != "Windows" or not re.match(r"^[A-Za-z]:", path):
        return False
    try:
        import ctypes  # noqa: PLC0415

        return ctypes.windll.kernel32.GetDriveTypeW(path[:2] + "\\") == 4  # DRIVE_REMOTE
    except Exception:  # noqa: BLE001
        return False


def config_folder(folder: str) -> Optional[str]:
    """The folder holding genai_config.json: `folder` itself or its only subfolder that has one."""
    if os.path.isfile(os.path.join(folder, CONFIG_FILE)):
        return folder
    found = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
             if os.path.isfile(os.path.join(folder, n, CONFIG_FILE))] if os.path.isdir(folder) else []
    return found[0] if len(found) == 1 else None


def _stamp(paths: Sequence[str]) -> str:
    h = hashlib.sha1()
    for p in paths:
        st = os.stat(p)
        h.update(f"{p}\0{st.st_size}\0{int(st.st_mtime)}\0".encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:12]


def _tag(source: str) -> str:
    """A copy's name's start, the same for every version of one source."""
    return hashlib.sha1(source.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _forget_older(root: str, tag: str, keep: str) -> None:
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.startswith(tag + "-") and path != keep and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)  # A model loaded by another QGIS stays (Windows refuses), that is all.


def _made(root: str, name: str, fill: Callable[[str], None]) -> str:
    """`root`/`name`, filled by `fill(folder)` in a temporary folder first (whole or not at all)."""
    target = os.path.join(root, name)
    if os.path.isdir(target):
        return target
    work = tempfile.mkdtemp(prefix=f".{name}-", dir=root)
    try:
        fill(work)
        try:
            os.rename(work, target)
        except OSError:
            if not os.path.isdir(target):
                raise
    finally:
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
    return target


def unpacked(zip_path: str, root: str, source: str = "") -> str:
    """The model folder of a .zip, unpacked once into `root` (again when the file changes)."""
    tag = _tag(source or zip_path)
    name = f"{tag}-{_stamp([zip_path])}"

    def fill(work: str) -> None:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                parts = info.filename.replace("\\", "/").split("/")
                if info.filename.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", info.filename):
                    raise AiError("モデルの zip を読めない", code="E604", detail=f"不正なパス {info.filename}")
            z.extractall(work)

    try:
        target = _made(root, name, fill)
    except zipfile.BadZipFile as error:
        raise AiError(f"モデルの zip を開けない（{os.path.basename(zip_path)}）", code="E604", detail=str(error)) from error
    _forget_older(root, tag, target)
    found = config_folder(target)
    if found is None:
        raise AiError(f"モデルの zip に {CONFIG_FILE} なし（{os.path.basename(zip_path)}）", code="E604")
    return found


def copied(folder: str, root: str) -> str:
    """A copy of the model folder `folder` in `root` (made once; again when its files change)."""
    files = sorted(os.path.join(folder, n) for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n)))
    tag = _tag(folder)
    target = _made(root, f"{tag}-{_stamp(files)}", lambda work: [shutil.copy2(p, work) for p in files])
    _forget_older(root, tag, target)
    return target


def model_folder_of(source: str, base: str = "", progress: Optional[Callable[[float], None]] = None,
                    cancelled: Callable[[], bool] = lambda: False, root: Optional[str] = None) -> str:
    """The local folder of the model `source` (as written in the settings), copied, unpacked or downloaded when needed."""
    whole = resolve_source(source, base)
    if is_remote(whole):
        if not urlsplit_path(whole).lower().endswith(".zip"):
            raise AiError(f"URL のモデルは .zip で指定（{whole.split('?', 1)[0]}）")
        from . import settings

        root = root or models_folder()
        zip_path = cache_path(whole, root)
        if not (os.path.isfile(zip_path) and os.path.getsize(zip_path) > 0):
            download(whole, zip_path, settings.auth_for(whole), progress, cancelled)
        return unpacked(zip_path, root, whole)
    if os.path.isfile(whole) and whole.lower().endswith(".zip"):
        return unpacked(whole, root or models_folder(), whole)
    if os.path.isdir(whole):
        folder = config_folder(whole)
        if folder is None:
            raise AiError(f"モデルのフォルダに {CONFIG_FILE} なし", code="E603", detail=whole)
        return copied(folder, root or models_folder()) if is_network_path(folder) else folder
    raise AiError("モデルなし", code="E603", detail=whole)


def urlsplit_path(url: str) -> str:
    return urlsplit(url).path


# ---- the model ------------------------------------------------------------------------------------

def params_of(model: dict) -> Optional[float]:
    """
    About how many parameters (billions) a model has, from genai_config.json's
    `model`: 12·layers·hidden² (attention and feed-forward) plus the
    vocabulary's embeddings. Enough to tell a 1B model from a 3B one.
    """
    try:
        d = model["decoder"]
        h, layers, vocab = int(d["hidden_size"]), int(d["num_hidden_layers"]), int(model.get("vocab_size") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return (12 * layers * h * h + vocab * h) / 1e9


class GenAiWriter:
    """A GenAI model and its tokenizer (core/text_assist.Writer): writes one reply at a time."""

    def __init__(self, folder: str, threads: int = 0):
        og = load_genai()
        self.folder = folder
        config = og.Config(folder)
        n = threads or default_threads()
        try:
            config.overlay(json.dumps({"model": {"decoder": {"session_options": {"intra_op_num_threads": n}}}}))
        except Exception:  # noqa: BLE001 (a version without overlay: the model's own setting)
            pass
        try:
            self.model = og.Model(config)
            self.tokenizer = og.Tokenizer(self.model)
        except Exception as error:  # noqa: BLE001
            raise AiError(f"モデルを読めない（{os.path.basename(folder)}）", code="E604", detail=str(error).splitlines()[0] if str(error) else repr(error)) from error
        self.context_length = 4096
        #: Its size (billions of parameters, from its settings; None when they do not say).
        self.params: Optional[float] = None
        try:
            with open(os.path.join(folder, CONFIG_FILE), encoding="utf-8") as f:
                model = json.load(f)["model"]
            self.context_length = int(model.get("context_length") or 4096)
            self.params = params_of(model)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        #: Whether its chat template can think first (Qwen3's 「<think>」): it is told not to (a short reply, and quick).
        self.thinks = _template_thinks(folder)
        self._lock = threading.Lock()
        self._og = og
        #: The generator kept with what it has read (文で操作: the list of operations read once), and those tokens.
        self._kept: Any = None
        self._held: list[int] = []
        self._kept_length = 0

    def prompt(self, messages: Sequence[Message]) -> str:
        """The conversation in the model's own chat template (a model whose template has no system turn: the instructions in the first)."""
        def apply(ms) -> str:
            text = self.tokenizer.apply_chat_template(messages=json.dumps(list(ms), ensure_ascii=False), add_generation_prompt=True)
            return no_thinking(text) if self.thinks else text

        try:
            return apply(messages)
        except Exception:  # noqa: BLE001 (no template, or one without a system turn)
            pass
        if messages and messages[0]["role"] == "system" and len(messages) > 1:
            folded = [dict(messages[1], content=messages[0]["content"] + "\n\n" + messages[1]["content"]), *messages[2:]]
            try:
                return apply(folded)
            except Exception:  # noqa: BLE001
                pass
        return plain_prompt(messages)

    def write(self, messages: Sequence[Message], max_tokens: int, cancelled: Callable[[], bool] = lambda: False,
              wrote: Callable[[str], None] = lambda _t: None, one_line: bool = False,
              until: Optional[Callable[[str], bool]] = None, keep: bool = False) -> str:
        """
        The reply (greedy: the same every time); `one_line`: ends at the
        first line break after some text; `until`: ends once it says the
        text so far is enough (all the lines asked for written …); `keep`:
        what is read is kept, and the next conversation kept that begins the
        same reads only what follows (文で操作: the list of operations is read
        once, about 8 s for a 3B model, then 0.5–1 s for each sentence).
        """
        with self._lock:
            ids = list(self.tokenizer.encode(self.prompt(messages)))
            if len(ids) + max_tokens > self.context_length:
                raise AiError(f"入力が長すぎる（{len(ids)} トークン、モデルは {self.context_length} まで）", code="E608")
            generator = self._read(ids, max_tokens, keep)
            stream = self.tokenizer.create_stream()
            text = ""
            try:
                for _ in range(max_tokens):
                    if generator.is_done() or cancelled():
                        break
                    generator.generate_next_token()
                    token = int(generator.get_next_tokens()[0])
                    if keep:
                        self._held.append(token)
                    text += stream.decode(token)
                    wrote(text)
                    if one_line and "\n" in text.lstrip():
                        break
                    if until is not None and until(text):
                        break
            except Exception:
                if keep:
                    self._drop()
                raise
            if cancelled():
                raise Cancelled()
            return text

    def prime(self, messages: Sequence[Message], room: int = 64) -> None:
        """Reads `messages` and keeps it (as write(keep=True) does, writing nothing): the next kept conversation that begins the same is quick."""
        with self._lock:
            ids = list(self.tokenizer.encode(self.prompt(messages)))
            if len(ids) + room <= self.context_length:
                self._read(ids, room, True)

    def _drop(self) -> None:
        self._kept, self._held, self._kept_length = None, [], 0

    def _read(self, ids: list[int], max_tokens: int, keep: bool) -> Any:
        """
        A generator that has read `ids`: a new one, or (`keep`) the one kept,
        taken back to what it has read in common with `ids` (rewind_to) and
        given the rest; one too short for what is asked is made again.
        """
        og = self._og
        if keep and self._kept is not None and len(ids) + max_tokens <= self._kept_length:
            same = 0
            for a, b in zip(ids, self._held):
                if a != b:
                    break
                same += 1
            same = min(same, len(ids) - 1)
            try:
                self._kept.rewind_to(same)
                self._kept.append_tokens(ids[same:])
                self._held = ids[:]
                if not self._kept.is_done():
                    return self._kept
            except Exception:  # noqa: BLE001 (a version without rewind_to, or a generator that cannot go back: a new one)
                pass
            self._drop()
        if not keep:
            params = og.GeneratorParams(self.model)
            params.set_search_options(max_length=len(ids) + max_tokens, do_sample=False)
            generator = og.Generator(self.model, params)
            generator.append_tokens(ids)
            return generator
        # Room for the next ones (a sentence longer than this one) without making the generator again.
        length = min(self.context_length, max(1024, len(ids) + max_tokens + 256))
        params = og.GeneratorParams(self.model)
        params.set_search_options(max_length=length, do_sample=False)
        self._drop()
        generator = og.Generator(self.model, params)
        generator.append_tokens(ids)
        self._kept, self._held, self._kept_length = generator, ids[:], length
        return generator


def _template_thinks(folder: str) -> bool:
    """Whether the model's chat template has the switch for thinking first (enable_thinking: Qwen3, Qwen3.5 …)."""
    for name in ("chat_template.jinja", "tokenizer_config.json"):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                if "enable_thinking" in f.read():
                    return True
        except OSError:
            continue
    return False


#: What a template that thinks first ends with, and what it writes instead when told not to (enable_thinking false).
_THINK_OPEN = "<think>\n"
_NO_THINKING = "<think>\n\n</think>\n\n"


def no_thinking(prompt: str) -> str:
    """The prompt as a template told not to think first makes it (GenAI's apply_chat_template cannot be told)."""
    if prompt.endswith(_NO_THINKING):
        return prompt
    if prompt.endswith(_THINK_OPEN):
        return prompt[:-len(_THINK_OPEN)] + _NO_THINKING
    if prompt.endswith("assistant\n"):
        return prompt + _NO_THINKING
    return prompt


def without_thinking(text: str) -> str:
    """A reply without what the model thought first (<think>…</think>, or an unclosed <think> to the end)."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    return re.sub(r"<think>.*", "", text, flags=re.DOTALL) if "<think>" in text else text


class ServerWriter:
    """
    A model on a server (core/text_assist.Writer): its OpenAI-compatible
    chat API asked through QGIS's network (its proxy and the authcfg chosen
    for the URL), off the main thread. The reply comes as a stream (the
    server sends as it writes, so QGIS's network timeout does not end a long
    one); the server keeps what it has read itself (vLLM's and llama.cpp's
    prefix caching), so `keep` and `prime` ask nothing more.
    """

    def __init__(self, model: TextModel, send: Optional[Callable[[str, dict, Optional[str], int], bytes]] = None,
                 auth_for: Optional[Callable[[str], Optional[str]]] = None):
        self.model = model
        self.url = model.chat_url
        self._send = send or _post
        self._auth_for = auth_for or _auth_for
        #: A server's model: its size is not known here (nothing said of it).
        self.params: Optional[float] = None
        self.context_length = 1 << 20

    def body(self, messages: Sequence[Message], max_tokens: int) -> dict:
        body = {"messages": [dict(m) for m in messages], "max_tokens": int(max_tokens), "temperature": 0, "stream": True,
                # Qwen3 and its kind: no thinking first (vLLM's and llama.cpp's servers pass it to the template).
                "chat_template_kwargs": {"enable_thinking": False}}
        if self.model.remote:
            body["model"] = self.model.remote
        return body

    def write(self, messages: Sequence[Message], max_tokens: int, cancelled: Callable[[], bool] = lambda: False,
              wrote: Callable[[str], None] = lambda _t: None, one_line: bool = False,
              until: Optional[Callable[[str], bool]] = None, keep: bool = False) -> str:
        from ..core.esri import EsriError

        if cancelled():
            raise Cancelled()
        try:
            raw = self._send(self.url, self.body(messages, max_tokens), self._auth_for(self.url), int(self.model.timeout * 1000))
        except Cancelled:
            raise
        except (EsriError, OSError, RuntimeError) as error:
            raise AiError(code="E611", detail=f"{self.url.split('?', 1)[0]}: {error}") from error
        text = without_thinking(reply_text(raw))
        if cancelled():
            raise Cancelled()
        if one_line and "\n" in text.lstrip():
            text = text.lstrip().split("\n", 1)[0] + "\n"
        wrote(text)
        return text

    def prime(self, messages: Sequence[Message], room: int = 64) -> None:
        """Nothing: the server keeps what it has read (its prefix cache)."""


def reply_text(raw: bytes) -> str:
    """The text of a chat API's answer: a stream of `data: {…}` lines (choices[0].delta.content), or one JSON object."""
    text = raw.decode("utf-8", errors="replace")
    out = []
    streamed = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        streamed = True
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            choice = (json.loads(data).get("choices") or [{}])[0]
        except (ValueError, AttributeError, IndexError):
            continue
        part = (choice.get("delta") or {}).get("content") or (choice.get("message") or {}).get("content") or choice.get("text") or ""
        out.append(part if isinstance(part, str) else "")
    if streamed:
        return "".join(out)
    try:
        value = json.loads(text)
    except ValueError as error:
        raise AiError(code="E611", detail=f"答えが JSON ではない: {text[:200]}") from error
    if isinstance(value, dict) and value.get("error"):
        raise AiError(code="E611", detail=str(value.get("error"))[:300])
    try:
        choice = value["choices"][0]
        return (choice.get("message") or {}).get("content") or choice.get("text") or ""
    except (KeyError, IndexError, TypeError) as error:
        raise AiError(code="E611", detail=f"答えに choices がない: {text[:200]}") from error


def _post(url: str, body: dict, authcfg: Optional[str], limit_ms: int) -> bytes:
    from .network import post_json

    return post_json(url, body, authcfg, limit_ms)


def _auth_for(url: str) -> Optional[str]:
    from . import settings

    return settings.auth_for(url)


# ---- for the screens ----------------------------------------------------------------------------------

class _Waiting:
    """The task of an ask waiting for the model to be made ready: cancelling it drops the ask."""

    def __init__(self, asked: "Asked"):
        self.asked = asked

    def cancel(self) -> None:
        self.asked.extra["cancelled"] = True


@dataclass
class Asked:
    """One request: the text written so far (read by the screen's timer; one str, no lock needed)."""
    text: str = ""
    #: What is happening now, for the screen (「モデルを読み込み中」 …).
    stage: str = ""
    seconds: float = 0.0
    task: Any = None
    extra: dict = field(default_factory=dict)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


class TextAssistant(QObject):
    """The settings' `textAssist` for the screens (on the context): one model loaded, one request at a time (main thread)."""

    #: The settings changed (which tasks show).
    changed = pyqtSignal()

    def __init__(self, context, writer_of: Optional[Callable[[str, int], Any]] = None,
                 folder_of: Optional[Callable[..., str]] = None, server_of: Optional[Callable[[TextModel], Any]] = None):
        super().__init__(context)
        self.context = context
        #: How a model is loaded and found, and how a server's is asked (tests give their own).
        self._writer_of = writer_of or GenAiWriter
        self._folder_of = folder_of or model_folder_of
        self._server_of = server_of or ServerWriter
        self.writer: Any = None
        self._writer_key: Optional[tuple] = None
        self.asked: Optional[Asked] = None
        #: An ask made while the model is made ready (prime), started once it is.
        self._waiting: Optional[Callable[[], None]] = None
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self.unload)
        context.configChanged.connect(self._config_changed)

    @property
    def settings(self) -> Optional[TextAssistSettings]:
        return getattr(self.context.config, "text_assist", None)

    @property
    def configured(self) -> bool:
        """Whether the settings give any task a model (the person can then turn the features on)."""
        s = self.settings
        return s is not None and any(s.has(t.name) for t in s.tasks)

    @property
    def enabled(self) -> bool:
        """Whether the person turned the generative AI's features on (担当と個人設定; off until then: opt-in)."""
        from .prefs import ai

        return ai.text_assist_on()

    def set_enabled(self, on: bool) -> None:
        """Turns the features on or off for this person: their buttons show or go at once (off: the model let go)."""
        from .prefs import ai

        if on == self.enabled:
            return
        ai.set_text_assist_on(on)
        if not on:
            if self.asked is not None:
                self.asked.cancel()
            self.unload()
        self.changed.emit()

    @property
    def available(self) -> bool:
        """Whether any task is given a model and the person uses them."""
        return self.configured and self.enabled

    def has(self, task: str) -> bool:
        """Whether `task` is given a model and the person uses the features (its button shows)."""
        s = self.settings
        return s is not None and s.has(task) and self.enabled

    @property
    def busy(self) -> bool:
        return self.asked is not None

    def _key(self, m: TextModel) -> tuple:
        return (m.path, self.context.source, m.threads, m.url, m.remote, m.timeout)

    def _make(self, model: TextModel, base: str, job) -> Any:
        """The writer of `model` (in the task): a server's, or the model loaded from its folder."""
        if model.on_server:
            return self._server_of(model)
        folder = self._folder_of(model.path, base, cancelled=job.isCanceled)
        job.check()
        return self._writer_of(folder, model.threads)

    def on_server(self, task: str) -> bool:
        """Whether `task`'s model is a server's."""
        s = self.settings
        m = s.model_for(task) if s is not None else None
        return m is not None and m.on_server

    def _config_changed(self) -> None:
        s = self.settings
        keys = {self._key(m) for m in s.models} if s is not None else set()
        if self.writer is not None and self._writer_key not in keys:
            self.unload()
        self.changed.emit()

    def unload(self) -> None:
        """Lets the model go (its memory is given back once nothing writes with it)."""
        self._idle.stop()
        self.writer, self._writer_key = None, None

    def caution(self, task: str) -> str:
        """What to say of the loaded model for `task`: a small one for a task meant for about 3B ("" when nothing)."""
        params = getattr(self.writer, "params", None)
        if task in LARGE_TASKS and params is not None and params < LARGE_MODEL:
            return self.tr("このモデル（約 {p:.1f}B）では精度が低いことがある（3B 級を推奨）").format(p=params)
        if task in CHAT_TASKS and params is not None and params < CHAT_MODEL:
            return self.tr("このモデル（約 {p:.1f}B）では精度が低いことがある（9B 級を推奨）").format(p=params)
        return ""

    def ask(self, task: str, title: str, messages: Sequence[Message], done: Callable[[str, float], None],
            failed: Callable[[str], None], one_line: bool = False,
            until: Optional[Callable[[str], bool]] = None, keep: bool = False,
            max_tokens: Optional[int] = None) -> Optional[Asked]:
        """
        Starts writing for `task` (in a task; `until`: when it has written
        enough; `keep`: what is read is kept for the next ask that begins
        the same, GenAiWriter.write; `max_tokens`: other than the task's);
        None (and `failed` told) when it cannot start.
        """
        return self._start(task, title, [messages], lambda texts, seconds: done(texts[0], seconds), failed, one_line, False,
                           until, keep, max_tokens=max_tokens)

    @property
    def priming(self) -> bool:
        """Whether the model is being made ready (prime): an ask made meanwhile starts once it is."""
        return self.asked is not None and bool(self.asked.extra.get("prime"))

    def prime(self, task: str, messages: Sequence[Message]) -> bool:
        """
        Loads `task`'s model (when it is not) and has it read `messages`
        ahead (文で操作: the list of operations, while the sentence is typed),
        quietly: nothing is said when it cannot. False when it does not start
        (busy, no model, or ready already).
        """
        s = self.settings
        model = s.model_for(task) if s is not None else None
        if model is None or self.asked is not None or not self.enabled:
            return False
        key, base = self._key(model), self.context.source
        writer = self.writer if self._writer_key == key else None
        if writer is not None and getattr(writer, "primed", None) == list(messages):
            return False
        if model.on_server:
            # Nothing to load: the server keeps what it has read.
            return False
        if writer is None and self.writer is not None:
            self.unload()
        asked = Asked(stage=self.tr("モデルを読み込み中…（初回は数秒〜）") if writer is None else self.tr("準備中…"), extra={"prime": True})

        make = self._make

        def work(job):
            w = writer
            if w is None:
                w = make(model, base, job)
            asked.stage = self.tr("準備中…")
            if hasattr(w, "prime"):
                w.prime(messages)
            w.primed = list(messages)
            return w

        def over(w) -> None:
            self.asked = None
            current = self.settings
            if w is not None and current is not None and key in {self._key(m) for m in current.models}:
                self.writer, self._writer_key = w, key
                if current.keep_minutes > 0:
                    self._idle.start(int(current.keep_minutes * 60_000))
            waiting, self._waiting = self._waiting, None
            if waiting is not None:
                waiting()

        self.asked = asked
        asked.task = run_task(self.tr("文章の手伝いの準備"), work, over, lambda _m: over(None))
        return True

    def ask_each(self, task: str, title: str, conversations: Sequence[Sequence[Message]],
                 done: Callable[[list[str], float], None], failed: Callable[[str], None], one_line: bool = True) -> Optional[Asked]:
        """Writes for each of `conversations` in turn (英訳 of several 備考); the stage says how far it is."""
        return self._start(task, title, list(conversations), done, failed, one_line, True)

    def _start(self, task: str, title: str, conversations: list, done: Callable[[list[str], float], None],
               failed: Callable[[str], None], one_line: bool, counted: bool,
               until: Optional[Callable[[str], bool]] = None, keep: bool = False,
               asked: Optional[Asked] = None, max_tokens: Optional[int] = None) -> Optional[Asked]:
        s = self.settings
        t = s.task(task) if s is not None else None
        model = s.model_for(task) if s is not None else None
        if t is None or model is None:
            failed(self.tr("{t} のモデルが未設定（管理者に連絡: textAssist.tasks）［E703］").format(t=task))
            return None
        if not self.enabled:
            failed(self.tr("生成 AI の機能を使わない設定（「担当と個人設定…」の「個人の設定」）"))
            return None
        if self.priming and asked is None and self._waiting is None:
            # Made ready meanwhile (prime): starts once it is, on the same Asked (cancelling it before then drops it).
            waiting = Asked(stage=self.asked.stage)

            def start() -> None:
                if waiting.extra.get("cancelled"):
                    failed("取消")
                    return
                self._start(task, title, conversations, done, failed, one_line, counted, until, keep, waiting, max_tokens)

            waiting.task = _Waiting(waiting)
            self._waiting = start
            return waiting
        if self.asked is not None:
            failed(self.tr("ほかの文章を作成中（終わってから）"))
            return None
        key, base = self._key(model), self.context.source
        writer = self.writer if self._writer_key == key else None
        if writer is None and self.writer is not None:
            # Another model is loaded: let it go first (one at a time, for the memory).
            self.unload()
        writing = self.tr("作成中…") if not model.on_server else self.tr("サーバーの生成 AI が作成中…")
        loading = self.tr("モデルを読み込み中…（初回は数秒〜）")
        if asked is None:
            asked = Asked()
        asked.stage = writing if writer is not None or model.on_server else loading
        make, max_tokens = self._make, max_tokens or t.max_tokens

        def work(job):
            w = writer
            if w is None:
                w = make(model, base, job)
            started = time.monotonic()
            texts = []
            for i, messages in enumerate(conversations):
                asked.stage = f"{writing} {i + 1}/{len(conversations)}" if counted else writing

                def wrote(text: str) -> None:
                    asked.text = text

                more = {}
                if until is not None:
                    more["until"] = until
                if keep:
                    more["keep"] = True
                texts.append(w.write(messages, max_tokens, job.isCanceled, wrote, one_line, **more))
                job.check()
            return w, texts, time.monotonic() - started

        def finished(result) -> None:
            w, texts, seconds = result
            self.asked = None
            current = self.settings
            if current is not None and key in {self._key(m) for m in current.models}:
                self.writer, self._writer_key = w, key
                if current.keep_minutes > 0:
                    self._idle.start(int(current.keep_minutes * 60_000))
                else:
                    self.unload()
            done(texts, seconds)

        def fail(message: str) -> None:
            self.asked = None
            failed(message)

        self.asked = asked
        asked.task = run_task(title, work, finished, fail)
        return asked
