"""
A tiny ONNX Runtime GenAI model for the tests of 文章の手伝い
(core/text_assist.py, qgis_io/text_assist_io.py): no real language model,
but the files and the inputs and outputs of one, so the real runtime loads
it, applies its chat template, tokenizes, keeps its cache and writes.

The tokenizer is byte-level BPE with only the 256 bytes (any text, Japanese
too, is its UTF-8 bytes) and four special tokens. The "model" writes
`reply` byte by byte after the assistant's turn starts (the next token
depends only on the token before), then ends; `heavy` adds that many large
matrix products per step, to see what happens while it is slow.
"""
from __future__ import annotations

import json
import os

import numpy as np

from mikage.core.ai.train.onnx_io import _attr_int, _bytes, _int, _str

FLOAT, INT64 = 1, 7
EOS, USER, ASSISTANT, SYSTEM = 256, 257, 258, 259
VOCAB = 260
HEAD = 4
CHAT_TEMPLATE = ("{% for message in messages %}<|{{ message['role'] }}|>{{ message['content'] }}<|endoftext|>{% endfor %}"
                 "{% if add_generation_prompt %}<|assistant|>{% endif %}")


def _bytes_to_unicode() -> dict[int, str]:
    """GPT-2's byte → character table (byte-level BPE)."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


def _tensor(name: str, a: np.ndarray) -> bytes:
    a = np.ascontiguousarray(a)
    kind = INT64 if a.dtype == np.int64 else FLOAT
    a = a.astype("<i8" if kind == INT64 else "<f4", copy=False)
    return b"".join(_int(1, d) for d in a.shape) + _int(2, kind) + _str(8, name) + _bytes(9, a.tobytes())


def _node(op: str, inputs, outputs, attrs=()) -> bytes:
    return (b"".join(_str(1, i) for i in inputs) + b"".join(_str(2, o) for o in outputs) + _str(3, outputs[0])
            + _str(4, op) + b"".join(_bytes(5, a) for a in attrs))


def _value(name: str, kind: int, dims) -> bytes:
    shape = b"".join(_bytes(1, _int(1, d) if isinstance(d, int) else _str(2, d)) for d in dims)
    return _str(1, name) + _bytes(2, _bytes(1, _int(1, kind) + _bytes(2, shape)))


def model_bytes(reply: str, heavy: int = 0) -> bytes:
    data = reply.encode("utf-8")
    table = np.full((VOCAB, VOCAB), -10.0, np.float32)
    previous = ASSISTANT
    for b in data:
        if table[previous].max() > 0:
            raise ValueError("reply: a byte followed by two different bytes (the next token depends on the last one only)")
        table[previous, b] = 10.0
        previous = b
    table[previous, EOS] = 10.0
    for t in range(VOCAB):
        if table[t].max() <= 0:
            table[t, EOS] = 10.0
    keys = np.random.default_rng(0).standard_normal((VOCAB, HEAD)).astype(np.float32)
    inits = [_tensor("table", table), _tensor("keys", keys), _tensor("axis1", np.array([1], np.int64))]
    nodes = [
        _node("Gather", ["table", "input_ids"], ["base"]),
        _node("Gather", ["keys", "input_ids"], ["k"]),
        _node("Unsqueeze", ["k", "axis1"], ["k4"]),
        _node("Concat", ["past_key_values.0.key", "k4"], ["present.0.key"], [_attr_int("axis", 2)]),
        _node("Concat", ["past_key_values.0.value", "k4"], ["present.0.value"], [_attr_int("axis", 2)]),
    ]
    if heavy:
        size = 1024
        rng = np.random.default_rng(1)
        inits += [_tensor("wide", rng.standard_normal((VOCAB, size)).astype(np.float32) * 0.01),
                  _tensor("zero", np.zeros((), np.float32))]
        nodes.append(_node("Gather", ["wide", "input_ids"], ["h0"]))
        for i in range(heavy):
            inits.append(_tensor(f"w{i}", rng.standard_normal((size, size)).astype(np.float32) * 0.03))
            nodes.append(_node("MatMul", [f"h{i}", f"w{i}"], [f"h{i + 1}"]))
        nodes += [_node("ReduceSum", [f"h{heavy}"], ["s"], [_attr_int("keepdims", 1)]),
                  _node("Mul", ["s", "zero"], ["nothing"]),
                  _node("Add", ["base", "nothing"], ["logits"])]
    else:
        nodes.append(_node("Identity", ["base"], ["logits"]))
    graph = (b"".join(_bytes(1, n) for n in nodes) + _str(2, "tiny") + b"".join(_bytes(5, t) for t in inits)
             + _bytes(11, _value("input_ids", INT64, ["batch", "sequence"]))
             + _bytes(11, _value("attention_mask", INT64, ["batch", "total"]))
             + _bytes(11, _value("past_key_values.0.key", FLOAT, ["batch", 1, "past", HEAD]))
             + _bytes(11, _value("past_key_values.0.value", FLOAT, ["batch", 1, "past", HEAD]))
             + _bytes(12, _value("logits", FLOAT, ["batch", "sequence", VOCAB]))
             + _bytes(12, _value("present.0.key", FLOAT, ["batch", 1, "total", HEAD]))
             + _bytes(12, _value("present.0.value", FLOAT, ["batch", 1, "total", HEAD])))
    return _int(1, 8) + _str(2, "mikage-tests") + _bytes(7, graph) + _bytes(8, _str(1, "") + _int(2, 13))


def write_model(folder: str, reply: str = "Tidy note.", heavy: int = 0, chat_template: bool = True) -> str:
    """Writes the model's files into `folder` (made when missing); the folder."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "model.onnx"), "wb") as f:
        f.write(model_bytes(reply, heavy))
    config = {
        "model": {
            "bos_token_id": EOS, "eos_token_id": EOS, "pad_token_id": EOS, "type": "llama", "vocab_size": VOCAB, "context_length": 4096,
            "decoder": {
                "filename": "model.onnx", "head_size": HEAD, "hidden_size": HEAD, "num_attention_heads": 1, "num_hidden_layers": 1,
                "num_key_value_heads": 1, "session_options": {"log_id": "onnxruntime-genai", "provider_options": []},
                "inputs": {"input_ids": "input_ids", "attention_mask": "attention_mask",
                           "past_key_names": "past_key_values.%d.key", "past_value_names": "past_key_values.%d.value"},
                "outputs": {"logits": "logits", "present_key_names": "present.%d.key", "present_value_names": "present.%d.value"},
            },
        },
        "search": {"do_sample": False, "max_length": 4096, "min_length": 0, "num_beams": 1, "num_return_sequences": 1,
                   "past_present_share_buffer": False, "repetition_penalty": 1.0, "temperature": 1.0, "top_k": 1, "top_p": 1.0},
    }
    with open(os.path.join(folder, "genai_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=1)
    chars = _bytes_to_unicode()
    special = [(EOS, "<|endoftext|>"), (USER, "<|user|>"), (ASSISTANT, "<|assistant|>"), (SYSTEM, "<|system|>")]
    tokenizer = {
        "version": "1.0", "truncation": None, "padding": None,
        "added_tokens": [{"id": i, "content": t, "single_word": False, "lstrip": False, "rstrip": False, "normalized": False, "special": True}
                         for i, t in special],
        "normalizer": None,
        "pre_tokenizer": {"type": "ByteLevel", "add_prefix_space": False, "trim_offsets": True, "use_regex": True},
        "post_processor": None,
        "decoder": {"type": "ByteLevel", "add_prefix_space": True, "trim_offsets": True, "use_regex": True},
        "model": {"type": "BPE", "dropout": None, "unk_token": None, "continuing_subword_prefix": None, "end_of_word_suffix": None,
                  "fuse_unk": False, "byte_fallback": False, "ignore_merges": False,
                  "vocab": {chars[b]: b for b in range(256)} | {t: i for i, t in special}, "merges": []},
    }
    with open(os.path.join(folder, "tokenizer.json"), "w", encoding="utf-8") as f:
        json.dump(tokenizer, f, ensure_ascii=False)
    tokenizer_config = {"tokenizer_class": "GPT2Tokenizer", "bos_token": "<|endoftext|>", "eos_token": "<|endoftext|>",
                        "unk_token": "<|endoftext|>", "add_bos_token": False, "clean_up_tokenization_spaces": False,
                        "model_max_length": 4096}
    if chat_template:
        tokenizer_config["chat_template"] = CHAT_TEMPLATE
    with open(os.path.join(folder, "tokenizer_config.json"), "w", encoding="utf-8") as f:
        json.dump(tokenizer_config, f, ensure_ascii=False, indent=1)
    with open(os.path.join(folder, "special_tokens_map.json"), "w", encoding="utf-8") as f:
        json.dump({"bos_token": "<|endoftext|>", "eos_token": "<|endoftext|>", "unk_token": "<|endoftext|>"}, f)
    return folder

