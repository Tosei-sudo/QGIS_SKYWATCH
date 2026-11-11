"""
定期照合（QGIS なし）: 出力履歴を画像カタログと照合し、撮像済み・未撮像と台帳
（繰り上げの戻し・期間内1回の完了）を更新する。Windows のタスクスケジューラから
定期的に実行する（README「定期照合（タスクスケジューラ）」）。

    "C:\\Program Files\\QGIS 3.34.12\\bin\\python-qgis-ltr.bat" ^
        "%APPDATA%\\QGIS\\QGIS3\\profiles\\default\\python\\plugins\\skywatch\\scheduled_follow.py" ^
        --config "\\\\fileserver\\gis\\skywatch\\config.json"

QGIS は起動しない（QGIS の Python を使うだけ。NumPy のある Python なら何でもよい）。
中身は skywatch/core/request/follow_job.py。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# The plugin's folder's parent, so `skywatch` imports as a package (and not the plugin's folder itself:
# its sub-folders are not top-level modules).
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]
sys.path.insert(0, os.path.dirname(_HERE))

from skywatch.core.request.follow_job import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
