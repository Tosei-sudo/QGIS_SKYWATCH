"""
撮像計画ドック (ui/imaging_plan_dock.py) の担当ごとの部分 (docs/architecture.md): each module one part
of the dock's methods (衛星, 目標, 結果の一覧, 雲量, ペア・再訪, 通知と予定表, 書き出し), and common.py what
they share. ImagingPlanDock is made of them: the widgets, the state and the behaviour are the dock's as before.
"""
