"""
A folder shown in the computer's file manager (画像カタログの「フォルダを開く」):
Explorer on Windows (an image file in it selected), Finder on macOS, the
desktop's own file manager elsewhere.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices


def show_folder(folder: str, file: Optional[str] = None) -> bool:
    """
    Opens `folder` in the file manager, with `file` (one in it) selected where
    the file manager can (Explorer, Finder). Whether it could be asked (the
    window opens by itself, nothing waits for it).
    """
    try:
        if sys.platform == "win32":
            if file:
                # Explorer reads `/select,"path"` as one argument (the list form would quote it apart).
                subprocess.Popen(f'explorer /select,"{os.path.normpath(file)}"')
            else:
                subprocess.Popen(["explorer", os.path.normpath(folder)])
            return True
        if sys.platform == "darwin" and file:
            subprocess.Popen(["open", "-R", file])
            return True
    except OSError:
        pass
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
