#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端能力表（`get_common_config`）解析门禁。

核心纪律：**认不出的字段一律当"读不到"，绝不猜一个值。**
猜出来的值会被当真事实用 —— 那正是"用经验值替代可读的服务端数据"的翻版。
"""
from __future__ import annotations

from app.upstream.jimeng.capabilities import (
    ModelConfigCache,
    parse_common_config,
)

#: 真实形状的近似样本（字段名取自 `probe_model_config.py` 的实测输出）。
SAMPLE = {
    "ret": "0",
    "data": {
        "model_list": [
            {
                "model_req_key": "high_aes_general_v50",
                "feats": {"generate": True, "byte_edit": True, "pro_hd": True},
                "generate_count_options": [1, 2, 3, 4, 5, 6, 7, 8],
                "default_generate_count": 4,
                "resolution_map": {"1:1": {"1k": [1024, 1024], "2k": [2048, 2048]}},
                "input_image_limit": 1,
            },
            {
                "model_req_key": "high_aes_general_v50p_large",
                "generate_count_options": [1, 2, 3, 4],
                "default_generate_count": 2,
            },
        ]
    },
}


def test_parses_model_list_with_real_field_names():
    specs, notes = parse_common_config(SAMPLE)
    assert notes == []
    assert set(specs) == {"high_aes_general_v50", "high_aes_general_v50p_large"}

    v50 = specs["high_aes_general_v50"]
    assert v50.count_options == (1, 2, 3, 4, 5, 6, 7, 8)
    assert v50.default_count == 4
    assert v50.input_image_limit == 1
    assert v50.resolution_map == {"1:1": {"1k": [1024, 1024], "2k": [2048, 2048]}}
    assert v50.feats == ("byte_edit", "generate", "pro_hd")

    # 🔴 这一条是那张表的**全部价值**：默认模型是 1..8，不是"经验值 1..4"
    assert max(v50.count_options) == 8
    assert max(specs["high_aes_general_v50p_large"].count_options) == 4


def test_string_numbers_are_normalised_but_not_invented():
    """上游把数字写成字符串是常态；要能读，但**读不出就不给值**。"""
    specs, _ = parse_common_config({
        "data": {"model_list": [
            {"model_req_key": "m1", "generate_count_options": ["1", "2"]},
            {"model_req_key": "m2", "generate_count_options": "1,2,3"},
        ]}})
    assert specs["m1"].count_options == (1, 2)
    assert specs["m2"].count_options is None, "逗号串不是合法形状 ⇒ 当读不到"


def test_unknown_shape_degrades_loudly_instead_of_guessing():
    specs, notes = parse_common_config({"ret": "0", "data": {"something_else": 1}})
    assert specs == {}
    assert notes and "找不到模型列表" in notes[0]


def test_non_dict_data_is_reported_not_guessed():
    specs, notes = parse_common_config({"data": "oops"})
    assert specs == {}
    assert "没有 dict 形态的 data" in notes[0]


def test_entries_without_a_model_key_are_skipped_and_counted():
    specs, notes = parse_common_config({"data": {"model_list": [
        {"foo": "bar"}, {"model_req_key": "ok", "generate_count_options": [1]}]}})
    assert set(specs) == {"ok"}
    assert any("无法解析出模型 key" in n for n in notes)


def test_boolean_is_not_accepted_as_a_number():
    """`True` 是 1 —— 但它不是"张数选项 1"，是形状不对。"""
    specs, _ = parse_common_config({"data": {"model_list": [
        {"model_req_key": "m", "generate_count_options": [True, 2]}]}})
    assert specs["m"].count_options == (2,)


# ---------------------------------------------------------------------------
# input_image_limit：标量与**数组**两形态（2026-10-10 放开 i2i 垫图上限的依据）
# ---------------------------------------------------------------------------

#: 实测数组形态（5.0 Pro）：按 ability 分，`byte_edit` 就是 blend / 图生图。
ARRAY_SAMPLE = {
    "data": {"model_list": [
        {"model_req_key": "high_aes_general_v50p",
         "input_image_limit": [
             {"max_image_num": 10, "ability_name": "byte_edit"}]},
    ]}
}


def test_input_image_limit_array_form_is_parsed_per_ability():
    """🔴 此前 `_as_int()` 读数组恒得 None ⇒ 声明了却读不出，i2i 只能钉 4。
    数组形态必须按 ability 解析出来。"""
    specs, _ = parse_common_config(ARRAY_SAMPLE)
    spec = specs["high_aes_general_v50p"]
    assert spec.input_image_limit is None, "数组形态不该再产出标量"
    assert spec.input_image_limits == {"byte_edit": 10}


def test_input_image_limit_scalar_form_still_works():
    """标量形态（老样本 SAMPLE 里 v50 的 `input_image_limit: 1`）保持原语义。"""
    specs, _ = parse_common_config(SAMPLE)
    assert specs["high_aes_general_v50"].input_image_limit == 1
    assert specs["high_aes_general_v50"].input_image_limits is None


def test_input_image_limit_malformed_array_is_rejected_wholesale():
    """任何元素畸形 ⇒ **整体**当读不到（一半真一半猜比全猜更危险）。"""
    for bad in (
        ["not-a-dict"],                                     # 元素不是 dict
        [{"ability_name": "byte_edit"}],                    # 缺 max_image_num
        [{"max_image_num": 10}],                            # 缺 ability_name
        [{"max_image_num": 0, "ability_name": "byte_edit"}],  # 0 无意义
        [{"max_image_num": True, "ability_name": "byte_edit"}],  # bool 不是数
    ):
        specs, _ = parse_common_config(
            {"data": {"model_list": [{"model_req_key": "m",
                                      "input_image_limit": bad}]}})
        assert specs["m"].input_image_limits is None, f"畸形形态被当真了：{bad}"
        assert specs["m"].input_image_limit is None


def test_input_image_limit_for_query_prefers_array_then_scalar():
    """查询优先按 ability 精确值；只有标量时退模型级统一值；都没有 → None。"""
    c = _StubClient(ARRAY_SAMPLE)
    cache = ModelConfigCache(c, ttl=999)
    assert cache.input_image_limit_for("high_aes_general_v50p", "byte_edit") == 10
    assert cache.input_image_limit_for("high_aes_general_v50p", "other") is None
    assert cache.input_image_limit_for("no_such_model", "byte_edit") is None

    c2 = _StubClient(SAMPLE)
    cache2 = ModelConfigCache(c2, ttl=999)
    assert cache2.input_image_limit_for("high_aes_general_v50", "byte_edit") == 1


# ---------------------------------------------------------------------------
# 缓存行为
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def common_config(self, **_kw):
        self.calls += 1
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_cache_reads_once_then_serves_from_memory():
    c = _StubClient(SAMPLE)
    cache = ModelConfigCache(c, ttl=999)
    assert cache.count_options("high_aes_general_v50") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert cache.count_options("high_aes_general_v50") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert c.calls == 1, "TTL 内不应重复打上游"


def test_probe_failure_falls_back_to_frozen_snapshot_and_reports_degradation():
    """探测失败 ⇒ 用冻结快照，**但必须明说**（静默降级 = 让人按 A 的预期为 B 付费）。"""
    c = _StubClient(RuntimeError("boom"))
    cache = ModelConfigCache(c, ttl=0)
    opts = cache.count_options("high_aes_general_v50")
    assert opts == (1, 2, 3, 4, 5, 6, 7, 8), "冻结快照里 v50 就是 1..8"
    note = cache.degradation_note("high_aes_general_v50")
    assert note and "未能读取" in note
    assert cache.stats()["last_error"]


def test_unknown_model_is_not_reported_as_degradation():
    """模型本来就不在服务端表里属正常（新模型），不该报降级 —— 那会让告警失去意义。"""
    c = _StubClient(SAMPLE)
    cache = ModelConfigCache(c, ttl=999)
    cache.snapshot()
    from app.upstream.jimeng.client import DEFAULT_COUNT_OPTIONS

    assert cache.count_options("brand_new_model") == DEFAULT_COUNT_OPTIONS
    assert cache.degradation_note("brand_new_model") is None
