"""
Copies an ONNX model folder, its external data split into files of at most LIMIT bytes
(model.onnx.data → model.onnx.data, model.onnx.data2, …), each tensor whole in one file.
Usage: python scripts/split_model_data.py SRC_FOLDER DST_FOLDER LIMIT_MB
(Used for models/: Git LFS takes files up to 2 GB, project files only small ones; see models/README.md.)
"""
import os
import shutil
import sys

import onnx
from onnx.external_data_helper import ExternalDataInfo

src, dst, limit = sys.argv[1], sys.argv[2], int(sys.argv[3]) * 1024 * 1024
os.makedirs(dst, exist_ok=True)
model = onnx.load(os.path.join(src, "model.onnx"), load_external_data=False)
tensors = [t for t in model.graph.initializer if t.data_location == onnx.TensorProto.EXTERNAL]
for node in model.graph.node:
    for a in node.attribute:
        if a.t is not None and a.t.data_location == onnx.TensorProto.EXTERNAL:
            tensors.append(a.t)
files, size, index = {}, 0, 1
outs = {}
for t in tensors:
    info = ExternalDataInfo(t)
    length = info.length
    name = "model.onnx.data" if index == 1 else f"model.onnx.data{index}"
    if size and size + length > limit:
        index, size = index + 1, 0
        name = f"model.onnx.data{index}"
    if name not in outs:
        outs[name] = open(os.path.join(dst, name), "wb")
    out = outs[name]
    pad = (-out.tell()) % 4096
    out.write(b"\0" * pad)
    offset = out.tell()
    with open(os.path.join(src, info.location), "rb") as f:
        f.seek(info.offset or 0)
        out.write(f.read(length))
    size = out.tell()
    del t.external_data[:]
    for k, v in (("location", name), ("offset", str(offset)), ("length", str(length))):
        e = t.external_data.add()
        e.key, e.value = k, v
for out in outs.values():
    out.close()
onnx.save_model(model, os.path.join(dst, "model.onnx"))
for f in os.listdir(src):
    if f not in ("model.onnx",) and not f.startswith("model.onnx.data"):
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
for f in sorted(os.listdir(dst)):
    print(f, os.path.getsize(os.path.join(dst, f)))
