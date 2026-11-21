"""
備考の単語集 (`glossary`, core/obs/glossary.py) as the screens know it: read
in a task when the settings file is read, again when someone asks (the
分類体系 「更新」) or when the words read are older than REREAD_SECONDS and a
form wants them. The words read last are kept while they are read again; a
table that cannot be read leaves the fields as they were without it (free
input, no suggestions) and says why.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core.obs.api import NO_GLOSSARY, Glossary, load_glossary
from .tasks import run_task

#: Words older than this are read again (in the background) when a form opens.
REREAD_SECONDS = 15 * 60
#: After a failed read, the next try waits this long.
RETRY_SECONDS = 60


class GlossaryKeeper(QObject):
    """The glossary of the settings file's `glossary`, read in tasks (main thread)."""

    #: The words (or why they could not be read) changed.
    changed = pyqtSignal()

    def __init__(self, context, clock: Callable[[], float] = time.monotonic):
        super().__init__(context)
        self.context = context
        self._clock = clock
        self.glossary: Glossary = NO_GLOSSARY
        #: Why the table could not be read last ("" when it was, or none is set).
        self.problem = ""
        self.reading = False
        self._read_at: Optional[float] = None
        self._failed = False
        self._generation = 0
        context.configChanged.connect(self._config_changed)

    @property
    def configured(self) -> bool:
        return self.context.config.glossary is not None

    def _config_changed(self) -> None:
        self._generation += 1
        had = bool(self.glossary) or bool(self.problem)
        self.glossary, self.problem, self.reading, self._read_at, self._failed = NO_GLOSSARY, "", False, None, False
        if had:
            self.changed.emit()
        # Once every other part has taken the new settings (a read started in the middle of that races with it).
        generation = self._generation
        QTimer.singleShot(0, lambda: self.read() if generation == self._generation else None)

    def due(self) -> bool:
        """Whether the words should be read again (never read, old, or the last read failed a while ago)."""
        if not self.configured or self.reading:
            return False
        if self._read_at is None:
            return True
        return self._clock() - self._read_at >= (RETRY_SECONDS if self._failed else REREAD_SECONDS)

    def read(self, force: bool = False, then: Optional[Callable[[], None]] = None) -> bool:
        """Reads the table in a task (`force`: even if read a moment ago); False when nothing is read."""
        source = self.context.config.glossary
        if source is None or self.reading or not (force or self.due()):
            return False
        self.reading = True
        generation = self._generation
        context = self.context

        def work(task):
            return load_glossary(context.esri(), source, context.token_for(source.url, source.token), task.isCanceled)

        def done(glossary: Glossary) -> None:
            if generation != self._generation:
                return  # Read for the settings file before.
            self.reading, self._read_at, self._failed = False, self._clock(), False
            if glossary != self.glossary or self.problem:
                self.glossary, self.problem = glossary, ""
                self.changed.emit()
            if then is not None:
                then()

        def failed(message: str) -> None:
            if generation != self._generation:
                return
            self.reading, self._read_at, self._failed = False, self._clock(), True
            problem = f"単語集を読めず、{'前に読んだ ' + str(len(self.glossary)) + ' 語で候補を表示' if self.glossary else '候補なし'}（{message}）"
            if problem != self.problem:
                self.problem = problem
                self.changed.emit()
            if then is not None:
                then()

        run_task(self.tr("単語集の読み込み"), work, done, failed, hidden=True)
        return True
