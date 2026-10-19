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

from app.main import _sync_generate
from app.service import Service

ROOT = Path(__file__).resolve().parent.parent


def test_service_forwards_count_to_bridge():
    """国际站**恒按 1 张出图**（2026-10-19 用户口径"张数全部固定死了 1"）。

    国际站 36/张（n=4 = 144）⇒ 成本控制；并要求"少出图留痕"。
    国内站不受影响（直出路径仍按 n）。
    """
    src = inspect.getsource(Service._submit_via_bridge)
    assert "count = 1" in src, "国际站必须固定 1 张"
    assert '"count": count' in src, "body 必须用固定值"
    assert "固定按 1 张出图" in src, "请求 n≠1 时必须留痕"


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


def test_bridge_submit_falls_back_to_default_model():
    """`rec.upstream_model` 为空时必须兜底默认模型。

    2026-10-19 线上实测事故：原来写 `rec.upstream_model or ""` ⇒ body 的
    `model` 为空串 ⇒ bridge 的 `if (model)` 为假 ⇒ **不选模型** ⇒ UI 用
    它自己的默认档（实测 `dreamina_gpt_image_2_5_flare`，**51 积分/张**），
    而调用方以为在用 Lite（36/张）。**静默换模型 = 静默多花钱**。
    """
    src = inspect.getsource(Service._submit_via_bridge)
    assert "rec.upstream_model or DEFAULT_MODEL" in src, \
        "必须兜底：跑 bridge 的 body 里 model 不能是空串"


def test_sg_rejects_i2i_upfront():
    """国际站的图生图**必须在派发期拒绝**（2026-10-19）。

    国际站图片上传被拒（AWS4 签名不匹配：`ApplyImageUpload 失败
    SignatureDoesNotMatch`）⇒ i2i 必然失败；而且上游给的是
    **HTTP 200 + 错误体**，同步调用方会误判成功、还要白等 7 秒。

    门禁钉两件事：① 派发期有前置拒绝；② 同步链把 `invalid_parameter`
    的失败转成 4xx（不再 200 + failure 体）。
    """
    submit_src = inspect.getsource(Service._submit)
    assert "暂不支持图生图" in submit_src, "国际站 i2i 必须前置拒绝"
    assert '"sg"' in submit_src and "image_uris" in submit_src, \
        "判据必须是「站点=sg」且「有垫图」"

    # 🔴 真正的入口是**上传**（在 `_submit` 之前）—— 只在 `_submit` 拦会走不到
    prep_src = inspect.getsource(Service._prepare_input_images)
    assert "暂不支持图生图" in prep_src, \
        "垫图上传入口必须也拒绝（否则会白等 5s 才拿到 SignatureDoesNotMatch）"

    sync_src = inspect.getsource(_sync_generate)
    assert "InvalidParameterError.err_type" in sync_src, \
        "同步链要把它转成 400，而不是回 200 + failure 体"
