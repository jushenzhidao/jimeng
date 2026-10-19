# -*- coding: utf-8 -*-
"""`sg-bridge` 路径的**张数**必须透传（2026-10-19 用户口径）。

背景：国际站 UI 参数面板有 "Images to generate"（1..8），**默认 4 张**。
Lite 实测 **36 积分/张**（1 张 36、4 张 144，`user_credit_history` 两证）
⇒ 调用方要 1 张、我们却按默认出 4 张，等于**多花 3 倍的钱**。

这条门禁用"源码级断言"而不是端到端（要真浏览器 + 真计费）：
钉的是**接线**——只要有人把 `count` 从 body 里删掉、或 bridge.js 不再点那个
控件，这里立刻红。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from app.service import Service

ROOT = Path(__file__).resolve().parent.parent


def test_service_forwards_count_to_bridge():
    """`_submit_via_bridge` 的 body 必须带 `count`（来自任务行的 `rec.n`）。"""
    src = inspect.getsource(Service._submit_via_bridge)
    assert '"count"' in src, "bridge body 少了 count ⇒ UI 按默认 4 张出图"
    assert "rec.n" in src, "count 必须来自任务行，不能写死"


def test_bridge_js_can_pick_image_count():
    """`bridge.js` 的 UI 流程必须能选 "Images to generate"。

    选择器依据（2026-10-19 实测 DOM）：
      `[class*='generate-count-field']` 里是 1..8 的叶子节点文本。
    上游改版时这里会红 —— 那时按新 DOM 改选择器即可（别删断言）。
    """
    src = (ROOT / "sg-bridge" / "bridge.js").read_text(encoding="utf-8")
    assert "generate-count-field" in src, "bridge.js 没有张数控件选择器"
    assert "generateUiCore({ prompt, model, resolution, ratio, count })" in src, \
        "generateUiCore 的入参少了 count"
    assert "ratio || resolution || count" in src, \
        "只传 count 时也必须打开参数面板，否则控件不在 DOM 里"
