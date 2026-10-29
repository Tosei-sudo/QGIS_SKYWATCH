"""
Trains a detector on synthetic scenes and measures it on others (mAP50,
mAP50-95), to compare the networks of core/ai/train:

    PYTHONPATH=. python3 scripts/ai_bench/bench.py --arch fpn4 --epochs 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from scenes import CLASSES, dataset  # noqa: E402

from satellite_observation.core.ai.train.metrics import evaluate  # noqa: E402
from satellite_observation.core.ai.train.trainer import InputSpec, TrainOptions, detect_window, new_model, train  # noqa: E402


def measure(model, windows):
    found = []
    for w in windows:
        ds = detect_window(model, w, score=0.05)
        found.append(np.array([[*d.box, d.cls, d.score] for d in ds], float).reshape(-1, 6))
    return evaluate(found, [w.boxes for w in windows], model.classes)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", default="fpn4")
    parser.add_argument("--size", default="small")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--tile", type=int, default=256)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--train", type=int, default=32)
    parser.add_argument("--test", type=int, default=12)
    parser.add_argument("--lr", type=float, default=0)
    args = parser.parse_args()
    train_set, test_set = dataset(1, args.train), dataset(2, args.test)
    model = new_model(CLASSES, InputSpec(), args.tile, args.size, arch=args.arch)
    options = TrainOptions(epochs=args.epochs, tile=args.tile, batch=args.batch)
    if args.lr:
        options.lr = args.lr
    started = time.time()
    run = train(model, train_set, [], options, log=lambda t: print(t, flush=True))
    seconds = time.time() - started
    s = measure(model, test_set)
    print(json.dumps({"arch": args.arch, "size": args.size, "epochs": args.epochs, "steps": run.steps, "seconds": round(seconds),
                      "per_step": round(seconds / run.steps, 3), **s.to_json()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
