"""V53 内部 key (hs300) ↔ QMT code (510300.SH) 双向映射"""
import pytest

from plugins.v53.code_map import V53_KEY_TO_QMT, QMT_TO_V53_KEY, ETF_KEYS


def test_ten_keys():
    assert len(ETF_KEYS) == 10
    assert set(ETF_KEYS) == {
        "hs300", "cyb", "bond", "gold", "commodity2",
        "commodity3", "crude_oil", "sp500", "nasdaq", "dividend",
    }


def test_key_to_qmt():
    assert V53_KEY_TO_QMT["hs300"] == "510300.SH"
    assert V53_KEY_TO_QMT["cyb"] == "159915.SZ"
    assert V53_KEY_TO_QMT["bond"] == "511260.SH"
    assert V53_KEY_TO_QMT["gold"] == "518880.SH"
    assert V53_KEY_TO_QMT["commodity2"] == "159981.SZ"
    assert V53_KEY_TO_QMT["commodity3"] == "159985.SZ"
    assert V53_KEY_TO_QMT["crude_oil"] == "159930.SZ"
    assert V53_KEY_TO_QMT["sp500"] == "513500.SH"
    assert V53_KEY_TO_QMT["nasdaq"] == "513100.SH"
    assert V53_KEY_TO_QMT["dividend"] == "512890.SH"


def test_qmt_to_key_inverse():
    for k, q in V53_KEY_TO_QMT.items():
        assert QMT_TO_V53_KEY[q] == k


def test_qdii_codes_marked():
    from plugins.v53.code_map import QDII_QMT_CODES
    assert QDII_QMT_CODES == {"513500.SH", "513100.SH"}
