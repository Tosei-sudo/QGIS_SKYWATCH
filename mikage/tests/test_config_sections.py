"""
The settings file's sections (core/config.py SECTIONS, docs/architecture.md 設定セクション): each
key once, each filling one PluginConfig attribute, every attribute filled by one (but DERIVED,
made from another's).
"""
import dataclasses

from mikage.core import config


def test_each_key_fills_one_attribute():
    keys = [s.key for s in config.SECTIONS]
    attrs = [s.attr for s in config.SECTIONS]
    assert len(set(keys)) == len(keys) and len(set(attrs)) == len(attrs)
    fields = {f.name for f in dataclasses.fields(config.PluginConfig)}
    assert set(attrs) | set(config.DERIVED) == fields and not set(attrs) & set(config.DERIVED)
    assert config.KEYS == tuple(keys)


def test_a_section_read_badly_says_why():
    for s in config.SECTIONS:
        if not s.whole:
            assert config.parse_config({s.key: object()})[1], s.key
