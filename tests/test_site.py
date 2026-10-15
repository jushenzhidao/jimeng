# -*- coding: utf-8 -*-
"""国际版 Dreamina（SG 站）适配的门禁（2026-10-15）。

钉住四类最容易塌的事：

1. **站点常量 = 抓包事实**（site.py 两实例的取值逐字段钉死；
   国际版 sign 向量 = 用户抓包的 (path, ts, sign) 三元组验算）；
2. **client 站点化请求形态**（sg 的 URL/头/体逐项对抓包；cn 默认行为
   **逐字节不变** —— 等价重构不许漂移）；
3. **按凭据自动探测站点**（cn 1015 → sg 归属 sg；全挂默认 cn 且不缓存；
   探测成功的能力表预热 cfg —— 同一凭据只发一次读请求）；
4. **站点↔模型一致性**（sg 特有模型配 cn 凭据 = 受理/派发两道闸；
   派发闸拦在提交上游之前 = 零计费风险）。

零真实上游调用：探测与建连全部走 httpx.MockTransport。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.upstream.jimeng.client import BASE as CN_BASE
from app.upstream.jimeng.client import JimengClient
from app.upstream.jimeng.site import CN_SITE, SG_SITE, site_by_name
from app.upstream.jimeng.upload import REGION as CN_AWS_REGION
from app.upstream.jimeng.upload import _aws_region


# ---------------------------------------------------------------------------
# ① 站点常量 = 抓包事实
# ---------------------------------------------------------------------------

def test_sg_site_constants_match_the_capture():
    """国际版常量逐项对 2026-10-15 抓包。改这些值 = 声明"上游改版了"，
    必须有新抓包依据，不许照命名习惯猜。"""
    assert SG_SITE.name == "sg"
    assert SG_SITE.base == "https://mweb-api-sg.capcut.com"
    assert SG_SITE.origin == "https://dreamina.capcut.com"
    assert SG_SITE.aid == "513641"
    assert SG_SITE.da_version == "3.3.30"
    assert SG_SITE.region == "PH"
    assert SG_SITE.loc == "PH"
    assert SG_SITE.sec_fetch_site == "same-site"
    assert SG_SITE.store_country_code == "ph"
    assert CN_SITE.name == "cn"
    assert CN_SITE.base == CN_BASE == "https://jimeng.jianying.com"
    assert CN_SITE.aid == "513695"
    assert CN_SITE.da_version == "3.3.28"
    assert CN_SITE.store_country_code == ""       # 国内不带这个头
    assert CN_SITE.sec_fetch_site == "same-origin"


def test_sg_sign_vector_from_capture():
    """国际版 sign 算法 = 国内同款。向量来自用户抓包（device-time=1792059011，
    sign=f9fa6bce…）—— 算法失效/前缀变化时这里红。"""
    from app.upstream.jimeng.sign import sign_headers
    h = sign_headers("/mweb/v1/aigc_draft/generate", ts=1792059011)
    assert h["sign"] == "f9fa6bce0f3447aa936d528b2309ff9a"


def test_site_by_name_and_unknown_rejects():
    assert site_by_name("cn") is CN_SITE
    assert site_by_name("sg") is SG_SITE
    with pytest.raises(KeyError):
        site_by_name("jp")


def test_with_overrides_only_touches_account_scoped_fields():
    """region/store-country-code 可覆盖；**域名/aid 不许被配置改**（那是站点事实）。"""
    from app.upstream.jimeng.site import with_overrides
    p = with_overrides(SG_SITE, region="US", store_country_code="us")
    assert p.region == "US" and p.store_country_code == "us"
    assert p.base == SG_SITE.base and p.aid == SG_SITE.aid
    # 缺省原样
    assert with_overrides(SG_SITE) is SG_SITE


# ---------------------------------------------------------------------------
# ② client 站点化请求形态
# ---------------------------------------------------------------------------

class _Capture(httpx.BaseTransport):
    """记录请求的假 transport（按 path 前缀回不同剧本）。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _ok_config(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ret": "0", "errmsg": "success",
                                     "data": {"model_list": []}})


def _query(req: httpx.Request) -> dict[str, str]:
    """httpx 请求的 query 参数（已解码，单值）。"""
    return dict(req.url.params)


def test_sg_client_request_shape_matches_capture():
    """sg 客户端的 URL / 头 / 体逐项对抓包。"""
    tp = _Capture(_ok_config)
    c = JimengClient(sessionid="abc", profile=SG_SITE, transport=tp)
    c.common_config()
    req = tp.requests[0]
    assert req.url.host == "mweb-api-sg.capcut.com"
    q = _query(req)
    assert q["aid"] == "513641"
    assert q["region"] == "PH"
    assert q["da_version"] == "3.3.30"
    assert req.headers["appid"] == "513641"
    assert req.headers["origin"] == "https://dreamina.capcut.com"
    assert req.headers["referer"] == "https://dreamina.capcut.com/"
    assert req.headers["sec-fetch-site"] == "same-site"
    assert req.headers["store-country-code"] == "ph"
    assert req.headers["store-country-code-src"] == "uid"
    assert req.headers["did"].isdigit() and len(req.headers["did"]) == 19
    assert req.headers["lan"] == "zh-Hant-TW"
    assert req.headers["loc"] == "PH"
    assert "sign" in req.headers and "device-time" in req.headers
    c.close()


def test_sg_submit_body_carries_sg_aid_and_da_version():
    """提交体的 `http_common_info.aid` 与 draft `version` 必须跟站点走。"""
    tp = _Capture(_ok_config)
    c = JimengClient(sessionid="abc", profile=SG_SITE, transport=tp)
    sid = c.submit("a cat", model="dreamina_gpt_image_2_5_flare",
                   count_options=(1,), dry_run=False)
    req = tp.requests[0]
    body = json.loads(req.content)
    assert body["http_common_info"] == {"aid": 513641}
    draft = json.loads(body["draft_content"])
    assert draft["version"] == "3.3.30"
    core = draft["component_list"][0]["abilities"]["generate"]["core_param"]
    assert core["model"] == "dreamina_gpt_image_2_5_flare"
    assert body["extend"]["root_model"] == "dreamina_gpt_image_2_5_flare"
    assert sid
    c.close()


def test_cn_default_behavior_is_byte_identical():
    """🔴 不传 profile（默认 cn）的请求形态必须与 profile 化之前一致 ——
    等价重构不许让存量请求漂移一个字节。"""
    tp = _Capture(_ok_config)
    c = JimengClient(sessionid="abc", transport=tp)   # 不传 profile
    c.common_config()
    req = tp.requests[0]
    assert req.url.host == "jimeng.jianying.com"
    q = _query(req)
    assert q["aid"] == "513695"
    assert q["region"] == "cn"
    assert q["da_version"] == "3.3.28"
    assert req.headers["origin"] == "https://jimeng.jianying.com"
    assert req.headers["sec-fetch-site"] == "same-origin"
    assert "store-country-code" not in req.headers
    assert "did" not in req.headers
    assert req.headers["lan"] == "zh-Hans"
    assert req.headers["appid"] == "513695"
    c.close()


# ---------------------------------------------------------------------------
# ③ 按凭据自动探测站点
# ---------------------------------------------------------------------------

def _svc(settings, store, handler):
    from app.service import Service
    return Service(settings, store=store,
                   upstream_transport=_Capture(handler))


def _ret(ret: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": ret, "errmsg": "x", "data": {}})
    return handler


def test_probe_cn_credential_lands_on_cn(settings, store):
    """国内凭据：cn 探测一次就成功，归属 cn，不再打国际站。"""
    tp_holder: dict[str, _Capture] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        tp_holder["req"] = request
        return httpx.Response(200, json={"ret": "0", "data": {"model_list": []}})

    tp = _Capture(handler)
    from app.service import Service
    svc = Service(settings, store=store, upstream_transport=tp)
    bundle = svc.bundle_for("cn-cred-1")
    assert bundle[0].profile.name == "cn"
    assert svc._site_cache["cn-cred-1"].name == "cn"
    hosts = [r.url.host for r in tp.requests]
    assert set(hosts) == {"jimeng.jianying.com"}, f"不该打国际站：{hosts}"


def test_probe_sg_credential_falls_through_cn(settings, store):
    """国际凭据：cn 1015 → sg ret=0 ⇒ 归属 sg，bundle 用 sg 客户端。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "jimeng.jianying.com":
            return httpx.Response(200, json={"ret": "1015", "errmsg": "login error"})
        return httpx.Response(200, json={"ret": "0", "data": {"model_list": []}})

    svc = _svc(settings, store, handler)
    bundle = svc.bundle_for("sg-cred-1")
    assert bundle[0].profile.name == "sg"
    assert svc._site_cache["sg-cred-1"].name == "sg"


def test_probe_uncertain_falls_back_to_cn_and_does_not_cache(settings, store):
    """🔴 两站都网络失败 ⇒ 默认 cn 建 bundle，但**不缓存归属** ——
    瞬时故障不许把凭据钉死在错误的站（下次再探）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={})

    svc = _svc(settings, store, handler)
    bundle = svc.bundle_for("flaky-cred")
    assert bundle[0].profile.name == "cn"
    assert "flaky-cred" not in svc._site_cache
    # 两站各试过一次（探测顺序 cn → sg）；其后默认 cn 的 bundle 回填门控
    # 还会补一次读（探测失败没 seed），所以只断言**前两次**的顺序。
    hosts = [r.url.host for r in svc._upstream_transport.requests]
    assert hosts[:2] == ["jimeng.jianying.com", "mweb-api-sg.capcut.com"]


def test_probe_warms_cfg_without_second_read(settings, store):
    """探测（history）+ 能力表回填（common_config）＝ 2 次读；TTL 内的
    快照刷新**不再**发新请求 —— `_warm_cfg` 的读必须被缓存接住。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": "0",
                                         "data": {"model_list": []}})

    svc = _svc(settings, store, handler)
    bundle = svc.bundle_for("warm-cred")
    n = len(svc._upstream_transport.requests)
    assert n == 2, f"探测+能力表应恰好 2 次读，实得 {n}"
    # TTL 内再取快照 —— 不该产生新的网络请求
    assert bundle[3].snapshot() is not None
    assert len(svc._upstream_transport.requests) == n, \
        "能力表快照没被缓存 ⇒ 同一凭据重复打上游"


def test_probe_uses_history_not_common_config(settings, store):
    """🔴 探测端点必须是 get_history_by_ids —— get_common_config 在国内站
    匿名可读（SG 凭据打 CN 照样 ret=0），用它会把国际凭据误判成 cn。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.path))
        return httpx.Response(200, json={"ret": "0", "data": {}})

    svc = _svc(settings, store, handler)
    svc.bundle_for("endpoint-cred")
    assert "/mweb/v1/get_history_by_ids" in seen, seen
    assert "/mweb/v1/get_common_config" in seen   # 能力表由回填读，也在
    # 探测那次（第一次请求）必须是 history
    assert seen[0] == "/mweb/v1/get_history_by_ids", seen


def test_second_bundle_for_hits_pool_without_new_probe(settings, store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": "0", "data": {"model_list": []}})

    svc = _svc(settings, store, handler)
    b1 = svc.bundle_for("pool-cred")
    n = len(svc._upstream_transport.requests)
    b2 = svc.bundle_for("pool-cred")
    assert b1 is b2
    assert len(svc._upstream_transport.requests) == n


# ---------------------------------------------------------------------------
# ④ 站点 ↔ 模型一致性
# ---------------------------------------------------------------------------

def test_model_sites_classification():
    from app.models import model_sites
    assert model_sites("dreamina_gpt_image_2_5_flare") == frozenset({"sg"})
    assert model_sites("gpt_image_2") == frozenset({"sg"})
    assert model_sites("high_aes_general_v50") == frozenset({"cn", "sg"})
    assert model_sites("jm_image_model_yc_mj82") == frozenset({"cn", "sg"})
    assert model_sites(None) is None
    assert model_sites("not-a-key") is None


def test_sg_only_aliases_resolve_and_gpt_prefix_survives_placeholder():
    """`gpt-image` 在占位前缀里 —— 别名必须穿过（否则静默降级 Lite）。"""
    from app.models import resolve
    for raw, key in (
        ("gpt-image-2-5-flare", "dreamina_gpt_image_2_5_flare"),
        ("GPT Image 2.5 Flare", "dreamina_gpt_image_2_5_flare"),
        ("gpt-image-2", "gpt_image_2"),
        ("nano-banana-pro", "gemini_3_image_pro"),
        ("Nano Banana 2.1", "jm_image_model_gg_bp_21"),
    ):
        cap, got = resolve(raw, has_image=False)
        assert cap.api_id == "jimeng-t2i"
        assert got == key, f"{raw!r} ⇒ {got!r}，期望 {key!r}"


def test_sg_models_have_no_measured_price():
    """🔴 未实测 = None = 不报价。谁手滑填了数字，谁就得先拿出对账证据。"""
    from app.models import UPSTREAM_MODEL_CREDITS
    for k in ("dreamina_gpt_image_2_5_flare", "dreamina_gpt_image_2_5_sunburst",
              "gpt_image_2", "gemini_3_image_pro", "jm_image_model_gg_bp_21"):
        assert UPSTREAM_MODEL_CREDITS[k] is None, \
            f"{k} 的价格没对过账，不许报数"


def test_check_site_of_record_blocks_mismatch_before_submit(settings, store):
    """派发闸：sg 特有模型 + cn 归属 ⇒ JimengParamError（不提交上游）。"""
    from app.service import Service
    from app.store import TaskRecord
    from app.upstream.jimeng import JimengParamError

    svc = Service(settings, store=store,
                  upstream_transport=_Capture(_ret("0")))
    svc._site_cache["cn-sid"] = CN_SITE
    rec = TaskRecord(task_id="jimeng_x", credential_id="c",
                     upstream_sessionid="cn-sid", model="jimeng-t2i",
                     cap_key="jimeng:t2i", status="queued", prompt="p",
                     upstream_model="dreamina_gpt_image_2_5_flare")
    cap = models_cap("t2i")
    with pytest.raises(JimengParamError):
        svc._check_site_of_record(rec, cap)


def test_check_site_of_record_passes_when_unjudged_or_shared(settings, store):
    """未判定归属放行（保守，不误杀）；共享模型（v50）两站都放行。"""
    from app.service import Service
    from app.store import TaskRecord

    svc = Service(settings, store=store,
                  upstream_transport=_Capture(_ret("0")))
    cap = models_cap("t2i")
    # 未判定
    rec = TaskRecord(task_id="jimeng_x", credential_id="c",
                     upstream_sessionid="unknown-sid", model="jimeng-t2i",
                     cap_key="jimeng:t2i", status="queued", prompt="p",
                     upstream_model="dreamina_gpt_image_2_5_flare")
    svc._check_site_of_record(rec, cap)          # 不抛
    # 共享模型 + 已判定 sg
    svc._site_cache["sg-sid"] = SG_SITE
    rec2 = TaskRecord(task_id="jimeng_y", credential_id="c",
                      upstream_sessionid="sg-sid", model="jimeng-t2i",
                      cap_key="jimeng:t2i", status="queued", prompt="p",
                      upstream_model="high_aes_general_v50")
    svc._check_site_of_record(rec2, cap)         # 不抛


def models_cap(name: str):
    from app.models import _BY_NAME
    return _BY_NAME[name]


# ---------------------------------------------------------------------------
# ⑤ 上传 region 取值（国际站上传链路**未验证**，这里只钉取值顺序）
# ---------------------------------------------------------------------------

def test_aws_region_order_token_first_then_domain_then_default():
    assert _aws_region({"region": "sg"}, "imagex-ap-singapore-1.bytevcloudapi.com") == "sg"
    assert _aws_region({}, "imagex-ap-singapore-1.bytevcloudapi.com") == "ap-singapore-1"
    assert _aws_region({}, "imagex.bytedanceapi.com") == CN_AWS_REGION == "cn-north-1"
    assert _aws_region({}, "") == "cn-north-1"


# ---------------------------------------------------------------------------
# ⑥ 配置：SG 三项从 env 读
# ---------------------------------------------------------------------------

def test_settings_read_sg_overrides_from_env(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("JIMENG_SG_REGION", "US")
    monkeypatch.setenv("JIMENG_SG_STORE_CC", "us")
    monkeypatch.setenv("JIMENG_SG_WORKSPACE_ID", "20712409081860")
    st = Settings.from_env()
    assert st.jimeng_sg_region == "US"
    assert st.jimeng_sg_store_cc == "us"
    assert st.jimeng_sg_workspace_id == 20712409081860
    # 缺省 = 空串/None（profile 原样，不覆盖）
    monkeypatch.delenv("JIMENG_SG_REGION")
    monkeypatch.delenv("JIMENG_SG_STORE_CC")
    monkeypatch.delenv("JIMENG_SG_WORKSPACE_ID")
    st2 = Settings.from_env()
    assert st2.jimeng_sg_region == ""
    assert st2.jimeng_sg_workspace_id is None


def test_profile_for_applies_sg_overrides_only(settings, store):
    from app.service import Service

    settings.jimeng_sg_region = "US"
    svc = Service(settings, store=store,
                  upstream_transport=_Capture(_ret("0")))
    p = svc._profile_for(SG_SITE)
    assert p.region == "US" and p.aid == "513641"
    assert svc._profile_for(CN_SITE) is CN_SITE


# ---------------------------------------------------------------------------
# ⑦ GPT Image 系清晰度定死 1k + degradations 展示开关（2026-10-15 用户口径）
# ---------------------------------------------------------------------------

def test_pinned_resolution_tier_classification():
    from app.models import pinned_resolution_tier
    assert pinned_resolution_tier("dreamina_gpt_image_2_5_flare") == "1k"
    assert pinned_resolution_tier("dreamina_gpt_image_2_5_sunburst") == "1k"
    assert pinned_resolution_tier("gpt_image_2") == "1k"
    # 🔴 2026-10-16 口径：nano-banana 两模型**也**定死 1k（5 个 SG 专属
    # 模型全覆盖；Seedream 家族双站不限）
    assert pinned_resolution_tier("gemini_3_image_pro") == "1k"
    assert pinned_resolution_tier("jm_image_model_gg_bp_21") == "1k"
    assert pinned_resolution_tier("high_aes_general_v50") is None  # 双站不限
    assert pinned_resolution_tier("high_aes_general_v50") is None
    assert pinned_resolution_tier(None) is None


def test_gpt_image_pin_rewrites_size_on_accept(client, client_state):
    """受理 flare 2k ⇒ 落库尺寸被定死到 1k（静态兜底 1024²）、tier=1k、留痕落库。"""
    from app.service import view as _view  # noqa: F401  （确认接线存在）
    r = client.post("/async/v1/images/generations",
                    json={"model": "gpt-image-2-5-flare", "prompt": "a cat",
                          "size": "2048x2048"},
                    headers={"Authorization": "Bearer sk-test-key-0000000000000000"})
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    r2 = client.get(f"/async/v1/images/generations/{task_id}",
                    headers={"Authorization": "Bearer sk-test-key-0000000000000000"})
    assert r2.status_code == 202          # 协调器关闭 ⇒ 保持 queued（不是错误）
    body = r2.json()
    # 受理态查不到 size 字段也没关系 —— 响应形状由 view 决定；这里断言
    # degradations 里有"定死 1k"留痕（夹具 show_degradations=True）
    deg = json.dumps(body.get("degradations") or [], ensure_ascii=False)
    assert "恒 1k 生成" in deg, f"pin 未留痕: {deg}"


def test_view_hides_degradations_by_default():
    """🔴 生产默认（show_degradations=False）响应体**没有** degradations 键。"""
    from app.service import view
    from app.store import TaskRecord

    rec = TaskRecord(task_id="jimeng_d", credential_id="c",
                     upstream_sessionid="s", model="jimeng-t2i",
                     cap_key="jimeng:t2i", status="queued", prompt="p",
                     degradations=["内部留痕"])
    _code, body_on = view(rec, show_degradations=True)
    assert body_on.get("degradations") == ["内部留痕"]
    _code, body_off = view(rec, show_degradations=False)
    assert "degradations" not in body_off, body_off


def test_settings_show_degradations_env(monkeypatch):
    from app.config import Settings
    assert Settings.from_env().show_degradations is False   # 默认不展示
    monkeypatch.setenv("SHOW_DEGRADATIONS", "1")
    assert Settings.from_env().show_degradations is True


FLARE_RMAP: dict = {
    "1k": {"image_ratio_sizes": [
        {"ratio_type": 1, "width": 1024, "height": 1024}]},
    "2k": {"image_ratio_sizes": [
        {"ratio_type": 1, "width": 2048, "height": 2048}]},
    "4k": {"image_ratio_sizes": [
        {"ratio_type": 1, "width": 4096, "height": 4096}]},
}


def test_gpt_image_2k_plans_one_upscale_level(settings, store, fake_jimeng,
                                              fake_uploader, fake_vod):
    """flare 请求 2k ⇒ 生成腿锁 1k（1024 槽）+ **1 级**超分计划。"""
    from app.service import Service
    from tests.conftest import FakeConfigCache

    settings.upscale_4k_enabled = True
    svc = Service(settings, store=store, client=fake_jimeng,
                  uploader=fake_uploader, vod=fake_vod,
                  cfg=FakeConfigCache(rmap=FLARE_RMAP),
                  upstream_transport=_Capture(_ret("0")))
    rec = svc.create({"model": "gpt-image-2-5-flare", "prompt": "a cat",
                      "size": "2048x2048"},
                     credential="c", sessionid="pin-cred")
    assert rec.size == "1024x1024"
    assert rec.resolution_tier == "1k"
    plan = json.loads(rec.extra_json or "{}").get("upscale") or {}
    assert plan.get("levels") == 1 and plan.get("gen_tier") == "1k"
    assert plan.get("gen_size") == "1024x1024"
    assert plan.get("requested_size") == "2048x2048"
    joined = json.dumps(rec.degradations, ensure_ascii=False)
    assert "恒 1k 生成" in joined and "1 级超清" in joined


def test_gpt_image_4k_is_rejected_at_accept(settings, store, fake_jimeng,
                                            fake_uploader, fake_vod):
    """🔴 SG 专属模型的 4k 请求**不受理**（2026-10-15 口径"4k 不受理 都走
    超分"）：实测 UI 4K 档收 50 credits 交付 2048²——按 4k 收费交 2k 图
    是资损源。受理层 400 拒绝，错误信息指引 2k。

    🔴 回归史：本用例的前身锁的是「4k → 1k 生成 + 2 级超分链」——那是
    直出路径的设计，而 SG 模型**没有直出路径**（shark 拦 ⇒ 只有 bridge
    UI 通路），超分腿的 UI 自动化未做 ⇒ 4k 超分链在 SG 上执行不了，
    受理了只会卡死。多级链代码保留（`_plan_gpt_image_upscale`），待
    SG 超分腿开放后本用例再改回「受理 + 降级 2k + 超分」。
    """
    import pytest
    from app.errors import InvalidParameterError
    from app.service import Service
    from tests.conftest import FakeConfigCache

    settings.upscale_4k_enabled = True
    svc = Service(settings, store=store, client=fake_jimeng,
                  uploader=fake_uploader, vod=fake_vod,
                  cfg=FakeConfigCache(rmap=FLARE_RMAP),
                  upstream_transport=_Capture(_ret("0")))
    with pytest.raises(InvalidParameterError) as ei:
        svc.create({"model": "gpt-image-2-5-flare", "prompt": "a cat",
                    "size": "4096x4096"},
                   credential="c", sessionid="pin-cred")
    msg = str(ei.value)
    assert "4k" in msg and "2k" in msg, msg


# ---------------------------------------------------------------------------
# ⑧ GPT Image 多级超分链的推进（1k 生成 → 级 1 → 级 2 → 交付）
# ---------------------------------------------------------------------------

def _sg_service(settings, store, fake_jimeng, fake_uploader, fake_vod):
    from app.service import Service
    from tests.conftest import FakeConfigCache
    settings.upscale_4k_enabled = True
    return Service(settings, store=store, client=fake_jimeng,
                   uploader=fake_uploader, vod=fake_vod,
                   cfg=FakeConfigCache(rmap=FLARE_RMAP),
                   upstream_transport=_Capture(_ret("0")))


def _leg_ok(url: str, *, hist: str, item: int, size: tuple[int, int]):
    """一条成功的超分腿：带本腿 history 与产物 item（下一级引用的原料）。"""
    from app.upstream.jimeng import GeneratedImage, TaskState
    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False, cost=9)
    st.history_record_id = hist
    st.images = [GeneratedImage(url=url, width=size[0], height=size[1],
                                format="png", item_id=item)]
    return st


def _test_gpt_image_4k_chain_runs_two_upscale_levels_DISABLED(
        client, client_state, fake_jimeng, service):
    """⛔ 已停用（2026-10-15）：SG 4k 不受理后此链路不可达。

    保留函数体作参考 —— SG 超分腿（normal_hd UI 自动化）开放后，
    按「受理 2k 降级 + 超分链」口径恢复并改名。
    """
    from tests.test_coordinator import submitted_state
    from tests.test_upscale_4k import _gen_ok
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen-1k.png"], hist="44850000000001",
                items=(777,), size=(1024, 1024)),
        _leg_ok("https://cdn/up-l1.png", hist="44850000000002",
                item=888, size=(2048, 2048)),
        _leg_ok("https://cdn/up-l2.png", hist="44850000000003",
                item=999, size=(4096, 4096)),
    ]
    tid = _create(client, model="gpt-image-2-5-flare", size="4096x4096")

    client_state.coordinator.tick()          # 建任务 + 第 1 次轮询（submitted）
    client_state.coordinator.tick()          # 生成腿成功 ⇒ 接第 1 级
    edits = fake_jimeng.of("edit")
    assert len(edits) == 1 and edits[0]["item_id"] == 777
    assert edits[0]["origin_history_id"] == 44850000000001

    client_state.coordinator.tick()          # 级 1 成功 ⇒ 接第 2 级
    edits = fake_jimeng.of("edit")
    assert len(edits) == 2, "级 1 完成后必须自动接第 2 级"
    assert edits[1]["item_id"] == 888, "第 2 级必须引用第 1 级的**产物**"
    assert edits[1]["origin_history_id"] == 44850000000002, \
        "第 2 级的 history 必须是第 1 级**超分腿**的（不是生成腿的）"
    rec = service.store.get(tid)
    assert rec.status == "in_progress", "两级未走完不许判成功"

    client_state.coordinator.tick()          # 级 2 成功 ⇒ 收尾
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/up-l2.png"]
    joined = " ".join(rec.degradations)
    assert "继续第 2 级" in joined
    assert "GPT Image 超分链完成" in joined
    assert "4096x4096" in joined, "交付尺寸必须写进留痕"


def _test_gpt_image_chain_level_failure_DISABLED(
        client, client_state, fake_jimeng, service):
    """⛔ 已停用（同上，4k 链不可达）。🔴 第 2 级失败 ⇒ 交付**第 1 级
    产物**（2048），不判任务失败。

    与国内改道同一口径：腿失败不砸任务，退回上一级并响亮留痕
    （该张尺寸低于请求档，调用方在留痕里看得见）。
    """
    from tests.test_coordinator import failed_state, submitted_state
    from tests.test_upscale_4k import _gen_ok
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen-1k.png"], hist="44850000000001",
                items=(777,), size=(1024, 1024)),
        _leg_ok("https://cdn/up-l1.png", hist="44850000000002",
                item=888, size=(2048, 2048)),
        failed_state("generate_failed"),
    ]
    tid = _create(client, model="gpt-image-2-5-flare", size="4096x4096")

    client_state.coordinator.tick()
    client_state.coordinator.tick()
    client_state.coordinator.tick()          # 级 1 成功 ⇒ 接第 2 级
    assert len(fake_jimeng.of("edit")) == 2
    client_state.coordinator.tick()          # 级 2 失败（第 1 次）⇒ 先重试
    rec2 = service.store.get(tid)
    assert rec2.status == "in_progress", "预算内（UPSCALE_MAX_RETRIES=1）先重试"
    client_state.coordinator.tick()          # 级 2 再失败（超预算）⇒ 退回级 1
    rec = service.store.get(tid)
    assert rec.status == "success", "级 2 失败不判任务失败"
    assert [i["url"] for i in rec.images] == ["https://cdn/up-l1.png"], \
        "退回的必须是**上一级**产物（2048），不是 1k 原图"
    joined = " ".join(rec.degradations)
    assert "未能完成超分" in joined and "上一级产物" in joined


def _create(client, **body):
    body.setdefault("model", "jimeng-t2i")
    body.setdefault("prompt", "a cat")
    r = client.post("/async/v1/images/generations", json=body,
                    headers={"Authorization":
                             "Bearer sk-test-key-0000000000000000"})
    assert r.status_code == 202, r.text
    return r.json()["task_id"]
