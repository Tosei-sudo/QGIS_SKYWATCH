"""
観測記録ドック (ui/observation_dock.py) の担当ごとの部分 (docs/architecture.md 段階7): each module one part
of the dock's methods (通報, 画像, 記録のしかた, 地図での操作, 入力欄, 記録の操作, 書き出しと連携, 一覧,
送信待ち, 表示, 地図の印), and common.py what they share. ObservationDock is made of them: the widgets,
the state and the behaviour are the dock's as before.
"""
