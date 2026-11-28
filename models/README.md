# 文章の手伝いのモデル

「文章の手伝い」（設定の `textAssist`）で使う小さな生成 AI のモデル。説明の「[文章の手伝い](../docs/features/observations.md#文章の手伝い小さな生成-aitextassist)」の手順で、2026-11-20 に作って確かめたもの。

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
             "searchWords": "large", "summary": "large", "translate": "large", "requestDraft": "large" }
}
```

使う人がそれぞれ「担当と個人設定…」の「生成 AI の機能」を入れると、ボタンが出る（初めは出ない）。軽い PC では `tidyNote`・`noteFields`・`searchWords` を `small` に。速さと出来の実測は README の表のとおり。

## 作り直すとき

README の手順で変換したあと、`python scripts/split_model_data.py <変換したフォルダー> <出力フォルダー> 280` で重みを 280 MB 以下のファイルに分けてからここに置く（GitHub LFS は 1 ファイル 2 GB まで、共有ファイルも大きいファイルは送れないため）。古いモデルを消しても LFS の容量は戻らない（無料枠は 10 GB）。

## 「ミカゲと話す」のモデル（9B 級）

「ミカゲと話す」（`textAssist.tasks.chat`。2026-11-28 までの「データに質問」の `dataChat` と「文で操作」の `command` をまとめた）は表の質問を SQL にするので、上の 3B では足りない（検討の 30 問で 3B は 2 問、Qwen3.5-9B は 24〜26 問が正しい SQL。操作の頼みは 32 文のうち 31 文。[ミカゲと話す](../docs/features/analysis.md#ミカゲと話すchat)）。9B は 5.7 GB と大きく、LFS の枠に入らないのでここには置かない。どちらかで用意する:

- **PC で動かす**: [Qwen/Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B)（Apache 2.0）を上の手順と同じく ONNX Runtime GenAI（0.20 以降。builder が `Qwen3_5` を読める）で int4 に変換し、共有フォルダーに置いて `"qwen9b": "\\\\fileserver\\gis\\models\\qwen3.5-9b-int4"`。メモリ 8 GB 程度、初めの 1 問は 1〜2 分、あとは 1 問 10〜20 秒
- **社内の GPU サーバー**: vLLM・llama.cpp の server など OpenAI 互換のサーバーで Qwen3.8-27B などを動かし、`"qwen9b": { "url": "https://llm.example.local/v1", "model": "qwen3.8-27b", "timeoutSeconds": 300 }`（QGIS の認証設定とプロキシを使う）

```jsonc
"textAssist": {
  "models": { "large": "\\\\fileserver\\gis\\models\\sarashina-3b-int4",
              "qwen9b": "\\\\fileserver\\gis\\models\\qwen3.5-9b-int4" },
  "tasks": { "requestDraft": "large", "chat": "qwen9b" }
}
```

サーバーができたら `models.qwen9b` を URL に変えるだけで切り替わる。
