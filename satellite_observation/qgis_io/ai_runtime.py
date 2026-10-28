"""
ONNX Runtime for the AI tools, as the viewer's ai/runtime.ts: imported the
first time an AI tool runs, with each model read and prepared once.

ONNX Runtime is not part of QGIS. It is used when the Python of QGIS has
it (`pip install onnxruntime`); the Windows zip of the plugin carries one
for QGIS's Python 3.12 (ext/win64, put there by scripts/build_zip.py
--win64), used when QGIS has none of its own.

Models are files: a path (relative paths from the settings file's folder)
or an http(s) URL, downloaded once into the plugin's folder of the QGIS
profile (through QGIS's network, its proxy and the authentication chosen
for the address).
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import numpy as np

from ..core.ai.model_config import OnnxSignature, OnnxValue

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Where the Windows zip keeps its ONNX Runtime.
VENDORED = os.path.join(HERE, "ext", "win64")
#: The Python the carried ONNX Runtime was built for.
VENDORED_PYTHON = (3, 12)


class AiError(Exception):
    """An AI tool that cannot run, in the person's words."""


class RuntimeMissing(AiError):
    """ONNX Runtime is not there."""


def install_hint(system: Optional[str] = None) -> str:
    """How to add ONNX Runtime to QGIS's Python, on this system."""
    system = system or platform.system()
    if system == "Windows":
        return "OSGeo4W Shell で「python -m pip install onnxruntime」を実行、または Windows 版の zip（onnxruntime 同梱）を使用"
    if system == "Darwin":
        return "ターミナルで「/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install onnxruntime」を実行（QGIS-LTR.app なら置き換え）"
    return "QGIS が使う Python に「python3 -m pip install onnxruntime」で追加（ディストリビューションのパッケージがあればそれでも可）"


_ort: Any = None


def load_ort(vendored: str = VENDORED, system: Optional[str] = None) -> Any:
    """ONNX Runtime, imported once: QGIS's own, else the one carried by the Windows zip."""
    global _ort
    if _ort is not None:
        return _ort
    try:
        import onnxruntime as ort  # noqa: PLC0415 (large, and most sessions never need it)
    except ImportError:
        ort = None
    if ort is None and os.path.isdir(os.path.join(vendored, "onnxruntime")):
        if sys.version_info[:2] != VENDORED_PYTHON:
            raise RuntimeMissing(f"同梱の onnxruntime は Python {VENDORED_PYTHON[0]}.{VENDORED_PYTHON[1]} 用"
                                 f"（この QGIS は {sys.version_info[0]}.{sys.version_info[1]}）。{install_hint(system)}")
        if vendored not in sys.path:
            sys.path.insert(0, vendored)
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except ImportError as error:
            raise RuntimeMissing(f"同梱の onnxruntime を読み込めず（{error}）。{install_hint(system)}") from error
    if ort is None:
        raise RuntimeMissing(f"AI の実行に必要な onnxruntime なし。{install_hint(system)}")
    try:
        ort.set_default_logger_severity(3)
    except Exception:  # noqa: BLE001 (an old version)
        pass
    _ort = ort
    return ort


def runtime_available() -> bool:
    try:
        load_ort()
        return True
    except AiError:
        return False


# Where a model is.

def resolve_source(source: str, base: str = "") -> str:
    """A model's path or URL as written in the settings file, made whole: relative ones from the settings file's folder (or URL)."""
    source = source.strip()
    if re.match(r"^https?://", source, re.IGNORECASE):
        return source
    if source.startswith("//"):
        scheme = urlsplit(base).scheme if re.match(r"^https?://", base, re.IGNORECASE) else "https"
        return f"{scheme}:{source}"
    if re.match(r"^https?://", base, re.IGNORECASE):
        return urljoin(base, source.replace("\\", "/"))
    if os.path.isabs(source) or re.match(r"^[A-Za-z]:[\\/]", source):
        return source
    folder = os.path.dirname(base) if base else os.getcwd()
    return os.path.normpath(os.path.join(folder, source))


def is_remote(source: str) -> bool:
    return bool(re.match(r"^https?://", source, re.IGNORECASE))


def cache_path(url: str, folder: str) -> str:
    """Where a downloaded model is kept: by its address, with its file name."""
    name = re.sub(r"[^\w.-]", "_", os.path.basename(urlsplit(url).path) or "model.onnx")[-80:]
    return os.path.join(folder, f"{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}-{name}")


def model_folder() -> str:
    from . import settings

    folder = os.path.join(settings.profile_folder(), "ai_models")
    os.makedirs(folder, exist_ok=True)
    return folder


def download(url: str, target: str, authcfg: Optional[str] = None, progress: Optional[Callable[[float], None]] = None,
             cancelled: Callable[[], bool] = lambda: False, idle_seconds: float = 60) -> str:
    """
    Downloads `url` to `target` (off the main thread) through QGIS's network,
    written as it comes; it gives up when nothing arrives for `idle_seconds`.
    """
    from qgis.core import QgsApplication, QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QEventLoop, QThread, QTimer, QUrl
    from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

    from .network import on_main_thread

    if on_main_thread():
        raise RuntimeError("モデルの取得は QgsTask の中で実行")
    partial = target + ".part"
    state: dict[str, Any] = {"error": None}

    class Exchange(QThread):
        def run(self) -> None:
            try:
                fetch()
            except BaseException as error:  # noqa: BLE001 (handed to the caller's thread)
                state["error"] = error

    def fetch() -> None:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        if authcfg and not QgsApplication.authManager().updateNetworkRequest(request, authcfg):
            raise AiError(f"認証設定（{authcfg}）を使えず（{url.split('?', 1)[0]}）")
        reply = QgsNetworkAccessManager.instance().get(request)
        if authcfg:
            QgsApplication.authManager().updateNetworkReply(reply, authcfg)
        last = {"t": time.monotonic(), "timed_out": False}
        with open(partial, "wb") as out:
            def read() -> None:
                out.write(bytes(reply.readAll()))
                last["t"] = time.monotonic()

            def moved(done: int, total: int) -> None:
                if progress and total > 0:
                    progress(done / total)

            loop = QEventLoop()
            reply.readyRead.connect(read)
            reply.downloadProgress.connect(moved)
            reply.finished.connect(loop.quit)
            timer = QTimer()

            def watch() -> None:
                if reply.isFinished():
                    loop.quit()
                elif cancelled():
                    reply.abort()
                elif time.monotonic() - last["t"] > idle_seconds:
                    last["timed_out"] = True
                    reply.abort()

            timer.timeout.connect(watch)
            timer.start(100)
            if not reply.isFinished():
                loop.exec()
            timer.stop()
            read()
        try:
            if cancelled():
                raise AiError("中止")
            if last["timed_out"]:
                raise AiError(f"モデルの取得が途中で停止（{url.split('?', 1)[0]}）")
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error() != QNetworkReply.NetworkError.NoError or (isinstance(status, int) and status >= 400):
                raise AiError(f"{url.split('?', 1)[0]} の読み込みに失敗（{'HTTP ' + str(status) if isinstance(status, int) else '接続不可'}）")
        finally:
            reply.deleteLater()

    exchange = Exchange()
    exchange.start()
    while not exchange.wait(100):
        pass
    if state["error"] is not None:
        if os.path.exists(partial):
            os.remove(partial)
        raise state["error"]
    os.replace(partial, target)
    return target


def local_model(source: str, base: str = "", progress: Optional[Callable[[float], None]] = None,
                cancelled: Callable[[], bool] = lambda: False, folder: Optional[str] = None) -> str:
    """The file of a model (downloaded once when it is a URL)."""
    whole = resolve_source(source, base)
    if not is_remote(whole):
        if not os.path.isfile(whole):
            raise AiError(f"モデルのファイルなし（{whole}）")
        return whole
    from . import settings

    target = cache_path(whole, folder or model_folder())
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    return download(whole, target, settings.auth_for(whole), progress, cancelled)


# A model ready to run.

def _dims(shape) -> list:
    return [d if isinstance(d, int) and d >= 0 else (d if isinstance(d, str) else None) for d in (shape or [])]


@dataclass
class LoadedModel:
    name: str
    session: Any
    #: The model's own metadata (`metadata_props`).
    metadata: dict[str, str] = field(default_factory=dict)
    signature: OnnxSignature = field(default_factory=OnnxSignature)
    #: Where it runs, for messages.
    backend: str = "CPU"

    @property
    def input_names(self) -> list[str]:
        return [v.name for v in self.signature.inputs]

    @property
    def output_names(self) -> list[str]:
        return [v.name for v in self.signature.outputs]

    def output_types(self) -> dict[str, str]:
        return {v.name: v.type for v in self.signature.outputs}

    def run(self, feeds: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The outputs by name, in the model's order."""
        names = self.output_names
        return dict(zip(names, self.session.run(names, feeds)))


#: Providers that run on a GPU, in the order they are tried.
GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider")
_PROVIDER_LABELS = {"CUDAExecutionProvider": "GPU（CUDA）", "DmlExecutionProvider": "GPU（DirectML）",
                    "CoreMLExecutionProvider": "GPU（Core ML）", "CPUExecutionProvider": "CPU"}


def threads() -> int:
    """Threads for one session: all cores but one (QGIS keeps answering)."""
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def create_model(path: str, name: str = "", use_gpu: bool = True) -> LoadedModel:
    """A session for the model in `path`: on a GPU when ONNX Runtime has one (falling back to the CPU if the model cannot run there)."""
    ort = load_ort()
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    available = list(ort.get_available_providers())
    tried = [p for p in GPU_PROVIDERS if use_gpu and p in available] + ["CPUExecutionProvider"]
    session, error = None, None
    for provider in tried:
        try:
            session = ort.InferenceSession(path, sess_options=options, providers=[provider])
            break
        except Exception as e:  # noqa: BLE001 (some models use operators a GPU provider does not have)
            error = e
    if session is None:
        raise AiError(f"モデルを読み込めず（{os.path.basename(path)}: {str(error).splitlines()[0] if error else '不明'}）")
    meta = session.get_modelmeta()
    signature = OnnxSignature(
        [OnnxValue(v.name, v.type, _dims(v.shape)) for v in session.get_inputs()],
        [OnnxValue(v.name, v.type, _dims(v.shape)) for v in session.get_outputs()],
    )
    used = session.get_providers()[0] if session.get_providers() else "CPUExecutionProvider"
    return LoadedModel(name or os.path.basename(path), session, dict(meta.custom_metadata_map or {}), signature, _PROVIDER_LABELS.get(used, used))


_loaded: dict[tuple, LoadedModel] = {}


def model_from(path: str, name: str = "") -> LoadedModel:
    """The model in `path`, loaded once (again when the file changes)."""
    stat = os.stat(path)
    key = (os.path.abspath(path), stat.st_size, stat.st_mtime)
    if key not in _loaded:
        _loaded[key] = create_model(path, name)
    return _loaded[key]


def name_of(names: list[str], wanted: str, index: int) -> str:
    """The input or output called `wanted`, else the `index`-th."""
    return wanted if wanted in names else names[index]
