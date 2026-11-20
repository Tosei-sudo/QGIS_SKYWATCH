sarashina-1b-int4

sbintuitions/sarashina2.2-1b-instruct-v0.1（MIT License、SB Intuitions）を ONNX Runtime GenAI 0.20 の builder で
int4（CPU）に変換したもの（2026-11-20）。ライセンスは LICENSE（元のモデルのもの）。
  python -m onnxruntime_genai.models.builder -i <ダウンロードしたフォルダー> -o sarashina-1b-int4 -p int4 -e cpu \
      --extra_options op_types_to_quantize=MatMul/Gather prune_lm_head=true

共有ファイルや GitHub（Git LFS）で渡すため、重み（model.onnx.data）を 280 MB 以下の 4 ファイル（model.onnx.data, model.onnx.data2 …）に
分けてある（中身は同じ。ONNX の外部データの置き場所を分けただけで、model.onnx がそれぞれを指している）。

使い方: このフォルダーのファイルを全部（11 ファイル、計 852M）同じフォルダーに置き、
共有フォルダーか社内の Web サーバー（zip にして）に置いて、設定ファイルの textAssist.models に書く。
  "textAssist": { "models": { "1b": "\\\\server\\share\\models\\sarashina-1b-int4" }, "tasks": { ... } }
共有フォルダーのものは初回に QGIS のプロファイルの text_models にコピーして使う（2 回目からはそのまま）。
