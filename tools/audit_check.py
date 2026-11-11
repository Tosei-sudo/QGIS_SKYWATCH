"""
監査ログの送り先のフォルダを点検する: セッションごとに行の番号が欠けていないか、
各行の prev が 1 つ前の行の SHA-256 と合うか（行の削除・書き換えの検知）。

    python tools/audit_check.py \\\\fileserver\\audit\\skywatch

問題がなければ終了コード 0、あれば 1。中身は skywatch/core/audit.py の verify
（プラグインのフォルダの 1 つ上で python -m skywatch.core.audit <フォルダ> も同じ）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from skywatch.core.audit import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
