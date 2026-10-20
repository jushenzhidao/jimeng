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


def test_sg_i2i_upload_signs_with_passed_region():
    """国际站上传的 AWS4 **密钥推导**必须用传入的 region（2026-10-19）。

    浏览器抓包对照后发现：scope 写 `ap-singapore-1`、kDate 却按模块常量
    `REGION`（cn-north-1）推导 ⇒ 恒 `SignatureDoesNotMatch`(100010)。
    改 `_hmac(k, region)` 后国际站上传成功（返回 `tos-alisg-i-.../xxx`）。

    🔴 同一类错误在 `service` 段已经踩过一次（scope 改 vod、推导仍用 imagex）
    —— 这条门禁把 region 维度的坑也钉住。
    """
    import inspect as _inspect
    from app.upstream.jimeng import upload as up

    fn_src = _inspect.getsource(up)
    assert "_hmac(k, region)" in fn_src, "kDate 必须用传入的 region"
    assert "_hmac(k, REGION)" not in fn_src, "不许再用模块常量 REGION 推导"


def test_sg_submit_routes_through_bridge_page_fetch(monkeypatch):
    """国际站的**写接口**必须经桥的页面上下文代发（2026-10-20 实测）。

    直连 `aigc_draft/generate` 恒 `ret=-6 shark not pass reject`（shark 验
    secsdk 运行时会话）；同一个请求在页面里 `fetch` 就通过 ⇒ 提交流量改走
    `sg-bridge` 的 `/fetch`。门禁钉三件事：
      ① `_build_bundle` 给 sg 客户端挂 `bridge_url`（漏接 = 静默直连被拦）；
      ② `_post` 只对 `PATH_SUBMIT` 走桥（读接口/STS 不该被拖慢）；
      ③ 转发的 body/URL 原样（桥只是通道，不做改写）。
    """
    from app.upstream.jimeng import client as cmod
    from app.upstream.jimeng.site import SG_SITE

    src = inspect.getsource(Service._build_bundle)
    assert "c.bridge_url" in src and '"sg"' in src, "sg 客户端必须挂 bridge_url"
    post_src = inspect.getsource(cmod.JimengClient._post)
    assert "PATH_SUBMIT" in post_src and "_post_via_bridge" in post_src, \
        "提交流量必须改走桥"

    seen: dict = {}

    class _R:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": 200, "json": {"ret": "0", "data": {"ok": 1}}}

    def _fake_post(url, **kw):
        seen["url"] = url
        seen["payload"] = kw["json"]
        return _R()

    monkeypatch.setattr(cmod.httpx, "post", _fake_post)
    c = cmod.JimengClient(sessionid="a" * 32, profile=SG_SITE,
                          capture_upstream=False)
    try:
        c.bridge_url = "http://sg-bridge:8300"
        out = c._post(cmod.PATH_SUBMIT, {"draft_content": "{}"})
    finally:
        c.close()
    assert out["ret"] == "0"
    assert seen["url"].endswith("/fetch"), "必须打桥的 /fetch"
    assert seen["payload"]["body"] == {"draft_content": "{}"}, "body 必须原样转发"
