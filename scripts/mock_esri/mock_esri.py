"""
A stand-in for ArcGIS feature services, for working on the plugin (and the
viewer) without an Esri server. Python's standard library only, the data in
SQLite (kept between runs). The server itself is the plugin's 練習モード
server (mikage/core/training/server.py, moved there 2026-11-22); this is its
command line, as before.

    python3 scripts/mock_esri/mock_esri.py                  # http://127.0.0.1:8765, sample data on first run
    python3 scripts/mock_esri/mock_esri.py --port 9000 --token secret
    python3 scripts/mock_esri/mock_esri.py --reset          # back to the sample data
    python3 scripts/mock_esri/mock_esri.py new-image        # registers a new catalog image now (新着通知の確認)

Each start writes <data>/config.mock.jsonc: a settings file whose services
point at this server. Choose it in the plugin's settings dialog (設定…).
What it answers: see the server module and README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import ThreadingHTTPServer
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DATA = os.path.join(HERE, "data")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from mikage.core.training import sample  # noqa: E402
from mikage.core.training.server import *  # noqa: E402,F401,F403 (Store, Service, make_handler… as before)
from mikage.core.training.server import Service, Store, load_services, make_handler, open_data  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ArcGIS フィーチャーサービスのモック（Esri なしでの開発用）")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "new-image", "config"],
                        help="serve: 起動（既定） / new-image: 画像カタログに新着画像を 1 件登録 / config: 設定ファイルだけ書き出し")
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス（既定 127.0.0.1。ほかの PC から使うなら 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=DEFAULT_DATA, help=f"データのフォルダ（既定 {DEFAULT_DATA}）")
    parser.add_argument("--reset", action="store_true", help="データを消してサンプルから作り直す")
    parser.add_argument("--empty", action="store_true", help="サンプルの地物を入れない（レイヤーの定義だけ）")
    parser.add_argument("--services", action="append", default=[], help="レイヤーの定義（JSON）を足す。複数可")
    parser.add_argument("--token", help="このトークン（か generateToken で発行したもの）がない要求を断る")
    parser.add_argument("--public-url", help="設定ファイルに書く URL の先頭（既定 http://<host>:<port>）")
    parser.add_argument("--quiet", action="store_true", help="要求を 1 行ずつ表示しない")
    a = parser.parse_args(argv)

    files = os.path.join(a.data, "files")
    host = "127.0.0.1" if a.host in ("0.0.0.0", "", "::") else a.host
    base = (a.public_url or f"http://{host}:{a.port}").rstrip("/")
    store = open_data(a.data, base, reset=a.reset, empty=a.empty)
    for path in a.services:
        with open(path, encoding="utf-8-sig") as f:
            n = load_services(store, json.load(f))
        print(f"定義を読み込み: {path}（{n} 件）")

    if a.command == "new-image":
        image_id = sample.add_new_image(store, files, base)
        print(f"新着画像を登録: {image_id}")
        return 0
    config_path = os.path.join(a.data, "config.mock.jsonc")
    sample.write_config(config_path, base, files)
    print(f"設定ファイル: {config_path}")
    if a.command == "config":
        return 0

    service = Service(store, a.token)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(service, files, a.quiet))
    print(f"起動: {base}/arcgis/rest/services （Ctrl+C で終了）")
    for name in store.services():
        print(f"  {base}/arcgis/rest/services/{name}/FeatureServer")
    if a.token:
        print(f"  トークン: {a.token}（「設定…」の表で、このサーバーに認証設定 ESRI-Token（このトークン）を選ぶ）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["Service", "Store", "load_services", "main", "make_handler", "open_data", "sample"]

if __name__ == "__main__":
    sys.exit(main())
