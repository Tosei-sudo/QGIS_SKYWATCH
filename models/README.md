# 文章の手伝いのモデル

「文章の手伝い」（設定の `textAssist`）で使う小さな生成 AI のモデル。README の「[文章の手伝い](../README.md#文章の手伝い小さな生成-aitextassist)」の手順で、2026-11-20 に作って確かめたもの。

| フォルダー | 元のモデル | 大きさ |
|---|---|---|
| `sarashina-1b-int4/` | [sbintuitions/sarashina2.2-1b-instruct-v0.1](https://huggingface.co/sbintuitions/sarashina2.2-1b-instruct-v0.1)（MIT） | 0.85 GB（11 ファイル） |
| `sarashina-3b-int4/` | [sbintuitions/sarashina2.2-3b-instruct-v0.1](https://huggingface.co/sbintuitions/sarashina2.2-3b-instruct-v0.1)（MIT） | 2.0 GB（15 ファイル） |

重み（`model.onnx.data`, `model.onnx.data2` …）と `tokenizer.json` は Git LFS に置いてある。普通に clone しただけでは落ちてこない（`.lfsconfig` の `fetchexclude`。中身の代わりに 130 バイトほどの目印のファイルになる）。プラグインの zip にも入らない。

## 取り方

[Git LFS](https://git-lfs.com/) を入れて（Git for Windows には入っている）、clone したフォルダーで:

```
git lfs install
git lfs pull -I "models/sarashina-3b-int4/**" -X ""
```

- 1B だけなら `models/sarashina-1b-int4/**`、両方なら `-I` を省いて `git lfs pull -X ""`
- `-X ""` は `.lfsconfig` の「models を落とさない」を外すためのもの。`-I` だけでは何も落ちてこない
- GitHub の無料枠では LFS のダウンロードはリポジトリ全体で月 10 GB まで（3B で 2.0 GB、両方で 2.9 GB 使う）。一度取ったら社内の共有フォルダーに置いて、そこから配る
- GitHub の画面で 1 ファイルずつ「Download raw file」しても取れる（同じく枠を使う）

## 使い方

取ったフォルダーをそのまま（ファイルを全部、同じフォルダーに）共有フォルダーか社内の Web サーバー（zip にして）に置き、設定ファイルに書く:

```jsonc
"textAssist": {
  "models": {
    "small": "\\\\fileserver\\gis\\models\\sarashina-1b-int4",
    "large": { "path": "\\\\fileserver\\gis\\models\\sarashina-3b-int4", "threads": 4 }
  },
  "tasks": { "tidyNote": "large", "handoverDraft": "large", "noteFields": "large",
             "searchWords": "large", "summary": "large", "translate": "large" }
}
```

軽い PC では `tidyNote`・`noteFields`・`searchWords` を `small` に。速さと出来の実測は README の表のとおり。

## 作り直すとき

README の手順で変換したあと、`python scripts/split_model_data.py <変換したフォルダー> <出力フォルダー> 280` で重みを 280 MB 以下のファイルに分けてからここに置く（GitHub LFS は 1 ファイル 2 GB まで、共有ファイルも大きいファイルは送れないため）。古いモデルを消しても LFS の容量は戻らない（無料枠は 10 GB）。
