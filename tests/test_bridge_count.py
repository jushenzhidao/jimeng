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

    选择器依据（2026-10-29 实测 DOM —— SG 前端改版后**换了形态**）：
      · 图片模式 = 首页上的 `AI Image` tab（`button` 文本，点完 URL 变 `?type=image`）；
      · 张数按钮 = `button[aria-label^="Images to generate"]`（文本 `1 image`），
        点开的 popover 里是 `label.lv-radio` 1..8，读回值也是这个 aria-label。
    旧形态（`[class*='generate-count-field']`）已从 DOM 消失，线上 793 单
    死在「张数控件找不到」。上游再改版时这里会红 —— 按新 DOM 改选择器即可。
    """
    src = (ROOT / "sg-bridge" / "bridge.js").read_text(encoding="utf-8")
    assert 'aria-label^="Images to generate"' in src, "bridge.js 没有张数按钮选择器"
    assert "label.lv-radio" in src, "张数/比例选项必须在 popover 的 radio 里点"
    assert "inImageMode" in src and '"AI Image"' in src, \
        "进图片模式的判据/tab 点击不能删（AI Agent 模式也有 combobox，会误判）"
    assert "张数选择失败" in src, "张数读回校验不能删（默认 4 张 = 4 倍成本）"
    assert "ratio, count," in src, "generateUiCore 的入参少了 count"
    #: 无提交探针：改版适配/回归都靠它（零积分）
    assert "/debug/ui-params" in src and "dryRun" in src, \
        "缺少零积分的参数探针，改版后无法在不花钱的前提下验证"


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


def test_bridge_accepts_tier_size_string(settings, store, monkeypatch):
    """`size="2K"` 必须能被 bridge 路径解析（2026-10-29 线上 `ValueError`）。

    调用方按 OpenAI 风格把 `size` 填成**档位串**是合规请求 —— `parse_size`
    早就接受 `1k/1.5k/2k/4k` 并按 1:1 展开（2026-10-14 用户口径）。
    而 `_submit_via_bridge` 原先手写 `int(x) for x in size.split("x")`，
    跟参数校验用的是**两套解析** ⇒ `int("2K")` 抛
    `ValueError: invalid literal for int() with base 10: '2K'`，
    被记成"未预期的内部错误"（线上 10 单）。
    """
    import httpx

    from app.store import TaskRecord

    seen: dict = {}

    class _R:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"ok": True, "handle": "h-1"}

    def _fake_post(url, **kw):
        seen["url"] = url
        seen["body"] = kw["json"]
        return _R()

    monkeypatch.setattr(httpx, "post", _fake_post)
    svc = Service(settings.replace(sg_bridge_url="http://sg-bridge:8300"),
                  store=store)
    rec = store.put(TaskRecord(
        task_id="jimeng_size_tier", credential_id="cred-a", model="jimeng-t2i",
        cap_key="jimeng:t2i", status="queued", prompt="p", size="2K", n=1,
        created_at=0, updated_at=0))

    sid = svc._submit_via_bridge(rec)

    assert sid == "bridge:h-1"
    assert seen["body"]["ratio"] == "1:1", \
        f"档位串没被展开成像素：{seen['body']}"
    assert seen["body"]["count"] == 1


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
    #: 🔴 只转协议头：浏览器指纹头转发给页内 fetch 会让 SDK 包装器崩
    #: （2026-10-20 实测 `TypeError: Cannot read properties of undefined`）。
    fwd = {k.lower() for k in seen["payload"]["headers"]}
    assert "sign" in fwd and "content-type" in fwd, "协议头必须带上"
    for bad_h in ("user-agent", "origin", "referer", "priority"):
        assert bad_h not in fwd, f"不许转发指纹头 {bad_h}"
