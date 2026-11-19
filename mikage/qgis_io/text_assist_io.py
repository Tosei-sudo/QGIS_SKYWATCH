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

The model stays loaded `keepMinutes` after its last use and is then let go
(its memory given back); the next use loads it again (a few seconds).
Generation releases the GIL, so QGIS keeps answering while it writes.
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

from ..core.text_assist import Message, TextAssistSettings, plain_prompt
from ..core.win64 import add_package
from .ai_runtime import (VENDORED, VENDORED_PYTHON, AiError, RuntimeMissing, cache_path, download, is_remote, load_ort,
                         resolve_source, threads as default_threads)
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
    try:
        import onnxruntime_genai as og  # noqa: PLC0415 (large, and most sessions never need it)
    except ImportError:
        og = None
    if og is None and os.path.isdir(os.path.join(vendored, "onnxruntime_genai")):
        if sys.version_info[:2] != VENDORED_PYTHON:
            raise RuntimeMissing(f"同梱の onnxruntime-genai は Python {VENDORED_PYTHON[0]}.{VENDORED_PYTHON[1]} 用"
                                 f"（この QGIS は {sys.version_info[0]}.{sys.version_info[1]}）。{install_hint(system)}")
        add_package("onnxruntime_genai", vendored, first=True)
        try:
            import onnxruntime_genai as og  # noqa: PLC0415
        except ImportError as error:
            raise RuntimeMissing(f"同梱の onnxruntime-genai を読み込めず（{error}）。{install_hint(system)}") from error
    if og is None:
        raise RuntimeMissing(f"文章の手伝いに必要な onnxruntime-genai なし。{install_hint(system)}")
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
                    raise AiError(f"モデルの zip に不正なパス（{info.filename}）")
            z.extractall(work)

    try:
        target = _made(root, name, fill)
    except zipfile.BadZipFile as error:
        raise AiError(f"モデルの zip を開けず（{os.path.basename(zip_path)}: {error}）") from error
    _forget_older(root, tag, target)
    found = config_folder(target)
    if found is None:
        raise AiError(f"モデルの zip に {CONFIG_FILE} なし（{os.path.basename(zip_path)}）")
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
            raise AiError(f"モデルのフォルダに {CONFIG_FILE} なし（{whole}）")
        return copied(folder, root or models_folder()) if is_network_path(folder) else folder
    raise AiError(f"モデルなし（{whole}）")


def urlsplit_path(url: str) -> str:
    return urlsplit(url).path


# ---- the model ------------------------------------------------------------------------------------

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
            raise AiError(f"モデルを読み込めず（{os.path.basename(folder)}: {str(error).splitlines()[0] if str(error) else error}）") from error
        self.context_length = 4096
        try:
            with open(os.path.join(folder, CONFIG_FILE), encoding="utf-8") as f:
                self.context_length = int(json.load(f)["model"].get("context_length") or 4096)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._lock = threading.Lock()
        self._og = og

    def prompt(self, messages: Sequence[Message]) -> str:
        """The conversation in the model's own chat template (a model whose template has no system turn: the instructions in the first)."""
        def apply(ms) -> str:
            return self.tokenizer.apply_chat_template(messages=json.dumps(list(ms), ensure_ascii=False), add_generation_prompt=True)

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
              wrote: Callable[[str], None] = lambda _t: None, one_line: bool = False) -> str:
        """The reply (greedy: the same every time); `one_line`: ends at the first line break after some text."""
        og = self._og
        with self._lock:
            ids = self.tokenizer.encode(self.prompt(messages))
            if len(ids) + max_tokens > self.context_length:
                raise AiError(f"入力が長すぎる（{len(ids)} トークン、モデルは {self.context_length} まで）")
            params = og.GeneratorParams(self.model)
            params.set_search_options(max_length=len(ids) + max_tokens, do_sample=False)
            generator = og.Generator(self.model, params)
            generator.append_tokens(ids)
            stream = self.tokenizer.create_stream()
            text = ""
            while not generator.is_done():
                if cancelled():
                    raise Cancelled()
                generator.generate_next_token()
                text += stream.decode(int(generator.get_next_tokens()[0]))
                wrote(text)
                if one_line and "\n" in text.lstrip():
                    break
            return text


# ---- for the screens ----------------------------------------------------------------------------------

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
    """The settings' `textAssist` for the screens (on the context): one model, one request at a time (main thread)."""

    #: The settings changed (whether the buttons show).
    changed = pyqtSignal()

    def __init__(self, context, writer_of: Optional[Callable[[str, int], Any]] = None,
                 folder_of: Optional[Callable[..., str]] = None):
        super().__init__(context)
        self.context = context
        #: How a model is loaded and found (tests give their own).
        self._writer_of = writer_of or GenAiWriter
        self._folder_of = folder_of or model_folder_of
        self.writer: Any = None
        self._writer_key: Optional[tuple] = None
        self.asked: Optional[Asked] = None
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self.unload)
        context.configChanged.connect(self._config_changed)

    @property
    def settings(self) -> Optional[TextAssistSettings]:
        return getattr(self.context.config, "text_assist", None)

    @property
    def available(self) -> bool:
        return self.settings is not None

    @property
    def busy(self) -> bool:
        return self.asked is not None

    def _key(self, s: TextAssistSettings) -> tuple:
        return (s.model, self.context.source, s.threads)

    def _config_changed(self) -> None:
        s = self.settings
        if self.writer is not None and (s is None or self._key(s) != self._writer_key):
            self.unload()
        self.changed.emit()

    def unload(self) -> None:
        """Lets the model go (its memory is given back once nothing writes with it)."""
        self._idle.stop()
        self.writer, self._writer_key = None, None

    def ask(self, title: str, messages: Sequence[Message], max_tokens: int, done: Callable[[str, float], None],
            failed: Callable[[str], None], one_line: bool = False) -> Optional[Asked]:
        """Starts writing (in a task); None (and `failed` told) when it cannot start."""
        s = self.settings
        if s is None:
            failed(self.tr("設定ファイルに textAssist（文章の手伝いのモデル）なし"))
            return None
        if self.asked is not None:
            failed(self.tr("ほかの文章を作成中（終わってから）"))
            return None
        key, base = self._key(s), self.context.source
        writer = self.writer if self._writer_key == key else None
        asked = Asked(stage=self.tr("作成中…") if writer is not None else self.tr("モデルを読み込み中…（初回は数秒〜）"))
        writer_of, folder_of = self._writer_of, self._folder_of

        def work(task):
            w = writer
            if w is None:
                folder = folder_of(s.model, base, cancelled=task.isCanceled)
                task.check()
                w = writer_of(folder, s.threads)
                asked.stage = "作成中…"
            started = time.monotonic()

            def wrote(text: str) -> None:
                asked.text = text

            text = w.write(messages, max_tokens, task.isCanceled, wrote, one_line)
            return w, text, time.monotonic() - started

        def finished(result) -> None:
            w, text, seconds = result
            self.asked = None
            current = self.settings
            if current is not None and self._key(current) == key:
                self.writer, self._writer_key = w, key
                if current.keep_minutes > 0:
                    self._idle.start(int(current.keep_minutes * 60_000))
                else:
                    self.unload()
            done(text, seconds)

        def fail(message: str) -> None:
            self.asked = None
            failed(message)

        self.asked = asked
        asked.task = run_task(title, work, finished, fail)
        return asked
