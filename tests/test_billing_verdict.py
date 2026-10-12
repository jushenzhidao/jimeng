#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账判据回归 —— `scripts/probe_res_tier_price.py::billing_verdict`。

背景（2026-10-12）：原实现是 `sid in json.dumps(整个响应)`，**子串匹配** ⇒
`--check -` 也回"✅ 已出账"（响应里到处是 '-'），任何短串/前缀一律假阳。
计费是红线（"实扣只认 user_credit_history"），判据不许松 ⇒ 钉在测试里。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    """按路径加载探针脚本（`scripts/` 不是包，只能这样 import）。"""
    spec = importlib.util.spec_from_file_location(
        "_probe_res_tier_price", REPO / "scripts" / "probe_res_tier_price.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load()
billing_verdict = probe.billing_verdict

RECORDS = [
    {"submit_id": "dabfa4ff-3e8e-427b-baac-d4a6515a6d02", "amount": 3,
     "title": "图片生成", "create_time": 1791791719},
    {"submit_id": "4b4195ff-1b48-4b71-b8ed-13febbf114b1", "amount": 20,
     "title": "积分到期清零"},
]


def test_exact_submit_id_is_a_hit():
    assert billing_verdict(RECORDS, "dabfa4ff-3e8e-427b-baac-d4a6515a6d02") == "hit"


def test_dash_is_not_a_hit():
    """🔴 这次的教训：`-` 在响应里到处都是（UUID 里就有），**绝不能算命中**。

    它落 `elsewhere`（确实在别的字段里出现过）也不算错，只要**不是 hit**。
    """
    assert billing_verdict(RECORDS, "-") != "hit"
    assert billing_verdict(RECORDS, "-") == "elsewhere"


def test_unknown_uuid_is_a_miss():
    assert billing_verdict(RECORDS, "deadbeef-0000-1111-2222-333333333333") == "miss"


def test_title_is_not_evidence():
    """`图片生成` 只出现在 title 里 ⇒ 不算出账（只能算线索）。

    顺带钉住 `ensure_ascii=False`：默认的 `json.dumps` 会把中文转义成
    「反斜杠 + uXXXX」形式，中文线索就永远命中不了（`elsewhere` 退化成 `miss`）。
    """
    assert billing_verdict(RECORDS, "图片生成") == "elsewhere"


def test_substring_of_a_real_id_is_not_a_hit():
    """前缀/子串不算命中 —— 必须是完整 submit_id。"""
    assert billing_verdict(RECORDS, "dabfa4ff") == "elsewhere"
    assert billing_verdict(RECORDS, "dabfa4ff-3e8e-427b-baac-d4a6515a6d02-x") == "miss"


def test_empty_records():
    assert billing_verdict([], "dabfa4ff-3e8e-427b-baac-d4a6515a6d02") == "miss"
    assert billing_verdict(None, "dabfa4ff-3e8e-427b-baac-d4a6515a6d02") == "miss"


def test_non_string_submit_id_in_records_still_matches():
    """上游偶发给非字符串 id ⇒ 归一化成 str 再比，别因此漏判。"""
    assert billing_verdict([{"submit_id": 12345}], "12345") == "hit"
