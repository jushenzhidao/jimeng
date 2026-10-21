#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外契约门禁（`docs/INTERFACE.md` 的冻结部分）。

这些用例断言的是**响应体的键集与形状**，不是"值大概对"。
多一个键、少一个键、类型不对，都要红 —— 契约保真是类型级的。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest

from tests.conftest import (AUTH, AUTH_B, KEY_B, failed_state, ok_state,
                            submitted_state)

BASE = "/async/v1/images/generations"

#: 一份**够用**的最小 PNG：`media.sniff` 按 magic bytes 判定类型，所以 8 字节头
#: 加上填充就足以走通"输入图"链路。刻意**不用外链 URL** —— 那会让后台执行期
#: 真的去发 HTTP（违反"测试零网络"纪律）；data URI 在 `media.load_one` 里
#: 直接内存解码，一个字节都不出网。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_create_returns_only_a_task_id(client):
    """受理**只回一个 id**（用户明确要求）。

    多回 `status`/`created_at`/`batch_key` 之类，会让调用方以为那些字段有语义。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "飞上天",
                                "image": []}, headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id"}, f"受理响应只应有 task_id，实得 {set(body)}"
    assert body["task_id"].startswith("jimeng_")
    assert len(body["task_id"]) == len("jimeng_") + 32


def test_create_sets_location_header(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    tid = r.json()["task_id"]
    assert r.headers["location"] == f"{BASE}/{tid}"


def test_create_does_not_touch_upstream(client, fake_jimeng):
    """受理在请求内**零上游往返** —— 建任务是计费动作，交给后台协调器。"""
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    assert fake_jimeng.calls == [], f"受理阶段不该调上游，实得 {fake_jimeng.kinds()}"


def test_placeholder_model_falls_back_to_t2i(client):
    """第三方 SDK 硬编码的占位名（dall-e-3 等）不代表调用意图。"""
    r = client.post(BASE, json={"model": "dall-e-3", "prompt": "x", "image": []},
                    headers=AUTH)
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# 输入校验：全部在**发出上游请求之前**
# ---------------------------------------------------------------------------


def test_image_string_is_accepted_as_a_single_image(client):
    """🔴 2026-10-14（用户口径）：`image` **字符串与数组都收**。

    非空字符串 = **单张图**，与 `["…"]` 完全等价 —— 此前这里报 400
    （"请写成数组"），把 OpenAI 风格客户端最常见的单图写法挡在门外。
    """
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "x",
                                "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 202, f"字符串形态应被当作单张图：{r.text}"


def test_image_string_still_passes_capability_shape_check(client):
    """⚠️ 放宽的是**形态**，不是**能力校验**。

    `jimeng-t2i`（文生图）不吃输入图 —— 传了非空字符串照样拒。
    那是"能力与形态不符"，与"写成标量还是数组"无关。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"], "要给出可执行的原因"


def test_image_of_other_types_is_still_rejected(client):
    """字符串/数组之外的形态仍明确 400 —— 别把写错的类型当"无图"吞掉。"""
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": 123}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert "image" in r.json()["error"]["message"]


def test_empty_image_defaults_to_t2i(client):
    """🔴 `image` 为空 / 未传 / `""` / `[]` 一律当"没有输入图"（= 文生图）。

    用户口径 2026-10-12：`image` 为空或没传**默认就是文生图** ——
    不该因为调用方传了个空串（或纯空白）就报 400。
    只有**非空字符串**才是真写错了数组形态（见上一个用例）。
    """
    for img in ("", "   ", [], None):
        payload: dict = {"model": "jimeng-t2i", "prompt": "x"}
        if img is not None:
            payload["image"] = img
        r = client.post(BASE, json=payload, headers=AUTH)
        assert r.status_code == 202, f"image={img!r} 应被当作文生图：{r.text}"

        # 空图时连 `model` 都可省（`resolve` 无歧义地推导成 t2i）。
        without_model = {k: v for k, v in payload.items() if k != "model"}
        r2 = client.post(BASE, json=without_model, headers=AUTH)
        assert r2.status_code == 202, \
            f"image={img!r} 省略 model 应推导为 t2i：{r2.text}"


def test_empty_material_ref_is_treated_as_absent():
    """`video` / `audio` 传空串 = 没传素材（与 `image` 同口径：空即缺省）。

    `create` 里 `has_materials = bool(body.get("video") or ...)` 早就把 `""`
    当 Falsy（无素材），校验层却报 400 —— 两处口径打架，这里对齐。
    非空字符串仍是"把数组写成了标量"的 400。
    """
    from app.errors import InvalidParameterError

    from app.service import Service

    for raw in ("", "   ", None, []):
        assert Service._validate_refs(raw, "video") == [], f"{raw!r}"
        assert Service._validate_refs(raw, "audio") == [], f"{raw!r}"

    with pytest.raises(InvalidParameterError) as e:
        Service._validate_refs("https://x/v.mp4", "video")
    assert e.value.param == "video", "非空字符串仍要明确 400 并指出字段"


def test_multiple_input_images_are_never_silently_truncated(client):
    """核心纪律不变：**绝不许**"收下 N 张、却只用 1 张"。

    契约按能力分（2026-09-20 起）：
      · `jimeng-i2i`（图生图）—— 上游草稿里 `image_uri_list` / `image_list`
        本来就是**列表**，所以**支持多张垫图**：全部上传、全部进草稿；
      · 后编辑三族（hd / pro-hd / outpaint）—— 上游用单个 `origin_image` 承载，
        多给必须**响亮 400**，不许静默丢；
      · 全局另有一道 10 张的合理性上限（挡住"一次塞几百个 URL"）。

    换句话说：**"支持"与"拒绝"必须是明确的两种行为，不允许有第三种（忽略）**。
    """
    # ① i2i：2 张是合法的（多张垫图）
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "改海边",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 202, r.text

    # ② 后编辑族：多给仍然明确报错
    r = client.post(BASE, json={"model": "jimeng-hd",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "最多接受 1 张" in msg
    assert "不会静默忽略" in msg, "报错要说清为什么拒绝"


def test_unknown_field_is_ignored_and_traced_instead_of_rejected(
        client, client_state):
    """🔴 2026-10-14（用户口径）：**不认识的字段忽略即可，不影响生图**。

    此前 `foo` / `output_format` 这类字段直接 400（"未知字段 […]"）——
    调用方拿到的是一条**与生成无关**的格式纠正，而不是一次生成。
    现在与 `KNOWN_UNSUPPORTED_FIELDS`（watermark 等）**同待遇**：
    进 `degradations`（看得见），但不阻塞。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "foo": 1},
                    headers=AUTH)
    assert r.status_code == 202, f"未知字段不该挡死生成：{r.text}"
    tid = r.json()["task_id"]
    got = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert got["status"] == "queued"
    assert any("foo" in d for d in got["degradations"]), \
        f"被忽略的字段要留痕，实得 {got.get('degradations')}"


def test_size_accepts_resolution_tier_shorthand(client):
    """🔴 2026-10-14（用户口径）：`size` 接受 `"2k"` 这类档位写法。

    此前一律 400（"无法解析 size='2k'"）。档位表达不了比例 ⇒ 按 **1:1** 展开
    （`2k` ⇒ `2048x2048`）；要别的比例请直接写像素。
    """
    for s in ("2k", "1k", "1.5k"):
        r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                    "size": s}, headers=AUTH)
        assert r.status_code == 202, f"size={s!r} 应被接受：{r.text}"


def test_known_but_unsupported_field_becomes_a_degradation(client, client_state):
    """**认得但做不到**的字段 ≠ 写错。前者进 `degradations`，后者 400。

    分不清这两者，调用方就会去查上游能力表，而真正的问题在请求体。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "watermark": True}, headers=AUTH)
    assert r.status_code == 202
    tid = r.json()["task_id"]
    got = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert got["status"] == "queued"
    assert any("watermark" in d for d in got["degradations"])


def test_capability_requiring_image_without_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-hd", "prompt": ""}, headers=AUTH)
    assert r.status_code == 400
    assert "需要输入图" in r.json()["error"]["message"]


def test_t2i_with_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "不接受输入图" in r.json()["error"]["message"]


def test_prompt_required_for_i2i(client):
    r = client.post(BASE, json={"model": "jimeng-i2i",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "需要 prompt" in r.json()["error"]["message"]


def test_bad_size_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "size": "huge"}, headers=AUTH)
    assert r.status_code == 400
    assert "2048x2048" in r.json()["error"]["message"]


def test_bad_n_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 0},
                    headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "n"


# ---------------------------------------------------------------------------
# 查询：状态与形状
# ---------------------------------------------------------------------------


def test_pending_task_returns_202_with_status(client):
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 202, "非终态必须是 202 —— 调用方据此继续轮询"
    assert r.json() == {"task_id": tid, "status": "queued"}


def test_success_body_matches_the_frozen_shape(client, client_state, fake_jimeng):
    """成功体 = `{status, data:[{url}], created, usage}`，且 `data[]` 里**只有 url**。

    2026-09-22 契约更新（用户指令）：成功态补顶层 `status: "success"` ——
    此前成功体只有 data/created/usage，是全链路唯一不带 status 的态，调用方
    只能按"有没有 data"推断终态。现五态（queued / in_progress / success /
    failure / canceled）**统一带顶层 status**。键集仍**精确相等**（防静默多键）。
    """
    fake_jimeng.states = [ok_state(["https://cdn/1.png", "https://cdn/2.png"],
                                   cost=44)]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 2},
                      headers=AUTH).json()["task_id"]

    client_state.coordinator.tick()   # 建任务
    client_state.coordinator.tick()   # 轮询到终态

    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "data", "created", "usage"}, \
        f"键集不符：{set(body)}"
    assert body["status"] == "success", "成功态必须带顶层 status=success"
    assert [d["url"] for d in body["data"]] == ["https://cdn/1.png",
                                                "https://cdn/2.png"]
    assert set(body["data"][0]) == {"url"}, "data[] 里只该有 url（与冻结契约逐字一致）"
    assert isinstance(body["created"], int)
    # 🔴 名字里带 forecast：它是上游的**预估**，不是实际扣费
    # （实测 i2i 报 55 / 实扣 12，高估 4~9 倍）
    assert body["usage"] == {"images": 2, "forecast_credits": 44}


def test_usage_omits_unknown_fields_instead_of_inventing_them(client,
                                                              client_state,
                                                              fake_jimeng):
    """上游没给 `forecast_generate_cost` 时，`credits` 键**不出现**。

    给个 0 更坏：那是在声称"本次消耗 0 积分"，而不是"不知道"。
    """
    st = ok_state(["https://cdn/1.png"])
    st.cost = None
    fake_jimeng.states = [st]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    usage = client.get(f"{BASE}/{tid}", headers=AUTH).json()["usage"]
    assert usage == {"images": 1}, "不知道的值不许编 —— 键不出现才对"
    assert "credits" not in usage and "forecast_credits" not in usage


def test_unknown_task_is_404_with_openai_error_envelope(client):
    r = client.get(f"{BASE}/jimeng_deadbeef", headers=AUTH)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "task_not_found"
    assert err["message"]


def test_cross_credential_read_is_404_and_touches_no_upstream(client, fake_jimeng):
    """跨凭证读取必须**本地拦死**，而不是丢给上游去判。

    放行到上游就是"用错的钥匙去查"，返回的 404/空无法区分
    "任务真没了"与"钥匙不对"，而且把跨凭证隔离交给了别人的实现去兜。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    before = len(fake_jimeng.calls)
    r = client.get(f"{BASE}/{tid}", headers=AUTH_B)
    assert r.status_code == 404
    assert len(fake_jimeng.calls) == before, "跨凭证读取不该产生任何上游请求"


def test_list_is_scoped_to_the_calling_key(client):
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "a"}, headers=AUTH)
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "b"}, headers=AUTH_B)
    a = client.get(BASE, headers=AUTH).json()
    assert a["total"] == 1, "列表只按服务过滤会把别人的任务列给你"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_non_terminal_task_fails_loudly(client):
    """即梦没有取消端点 ⇒ 对未终态任务的删除必须**响亮失败**。

    本地删掉只会让"还在跑并继续计费"变成看不见的事。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.delete(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 400
    assert "没有取消端点" in r.json()["error"]["message"]


def test_delete_terminal_task_removes_the_record(client, client_state, fake_jimeng):
    fake_jimeng.states = [ok_state(["https://cdn/1.png"])]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    assert client.delete(f"{BASE}/{tid}", headers=AUTH).json()["status"] == "deleted"
    assert client.get(f"{BASE}/{tid}", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_missing_key_is_401(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_missing_bearer_is_still_401(client):
    """🔴 **没有** Authorization 头依然 401（透传不改变这条）。

    2026-10-03 口径变更：Bearer 改成**即梦 sessionid 透传**、不再查白名单，
    所以"带一个不认识的 key"不再是 401（它会被当sessionid 收下，
    在**派发时**才因凭据无效而失败 —— 那是上游的判断，不是鉴权层的）。

    ⚠️ 代价（知情接受）：**任何持有即梦登录态的人都能调本服务**，
    计费与风控归他自己，我们的��务不代付。
    """
    assert client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}
                       ).status_code == 401
    assert client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                       headers={"Authorization": ""}
                       ).status_code == 401


def test_bare_bearer_scheme_without_token_is_401(client):
    """🔴 `Authorization: Bearer`（**只有 scheme、没有 token**）必须 401。

    回归：`_bearer` 的兜底分支 `return raw.strip()` 会把**字面量 `"Bearer"`**
    当成一个 sessionid 凭证收下 —— 空 token 不该变成"合法凭证"
    （`split(None, 1)` 会吃掉尾随空白，所以 `Bearer ` 也走同一条）。
    """
    for hdr in ("Bearer", "Bearer "):
        r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                        headers={"Authorization": hdr})
        assert r.status_code == 401, f"{hdr!r} 应 401：{r.text}"


def test_any_bearer_is_accepted_as_sessionid(client):
    """透传的**正向**语义：任何非空 Bearer 都能建任务（凭据有效性在上游校验）。"""
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers={"Authorization": "Bearer 522ceab845a313502b72f5067534d191"})
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# 模型清单 / 运维端点
# ---------------------------------------------------------------------------


def test_models_endpoint_lists_only_verified_capabilities(client):
    data = client.get("/v1/models", headers=AUTH).json()
    ids = {m["id"] for m in data["data"]}
    # 🔴 2026-09-24：视频族对外**一律方舟模型名**（内部名退为路由别名，
    # 只出现在条目的 internal_id 字段）；全能参考/补帧是请求形态不单列。
    # 🔴 2026-10-02 新增 `jimeng-audit`（素材预审）：**成功时零产物**是预期结果
    # （用户拍板的口径：判定结论在 degradations 里），已豁免"零产物=失败"。
    assert ids == {"jimeng-t2i", "jimeng-i2i", "jimeng-hd",
                   "jimeng-pro-hd", "jimeng-outpaint",
                   "jimeng-detail-fix", "jimeng-audit",
                   "doubao-seedance-2-0-mini-260615",
                   "doubao-seedance-2-0-fast-260128",
                   "doubao-seedance-2-0-260128",
                   "doubao-seedance-2-5-260628"}
    assert "detail" not in ids, "旧的工具名不对外"


#: `/v1` 前缀下**允许**的端点 —— 全部是**同步语义**（一次请求一个最终响应）。
#: 2026-09-23 从 `{"/v1/models"}` 扩到当前集合：新增了**同步生成**端点
#: （创建+轮询合并），它同样属于"同步、无 `202 + task_id` 轮询语义"的正牌成员。
#: **异步**族（受理/查询/删除）必须留在 `/async/v1`（护栏拦的是"异步语义挂 /v1"）。
#: 🔴 `/v1` 下的白名单 —— 只放**同步语义**端点（一问一答/立即返回）。
#: 2026-10-14 新增 `/v1/chat/completions`：它是**图片 chat**（借 chat 协议出图，
#: 返回前等到生成结果），同步语义 ⇒ 同族。
#: ⚠️ 与"连通性占位"时期不同：它**真的建任务并计费**（见对应用例）。
_V1_SYNC_ENDPOINTS = frozenset({"/v1/models", "/v1/images/generations",
                                "/v1/images/edits", "/v1/chat/completions"})


def test_models_endpoint_lives_only_under_v1(client):
    """模型清单**只在 `/v1/models`**；`/v1` 下只放**同步语义**端点（白名单钉死）。

    这条门禁是**双向**的：
      · `/v1/models` 必须在，且**免鉴权也 200**（2026-09-23 起刻意公开）；
      · `/async/v1/models` 必须**不存在** —— 否则有人"顺手加回来"就又成了双前缀，
        两边的文档/门禁/调用方认知会重新分叉。
    反向断言：`/v1` 下只允许白名单里的端点。生成/查询/删除的**异步**语义
    （`202 + task_id` + 轮询）挂到 `/v1` 会被误读成同步 OpenAI images/videos
    API —— `/async` 前缀就是那道护栏。而**同步生成端点**
    （`POST /v1/images/generations`，创建+轮询合并）本来就是同步语义，
    放 `/v1` 是正确的归属，不是护栏要拦的东西。
    """
    ok = client.get("/v1/models", headers=AUTH)
    assert ok.status_code == 200, ok.text
    # 🔓 2026-09-23 用户指令：免鉴权。不带任何凭据也必须 200，且内容与带 Key 一致 ——
    # 它是发现性端点，客户端要在配置 Key 之前先探"这服务有什么能力"。
    anon = client.get("/v1/models")
    assert anon.status_code == 200, \
        f"/v1/models 应免鉴权（发现性端点），实得 {anon.status_code}"
    assert anon.json() == ok.json(), "带不带 Key 拿到的清单必须一致"

    gone = client.get("/async/v1/models", headers=AUTH)
    assert gone.status_code == 404, \
        f"/async/v1/models 应当已取消，实得 {gone.status_code}"

    paths = {r.path for r in client.app.routes}
    assert "/v1/models" in paths
    assert "/async/v1/models" not in paths
    leaked = {p for p in paths if p.startswith("/v1/")} - _V1_SYNC_ENDPOINTS
    assert not leaked, \
        f"/v1 下只允许同步语义端点（白名单）：{sorted(leaked)}"


def test_chat_completions_generates_an_image_as_markdown(sync_client, fake_jimeng):
    """🔴 2026-10-14（用户口径）：chat 端点承载**图片 chat** —— 真出图，
    产物以 **Markdown 图片**回给调用方（客户端才能在对话流里直接渲染）。

    与旧行为**相反**：此前它是"只回说明、绝不建任务"的连通性占位；
    现在**真的建任务**（因此也真的计费）。这条用例同时钉住两个方向 ——
    既要有产物，也要确实建了任务。
    """
    before = sync_client.get(BASE, headers=AUTH).json()["total"]
    fake_jimeng.states = [ok_state(["https://cdn/cat.png"])]

    r = sync_client.post("/v1/chat/completions", json={
        "model": "doubao-seedream-5-0-pro-260628",
        "messages": [{"role": "user", "content": "画一只橘猫"}],
    }, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "doubao-seedream-5-0-pro-260628"
    content = body["choices"][0]["message"]["content"]
    assert "![image](https://cdn/cat.png)" in content, content

    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before + 1, "图片 chat 必须真的建任务（这是与旧占位的分水岭）"


def test_chat_completions_multimodal_image_becomes_an_i2i_input(
        sync_client, fake_jimeng):
    """多模态 messages 里的 `image_url` 转**垫图** ⇒ 图生图。

    `model` 缺省时也得有这个默认 —— chat 请求不带出图族的参数，
    缺省若撞上"多个能力可选"的 400，客户端就永远调不通。
    输入图用 data URI（**不出网**，见文件头 `PNG_DATA_URI`）。
    """
    fake_jimeng.states = [ok_state(["https://cdn/out.png"])]

    r = sync_client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "把它变成水彩画"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URI}},
        ]}],
    }, headers=AUTH)

    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"]
    assert "![image](https://cdn/out.png)" in content, content
    # 有垫图 ⇒ 走 i2i（blend）；文生图不会调 blend
    assert "blend" in fake_jimeng.kinds(), fake_jimeng.kinds()


def test_chat_completions_streams_sse_with_the_image(sync_client, fake_jimeng):
    """🔴 `stream: true` 必须回**标准 SSE** —— 这正是"chat 空回复"的修复点。

    客户端（new-api / LobeChat…）按行扫 `data: ` 前缀解析；给它整包 JSON
    会一个 chunk 都取不到，用户看到的就是"HTTP 200 但回复空白"。
    """
    fake_jimeng.states = [ok_state(["https://cdn/cat.png"])]

    r = sync_client.post("/v1/chat/completions", json={
        "model": "jimeng-t2i",
        "messages": [{"role": "user", "content": "画一只猫"}],
        "stream": True,
    }, headers=AUTH)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream"), r.headers
    assert r.text.rstrip().endswith("data: [DONE]"), r.text[-120:]

    chunks: list[dict] = []
    for line in r.text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            continue
        chunks.append(json.loads(payload))

    assert chunks, "SSE 里至少要有一个 data: 分片"
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    merged = "".join(
        (c.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
        for c in chunks)
    assert "![image](https://cdn/cat.png)" in merged, merged


def test_chat_completions_without_a_prompt_is_a_clear_400(sync_client):
    """空体/无 prompt ⇒ **400 且 `param` 指向 `prompt`**（不再无脑 200）。

    旧行为是"探针友好地回一段说明"；图片 chat 之后没有 prompt 根本没法出图，
    与其回一句废话，不如明确说清缺什么。⚠️ **零建任务** —— 400 发生在受理之前。
    """
    before = sync_client.get(BASE, headers=AUTH).json()["total"]

    r = sync_client.post("/v1/chat/completions", content=b"", headers=AUTH)

    assert r.status_code == 400, r.text
    assert r.json()["error"]["param"] == "prompt"
    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before, "参数校验失败不该建任务"


def test_chat_completions_raises_504_when_the_budget_runs_out(client_factory):
    """🔴 预算耗尽但仍在生成 ⇒ **504 `sync_timeout`**（2026-10-21 口径）。

    旧口径（2026-10-14）是 `202 + task_id + Location` 让调用方转异步；
    用户随后拍板"同步任务就别 202 了" —— 同步调用方拿 202 得到的是
    "既不是结果也不是错误"的东西，还得自己实现轮询。
    任务**仍然活着**（message 带 task_id），异步端点照常可轮询。
    """
    c = client_factory(sync_max_wait=0.05)
    st = c.app.state
    # advances=0 ⇒ wake() 不推进 ⇒ 任务停在 queued，预算一到就报超时
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    r = c.post("/v1/chat/completions",
               json={"model": "jimeng-t2i",
                     "messages": [{"role": "user", "content": "画一只猫"}]},
               headers=AUTH)

    assert r.status_code == 504, r.text
    assert "location" not in {k.lower() for k in r.headers}, dict(r.headers)
    err = r.json()["error"]
    assert err["code"] == "sync_timeout", err
    assert err["type"] == "timeout_error", err
    # 对外只给错误信息（2026-10-12 口径）：连 detail 都不出现。
    assert set(err) == {"message", "type", "code"}, err
    m = re.search(r"jimeng_[0-9a-f]{32}", err["message"])
    assert m, err

    # 拿到的必须是**真** task_id（能被异步端点查到），不是占位串
    got = c.get(f"/async/v1/images/generations/{m.group(0)}", headers=AUTH)
    assert got.status_code == 202, got.text
    assert got.json()["task_id"] == m.group(0)


# ---------------------------------------------------------------------------
# 兼容面：OpenAI 图片编辑端点 `POST /v1/images/edits`（multipart）
#
# 2026-10-14 新增。与 `/v1/images/generations` 共用 `_sync_generate`，
# 唯一区别是**入参形态**（文件上传 vs JSON 里的 URL）—— 所以这里重点钉
# "上传字节能不能变成一张正常的输入图"，以及三个端点级口径。
# ---------------------------------------------------------------------------

EDITS = "/v1/images/edits"


def _png_upload(name: str = "in.png") -> tuple[str, bytes, str]:
    """`(字段名之外的表单形态)` → TestClient 的 files 元素。"""
    return (name, PNG_BYTES, "image/png")


def test_edits_uploads_a_file_and_returns_the_image(sync_client, fake_jimeng):
    """🔴 上传的字节 → `data:` URI → 复用整条下游；产物照常回 `{status,data,…}`。"""
    before = sync_client.get(BASE, headers=AUTH).json()["total"]
    fake_jimeng.states = [ok_state(["https://cdn/edited.png"])]

    r = sync_client.post(EDITS, files=[("image", _png_upload())],
                         data={"prompt": "把它变成水彩画"}, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert [d["url"] for d in body["data"]] == ["https://cdn/edited.png"]
    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before + 1, "edits 应当真的建了一条任务"


def test_edits_accepts_the_image_bracket_field_name(sync_client, fake_jimeng):
    """`image[]` 这个字段名也认（OpenAI SDK 两种写法都有）。"""
    fake_jimeng.states = [ok_state(["https://cdn/b.png"])]

    r = sync_client.post(EDITS, files=[("image[]", _png_upload())],
                         data={"prompt": "x"}, headers=AUTH)

    assert r.status_code == 200, r.text


def test_edits_defaults_to_i2i_when_model_is_missing(sync_client, fake_jimeng):
    """未传 `model` ⇒ 默认 `jimeng-i2i`（图生图），并且**留痕**说明这个默认。

    chat 请求那种"不给默认就撞 400"的坑，在 edits 这里必须堵死。
    """
    fake_jimeng.states = [ok_state(["https://cdn/x.png"])]

    r = sync_client.post(EDITS, files=[("image", _png_upload())],
                         data={"prompt": "补个帽子"}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert "blend" in fake_jimeng.kinds(), \
        f"应走图生图（blend），实得 {fake_jimeng.kinds()}"
    deg = " ".join(r.json().get("degradations") or [])
    assert "jimeng-i2i" in deg, f"默认档必须留痕，实得 {deg!r}"


def test_edits_mask_is_accepted_but_ignored_with_a_note(sync_client, fake_jimeng):
    """`mask`（局部重绘遮罩）收下但**不生效** —— 必须响亮留痕。

    即梦这条链路没有遮罩重绘。静默忽略会让人以为"只有遮罩区在变"，
    而那正是**另一个**产品能力。
    """
    fake_jimeng.states = [ok_state(["https://cdn/y.png"])]

    r = sync_client.post(EDITS,
                         files=[("image", _png_upload()),
                                ("mask", ("m.png", PNG_BYTES, "image/png"))],
                         data={"prompt": "只改天空"}, headers=AUTH)

    assert r.status_code == 200, r.text
    deg = " ".join(r.json().get("degradations") or [])
    assert "mask" in deg and "忽略" in deg, f"mask 必须响亮留痕，实得 {deg!r}"


def test_edits_without_any_image_is_a_clear_400(sync_client):
    """没有输入图 ⇒ 400（`param="image"`），且**零建任务**。"""
    before = sync_client.get(BASE, headers=AUTH).json()["total"]

    r = sync_client.post(EDITS, data={"prompt": "x"}, headers=AUTH)

    assert r.status_code == 400, r.text
    assert r.json()["error"]["param"] == "image"
    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before, "校验失败不该建任务"


def test_edits_rejects_a_non_image_upload(sync_client):
    """上传非图片（按 magic bytes 判）⇒ 400，且**零建任务**。"""
    before = sync_client.get(BASE, headers=AUTH).json()["total"]

    r = sync_client.post(
        EDITS,
        files=[("image", ("a.txt", b"definitely not an image", "text/plain"))],
        data={"prompt": "x"}, headers=AUTH)

    assert r.status_code == 400, r.text
    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before


def test_edits_requires_a_bearer(client):
    """门禁口径统一：业务端点一律要 Bearer。"""
    r = client.post(EDITS, files=[("image", _png_upload())], data={"prompt": "x"})
    assert r.status_code == 401


def test_sync_wait_yields_the_loop_instead_of_blocking(service):
    """🔴 同步等待必须**让出事件循环**（`await asyncio.sleep`），不能死等。

    2026-10-14（用户追问"多个同步任务不会阻塞？"）：原先同步端点用
    `def` + `time.sleep` ⇒ FastAPI 把每个请求丢进**线程池**
    （Starlette/AnyIO 默认 **40** 个线程），而每个请求最长占线程
    `SYNC_MAX_WAIT`（300s）⇒ **40 个慢请求就把 chat / images / edits
    三个端点的同步请求一起堵死**。

    这条用例用"两个 0.4s 的等待并发跑"来钉住它：
      · 让出事件循环 ⇒ 总耗时 ≈ 0.4s；
      · 若是 `time.sleep` 式的死等 ⇒ ≈ 0.8s（用例会红）。

    ⚠️ 刻意用 `asyncio.run` 而不是 `@pytest.mark.asyncio`：
    `pytest-asyncio` **不在** `requirements-dev.txt` 里，而 `pytest.ini` 开了
    `--strict-markers` ⇒ 未注册的 marker 会让**收集阶段**直接报错
    （实测：本地装了那个包所以绿、CI 没装所以红）。不引新依赖更稳。
    """
    import asyncio
    import time as _time

    from app.main import _wait_terminal_async

    rec = service.create({"model": "jimeng-t2i", "prompt": "x"},
                         credential="cred-a", sessionid="sid-a")

    async def _race() -> None:
        await asyncio.gather(
            _wait_terminal_async(service, rec.task_id, "cred-a", 0.4),
            _wait_terminal_async(service, rec.task_id, "cred-a", 0.4),
        )

    t0 = _time.monotonic()
    asyncio.run(_race())
    elapsed = _time.monotonic() - t0

    assert elapsed < 0.7, (
        f"两个 0.4s 的等待应当**并发**完成（≈0.4s），实耗 {elapsed:.2f}s "
        f"⇒ 等待没让出事件循环（是不是又被改回 time.sleep / 同步 def 了？）")


def test_chat_completions_requires_a_bearer(client):
    """门禁口径统一：业务端点一律要 Bearer。"""
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401


#: FastAPI 自带的接口文档路由 —— 它们**不是**本服务的业务端点，
#: 天然没有业务鉴权（`/docs` 暴露整个 API 形态，属于**部署层**决定要不要关，
#: 见 README 的边界一节）。
_FRAMEWORK_ROUTES = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect",
                               "/redoc"})

#: 🔓 **刻意公开的业务端点**（不挂 `require_key`）—— 2026-09-23 用户指令：
#: `/v1/models` 是**发现性端点**（客户端在配置 Key 之前先探"这服务有什么能力"
#: 是常规做法），内容只有能力的公开描述、不含任何任务/凭据/内部状态。
#: ⚠️ 往这里加东西 = 对公网免鉴权，每一项都必须有一个说得出口的理由。
_PUBLIC_PATHS = frozenset({"/v1/models", "/", "/llms.txt"})   # 发现面：给人/给 LLM，刻意公开（2026-09-24）


def test_every_business_route_requires_a_bearer(client):
    """🔴 **所有业务端点（任何方法）都必须挂 `require_key`** —— 例外 = 探活 + 白名单。

    为什么要有这条**结构性**门禁（而不是只测几条具体路径）：靠人记着"新加路由
    时别忘了挂依赖"是记不住的。2026-09-23 收紧鉴权时就是这个原因暴露出来的 ——
    查单条的 GET 一直是"可选鉴权"（`task_id` 即凭据），`/v1/models` 与 `/stats`
    更是**完全开放**，而公网入口就是 80 端口。

    🔴 从"只看 GET"升级到**全方法**：新增的同步生成端点
    （`POST /v1/images/generations`）不在旧的扫描面里 —— 只盯 GET 的门禁，
    对 POST/DELETE/PUT 全是盲区，而它们才是会产生费用的写路径。
    判据只认路由级依赖表里有 `require_key`。

    两类例外：
      · **探活**：`/healthz`（容器 HEALTHCHECK）与 `/readyz`（编排层判活）
        必须在**没有任何凭据**时可用 —— 判活失败要说清是服务的问题，
        不能因为"没带 Key"而看起来像挂了；
      · **白名单**（`_PUBLIC_PATHS`）：`/v1/models` 是发现性端点，刻意公开
        （2026-09-23 用户指令）。
    """
    from app.observability import PROBE_PATHS

    assert set(PROBE_PATHS) == {"/healthz", "/readyz"}, \
        "探活路径表变了 —— 这里的不鉴权例外名单要跟着改"

    open_paths: set[str] = set()
    for route in client.app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", []) or ())
        if not path.startswith("/") or not methods:
            continue
        if path in PROBE_PATHS or path in _FRAMEWORK_ROUTES \
                or path in _PUBLIC_PATHS:
            continue
        dep = getattr(route, "dependant", None)
        if dep is None:
            # 非 FastAPI 业务路由（Starlette 原生 Route/Mount 等）—— 不适用本门禁；
            # 若真有人用它挂业务端点，契约用例（如 /v1/models）会先红。
            continue
        deps = {d.call.__name__ for d in dep.dependencies}
        if "require_key" not in deps:
            open_paths.add(path)

    assert not open_paths, \
        f"这些端点没挂鉴权（新加的？）：{sorted(open_paths)}"


def test_healthz_is_dependency_free_and_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_does_not_depend_on_env_sessionid(settings, monkeypatch):
    """🔴 2026-10-08 语义变更：**env 里没有 sessionid 也必须 ready**。

    透传后凭据由**每个请求的 Bearer** 带来，服务自己不需要持有。
    原先`/readyz` 判 `bool(JIMENG_SESSIONID or COOKIE)` ⇒ env 空就 503，
    而**Bearer 是对的**也没用—— 那会让"没配 env 的正确部署"直接不可用。

    ⚠️ 这条替代了原来的 `test_readyz_reports_upstream_configuration`
    （那个语义已随透传作废）。
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.service import Service

    blank = settings.replace(jimeng_cookie="")   # 无 sessionid 配置项了
    monkeypatch.setattr(main_mod, "Service", lambda s: Service(s))
    app = main_mod.create_app(blank)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200, (
            "env 无 sessionid 不该让服务报 not-ready")
        # 受理也不该被"服务没配 env"拦住
        r = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                   headers=AUTH)
        assert r.status_code == 202, r.text



def test_missing_credential_is_401_not_503(client_factory):
    """🔴 缺凭据是**调用方的身份问题** ⇒ 401（不是部署问题 503）。

    2026-10-08 语义变更：原先"env 没配 sessionid"⇒ 503；
    透传后**服务不持有凭据**，所以"缺凭据"只可能是**请求没带 Bearer** ⇒ 401。
    ⚠️ 这条替代了 `test_unconfigured_upstream_is_503_not_401`（语义已作废）。
    """
    c = client_factory()
    r = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] in ("missing_api_key", "invalid_api_key",
                                         "authentication_error")



# ---------------------------------------------------------------------------
# 同步生成：POST /v1/images/generations（创建+轮询合并）
# ---------------------------------------------------------------------------

SYNC = "/v1/images/generations"


class _InlineCoordinator:
    """把协调器换成"`wake()` 时就地推进"的替身 —— 同步接口用例专用。

    为什么需要它：同步端点在**一个请求内**等到终态，而测试纪律是
    "协调器不启线程、用例显式 tick"（见 conftest 纪律 #2）。这个替身把
    两者接起来：请求里调 `wake()` 时直接跑 N 轮 `tick()`，等待循环的第一轮
    查库就能看到终态 —— 确定性最高，不引入后台线程的时序不确定性。
    """

    def __init__(self, real: Any, *, advances: int = 3,
                 running: bool = True) -> None:
        self.real = real
        self.advances = advances
        self._running = running
        self.wakes = 0

    @property
    def running(self) -> bool:
        return self._running

    def wake(self) -> None:
        self.wakes += 1
        for _ in range(self.advances):
            self.real.tick()

    def stats(self) -> dict:
        return self.real.stats()

    def __getattr__(self, name: str) -> Any:
        # 其余接口（stop/start/…）转发给真协调器 —— 用例只替换
        # running/wake/stats 三个，lifespan 退出时的 stop() 也能照常工作。
        return getattr(self.real, name)


@pytest.fixture
def sync_client(client, client_state):
    """默认 app 的"同步版"：协调器换成 inline 替身（wake 即推进）。"""
    client_state.coordinator = _InlineCoordinator(client_state.coordinator)
    return client


def test_sync_single_call_returns_final_result(sync_client, client_state,
                                               fake_jimeng):
    """一次 POST 拿到最终结果 —— 这就是"创建+轮询合并"的全部意义。"""
    fake_jimeng.states = [ok_state(["https://cdn/a.png", "https://cdn/b.png"])]
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x",
                                     "n": 2}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # 与异步查询成功体**逐键一致**：同一个 `view()` 出口，不许长出第二套形状
    assert set(body) == {"status", "data", "created", "usage"}
    assert body["status"] == "success"
    assert [d["url"] for d in body["data"]] == ["https://cdn/a.png",
                                                "https://cdn/b.png"]
    assert client_state.coordinator.wakes == 1, "受理后应叫醒协调器"


def test_sync_failure_is_a_200_with_failure_status(sync_client, fake_jimeng):
    """普通失败（风控/限流/额度/上游故障…）仍是"完整的答案"：
    直接回 `failure` 体，调用方无需再轮询。

    🔴 边界（2026-10-10 用户口径）：**空图**与**内容审核**两种失败
    不走这条路 —— 空图直接抛 **500**、审核抛 **451**（见下面几个用例）。
    """
    fake_jimeng.states = [failed_state("generate_failed")]
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failure"
    assert r.json()["error"]


def test_sync_empty_result_raises_500(sync_client, fake_jimeng):
    """🔴 空图（终态成功却零产物）⇒ 同步接口**直接抛 500**。

    用户口径 2026-10-10：调用方拿一个 200 + `data: []` 的"成功体"
    等于被骗 —— 同步是一锤子买卖，没有东西就说没有，用状态码说。
    🔴 2026-10-21 口径（**覆盖** 2026-10-12 的 451）：直接 **500** ——
    451 会被一部分网关归到"内容合规"、甚至渲染成空响应 `{}`，
    调用方照样读成"拿到了东西"。500 才是不会被误读的服务端失败。
    判据链：零产物落库为 `empty_result_error` ⇒ 同步端点按 type 转抛。
    """
    from app.upstream.jimeng.client import TaskState

    empty = TaskState(submit_id="upstream-submit-id", status=50,
                      status_name="success", finished=True, failed=False,
                      total=4, finished_count=0)
    empty.images = []  # 不构造任何 GeneratedImage —— 就是零产物
    fake_jimeng.states = [submitted_state(), empty]

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)
    assert r.status_code == 500, r.text
    err = r.json()["error"]
    assert err["type"] == "empty_result_error"
    assert err["code"] == "upstream_empty_result"
    assert "零产物" in err["message"]

    # 🔴 2026-10-12 口径（用户）：抛错信息里**只带错误信息** ——
    # 对客户端不暴露模型/能力标识、上游状态码与计数、上游 id。
    # ⇒ 错误体**只有** message/type/code 三个键，连 `detail` 都不出现。
    assert set(err) == {"message", "type", "code"}, err
    assert "零产物" in err["message"]
    for leaked in ("jimeng-t2i", "jimeng:", "status=", "submit_id",
                   "capability", "model", "total"):
        assert leaked not in err["message"], f"message 不该出现 {leaked!r}"


def test_sync_zero_product_success_raises_500(sync_client, fake_jimeng):
    """🔴 终态 **success 却零产物** ⇒ 同步接口**直接抛 500**（2026-10-15 口径：
    "同步接口返回空图请报错"；2026-10-21 从 451 收紧为 500 ——
    "不要返回空图或者 `{}`，这个直接报 500"）。

    ⚠️ 与上面那条 `test_sync_empty_result_raises_500` 判据**不同**：那条是
    `_advance` 把"零产物"判成了**失败**（落库 `error.type`），这条是任务**被判成功**、
    只是交付张数为 0 —— 所以闸口必须落在**交付张数**上。

    场景取当前**唯一**已知的真实来源：`jimeng-audit`（素材预审，成功时本就没有产物，
    任务级刻意豁免"零产物=失败"）。用户口径：**一律** —— 同步接口不交付空的成功。

    同时钉住**反面**：异步端点不受影响（`data: []` + `degradations` 里的判定
    仍是预审类能力的正规出口）—— 这条闸只加在同步链上。
    """
    # 预审走的是"受理即终态"（`Service._run_audit`），不需要派发/轮询剧本；
    # 素材用 data URI（测试零网络纪律，见文件顶部）。
    r = sync_client.post(SYNC, json={"model": "jimeng-audit",
                                     "image": [PNG_DATA_URI]}, headers=AUTH)
    assert r.status_code == 500, r.text
    err = r.json()["error"]
    assert err["type"] == "empty_result_error", err
    assert err["code"] == "upstream_empty_result", err
    assert "零产物" in err["message"], err
    # 对外**只给错误信息**（2026-10-12 口径）：连 `detail` 都不出现。
    assert set(err) == {"message", "type", "code"}, err
    # 上游/内部标识一律不外泄（能力 key、模型名、上游 id、状态码）。
    for leaked in ("jimeng-audit", "jimeng:", "audit", "skill",
                   "status=", "capability", "model"):
        assert leaked not in err["message"], f"message 不该出现 {leaked!r}"

    # 🔴 闸口**不是**把任务判失败：库里它仍是 success + 零产物，
    # 且 message 里给了"去哪看完整记录" ⇒ 调用方能无缝转异步。
    tid = re.search(r"jimeng_[0-9a-f]{32}", err["message"])
    assert tid, f"message 必须带上可继续查询的 task_id：{err['message']!r}"
    got = sync_client.get(f"{BASE}/{tid.group(0)}", headers=AUTH)
    assert got.status_code == 200, got.text
    rec = got.json()
    assert rec["status"] == "success", rec
    assert rec["data"] == [], rec          # 异步侧**仍是** data:[]（契约不变）
    assert any("素材预审" in d for d in rec.get("degradations", [])), rec


def test_sync_empty_body_raises_500(sync_client, fake_jimeng, monkeypatch):
    """🔴 200 体是空对象 `{}` ⇒ 同步接口**必须报 500**（2026-10-21 用户口径）。

    这条刻意绕过语义层（产物是有的），只把 `view()` 的返回值换成一个空
    dict —— 钉住 `_sync_view()` 的**形状守卫**：无论将来哪条路径写出
    "200 什么都没带"的响应，同步调用方都不该把它读成成功。
    """
    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    monkeypatch.setattr("app.main.view", lambda rec, **kw: (200, {}))

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)
    assert r.status_code == 500, r.text
    err = r.json()["error"]
    assert err["type"] == "empty_result_error", err
    assert set(err) == {"message", "type", "code"}, err


def test_sync_content_policy_raises_451(sync_client, fake_jimeng):
    """🔴 内容审核拒绝 ⇒ 同步接口**直接抛 451**。

    用户口径 2026-10-10：审核失败原样重试必再拒（还可能每次都计费），
    200 + failure 体骗不出这个性质 —— 451 让调用方一眼分流。
    场景用 2026-10-02 实锤的形态：`fail_code` 为空、真因只在
    `fail_starling_key`（版权拦截）。
    """
    from app.upstream.jimeng.client import TaskState

    blocked = TaskState(submit_id="upstream-submit-id", status=30,
                        status_name="generate_failed", finished=True,
                        failed=True, fail_code=None,
                        fail_key="web_fail2generate_copyright_block")
    fake_jimeng.states = [submitted_state(), blocked]

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)
    assert r.status_code == 451, r.text
    err = r.json()["error"]
    assert err["type"] == "content_policy_violation"
    assert err["message"]
    # 🔴 2026-10-12 口径：错误体**只有** message/type/code；message 里只留人话，
    # 不拼上游状态码 / `fail_code` / `fail_key`（内部枚举不进响应体）。
    assert set(err) == {"message", "type", "code"}, err
    for leaked in ("status=", "fail_code", "fail_key", "status_name",
                   "web_fail2generate_copyright_block"):
        assert leaked not in err["message"], f"message 不该出现 {leaked!r}"


def test_sync_timeout_raises_504(client_factory):
    """预算耗尽 ⇒ **504 `sync_timeout`**；任务不受影响，异步仍可轮询。

    2026-10-21 用户口径："同步任务就别 202 了" —— 旧行为（202 + Location
    转异步）作废：同步接口要么给结果、要么给错误，不交付"半截子"。
    任务不会被取消（即梦根本没有取消端点），只是这个 HTTP 请求不再等它；
    message 里带 task_id，调用方要结果就自己去异步端点接着查。
    """
    import time as _time

    c = client_factory(sync_max_wait=0.05)
    st = c.app.state
    # advances=0 ⇒ wake() 不推进 ⇒ 任务停在 queued，预算一到就报超时
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    t0 = _time.monotonic()
    r = c.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    elapsed = _time.monotonic() - t0

    assert r.status_code == 504, r.text
    assert "location" not in {k.lower() for k in r.headers}, dict(r.headers)
    err = r.json()["error"]
    assert err["code"] == "sync_timeout", err
    assert err["type"] == "timeout_error", err
    # 对外只给错误信息：message / type / code，**无 detail**。
    assert set(err) == {"message", "type", "code"}, err
    m = re.search(r"jimeng_[0-9a-f]{32}", err["message"])
    assert m, err
    # 🔴 防"配置没生效、真等 300s"把用例挂死：耗时必须远小于默认预算
    assert elapsed < 5, f"超预算应当及时报错，实耗 {elapsed:.1f}s"

    # 报错之后任务还在（没被取消/删除），异步端点照常可轮询
    got = c.get(f"/async/v1/images/generations/{m.group(0)}", headers=AUTH)
    assert got.status_code == 202
    assert got.json()["status"] == "queued"


def test_sync_fast_fails_when_backlog_exceeds_budget(sync_client, monkeypatch):
    """🔴 队列最老等待 ≥ 同步预算 ⇒ **429 快速失败**，且**零建任务**。

    2026-10-21 用户口径："队列最老等待 > 预算就直接 503/429，不让它白等 5 分钟"。
    没有这道闸时：先建任务（真提交上游、真计费）→ 干等满 300s → 504，
    而图多半已经生成好却没人来取（白花钱），队列还被排得更长。
    """
    monkeypatch.setattr("app.service.Service.queue_oldest_wait_s",
                        lambda self, *, within_s=None, priority_only=False: 999)
    before = sync_client.get(BASE, headers=AUTH).json()["total"]

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)

    assert r.status_code == 429, r.text
    err = r.json()["error"]
    assert err["code"] == "queue_overloaded", err
    assert err["type"] == "rate_limit_error", err
    # 粗估的等待要给出来（别编精确数字），且 message 指向异步端点。
    assert err["retry_after"] >= 1, err
    assert "异步端点" in err["message"], err

    after = sync_client.get(BASE, headers=AUTH).json()["total"]
    assert after == before, "被拒的请求不该建任务（零计费、零排队）"


def test_sync_backlog_guard_does_not_block_audit(sync_client, monkeypatch):
    """🔴 积压闸**不拦"不排队"的能力** —— `jimeng-audit` 是受理即终态。

    线上实测（2026-10-21 16:05）：队列积压 849 条、最老等了 5 天。这种时候
    把预审请求也 429 掉就是**纯误伤** —— 它不建生成任务、不占并发槽，
    队列再堵它也能立刻出结果。
    """
    monkeypatch.setattr("app.service.Service.queue_oldest_wait_s",
                        lambda self, *, within_s=None, priority_only=False: 9999)

    r = sync_client.post(SYNC, json={"model": "jimeng-audit",
                                     "image": [PNG_DATA_URI]}, headers=AUTH)

    # 空产物照旧 500（不是 429）—— 说明它根本没走队列闸。
    assert r.status_code == 500, r.text
    assert r.json()["error"]["code"] == "upstream_empty_result", r.text


def test_sync_backlog_guard_can_be_switched_off(client_factory, monkeypatch):
    """闸关掉之后必须回到"照常建任务、等满预算再 504"的老路径。

    留这个开关是为了**排障**：若线上误判（例如队列指标被僵尸污染），
    运维能立刻关掉它，而不是回滚整个版本。
    """
    c = client_factory(sync_max_wait=0.05, sync_backlog_guard=False)
    st = c.app.state
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)
    monkeypatch.setattr("app.service.Service.queue_oldest_wait_s",
                        lambda self, *, within_s=None, priority_only=False: 9999)

    r = c.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)

    assert r.status_code == 504, r.text
    assert r.json()["error"]["code"] == "sync_timeout", r.text


def test_audit_is_not_blocked_by_the_queue_cap(client_factory):
    """🔴 单凭据排队背压**不拦"不排队"的能力**（audit）。

    线上实测（2026-10-21 16:30）：开了 `MAX_QUEUED_PER_CREDENTIAL=100` 之后，
    该凭据连 `jimeng-audit` 都回 429 —— 可它受理即终态、不占并发槽，
    队列再堵也能立刻出结果。拦它属于连坐（预审本来是"提交前省钱"的第一道闸）。
    """
    c = client_factory(max_queued_per_credential=1)
    st = c.app.state
    # advances=0 ⇒ 任务留在 queued，队列不消化
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    assert c.post(BASE, json={"model": "jimeng-t2i", "prompt": "a"},
                  headers=AUTH).status_code == 202
    # 第 2 条撞上限 ⇒ 429（背压生效）
    r2 = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "b"}, headers=AUTH)
    assert r2.status_code == 429, r2.text
    assert r2.json()["error"]["code"] == "queue_overloaded", r2.text

    # 但 audit 必须放行（异步受理 → 202，受理即终态）
    r3 = c.post(BASE, json={"model": "jimeng-audit",
                            "image": [PNG_DATA_URI]}, headers=AUTH)
    assert r3.status_code == 202, r3.text


def test_content_dedupe_reuses_when_image_bytes_match(client_factory, monkeypatch):
    """🔴 同一张图**换了 URL** 也必须认出是同一个请求（2026-10-21 线上）。

    现场：调用方每次重试都把图**重新上传**到 S3（`tmpXXXX.jpg?AWSAccessKey…`
    每次都变）⇒ 指纹含 URL 原文 ⇒ 去重永不命中 ⇒ 同一个「2k 生成 + 超清」
    被受理 N 次，页面上出现多条相同的 4K 结果，而且每次都计费。

    修法：指纹不命中时，用**不含图**的弱指纹找近邻，再比对图片**内容**。
    """
    c = client_factory()
    st = c.app.state
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    body = {"model": "jimeng-i2i", "prompt": "同一张脸",
            "size": "2048x2048"}
    r1 = c.post(BASE, json={**body, "image": ["https://s3/x/a.jpg?sig=1"]},
                headers=AUTH)
    assert r1.status_code == 202, r1.text
    tid = r1.json()["task_id"]

    # 内容摘要相同（模拟"同一张图重新上传、URL 变了"）
    monkeypatch.setattr("app.service.Service._image_digest",
                        lambda self, ref: "same-bytes")
    r2 = c.post(BASE, json={**body, "image": ["https://s3/x/b.jpg?sig=2"]},
                headers=AUTH)
    assert r2.status_code == 202, r2.text
    assert r2.json()["task_id"] == tid, "同一内容换了 URL ⇒ 应复用，不该新建"


def test_content_dedupe_keeps_different_images_apart(client_factory, monkeypatch):
    """换了一张**不同**的图 ⇒ 必须新建（不能因为 prompt 一样就复用）。"""
    c = client_factory()
    st = c.app.state
    st.coordinator = _InlineCoordinator(st.coordinator, advances=0)

    body = {"model": "jimeng-i2i", "prompt": "同一张脸", "size": "2048x2048"}
    r1 = c.post(BASE, json={**body, "image": ["https://s3/x/a.jpg"]},
                headers=AUTH)
    assert r1.status_code == 202, r1.text
    tid = r1.json()["task_id"]

    # 摘要不同（内容真的换了）⇒ 必须新建
    monkeypatch.setattr("app.service.Service._image_digest",
                        lambda self, ref: ref)
    r2 = c.post(BASE, json={**body, "image": ["https://s3/x/other.jpg"]},
                headers=AUTH)
    assert r2.status_code == 202, r2.text
    assert r2.json()["task_id"] != tid, "图换了就不该复用"


def test_priority_queue_puts_sync_tasks_first(store):
    """🔴 同步请求（priority=1）必须排在异步积压**前面**。

    2026-10-21 用户口径："同步任务不做 queued 免得积压"。
    线上实测：849 条异步积压、最老等 5 天 —— 同步请求若跟它们一起 FIFO，
    每个都必然等满预算。这里钉住派发顺序的唯一依据：
    `list_by_status(order="priority")` 先按 priority 降序、再按 FIFO。
    """
    import time as _time

    from sqlmodel import Session

    from app.store import TaskRecord

    now = int(_time.time())

    def _mk(task_id: str, *, created_at: int, priority: int) -> TaskRecord:
        return TaskRecord(task_id=task_id, credential_id="c1",
                          model="jimeng-t2i", cap_key="jimeng:t2i",
                          status="queued", priority=priority,
                          created_at=created_at, updated_at=created_at)

    with Session(store.engine) as s:
        # 异步的那条**更老**（FIFO 本该它先），同步的刚进来 —— 同步仍要排第一。
        s.add(_mk("async-old", created_at=now - 3600, priority=0))
        s.add(_mk("sync-new", created_at=now, priority=1))
        s.commit()

    order = [r.task_id for r in store.list_by_status("queued",
                                                     order="priority")]
    assert order == ["sync-new", "async-old"], order


def test_backlog_wait_ignores_async_backlog(store):
    """🔴 积压判据只看**同步自己的**排队（`priority_only=True`）。

    异步灌 800 条不等于同步会等 800 条 —— 它能插队。若判据看全局，
    就会把"本来能立刻跑"的同步请求全部 429 掉（这是最坏的误伤：
    越忙越拒，等于自断服务）。
    """
    import time as _time

    from sqlmodel import Session

    from app.store import TaskRecord

    now = int(_time.time())
    with Session(store.engine) as s:
        s.add(TaskRecord(task_id="async-1", credential_id="c1",
                         model="jimeng-t2i", cap_key="jimeng:t2i",
                         status="queued", priority=0,
                         created_at=now - 900, updated_at=now - 900))
        s.commit()

    # 全局：异步那条等了 15 分钟。
    assert store.queue_oldest_wait_s(within_s=3600) >= 890
    # 只看同步：没有同步在排队 ⇒ 0，闸不会误开。
    assert store.queue_oldest_wait_s(within_s=3600, priority_only=True) == 0


def test_queue_oldest_wait_window_excludes_zombies(store):
    """🔴 时间窗必须把"从不派发"的僵尸 queued 排除在外。

    现场：库里真出现过 `queued` 且 `upstream_submit_id` 为空、永远派不出去的
    任务（本地生产库就躺着一条 10-04 的）。它的"等待时间"会无限涨 ——
    判据若不设窗口，一条僵尸就能让**整个同步面永久 429**。
    取舍写死：宁可漏判（偶发 504），不能误判（全站不可用）。
    """
    import time as _time

    from sqlmodel import Session

    from app.store import TaskRecord

    old = int(_time.time()) - 86400
    with Session(store.engine) as s:
        s.add(TaskRecord(task_id="zombie-queued-1", credential_id="c1",
                         model="jimeng-t2i", cap_key="jimeng:t2i",
                         status="queued", created_at=old, updated_at=old))
        s.commit()

    # 不带窗口：它就是最老那条（≈1 天）—— 这正是会毒化同步面的值。
    assert store.queue_oldest_wait_s() >= 86000
    # 带窗口（同步链实际用的形态）：僵尸被排除 ⇒ 0，闸不会误开。
    assert store.queue_oldest_wait_s(within_s=3600) == 0


def test_sync_503_without_a_running_coordinator(client):
    """没有推进者 ⇒ **快速 503**，而不是白等一整个预算。

    夹具默认 `COORDINATOR_ENABLED=0` ⇒ 协调器线程没起 ⇒ running=False。
    这个部署形态下任务根本不会被推进，同步等待注定熬到超时 —— 快速说清。
    同时断言：**没有落下任何任务**（503 发生在受理之前）。
    """
    before = client.get(BASE, headers=AUTH).json()["total"]
    r = client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers=AUTH)
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["code"] == "sync_unavailable"
    assert "协调器" in err["message"]
    after = client.get(BASE, headers=AUTH).json()["total"]
    assert after == before, "503 发生在受理之前，不该留下孤儿任务"


def test_sync_requires_bearer(sync_client):
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401


def test_sync_validates_like_the_async_endpoint(sync_client):
    """参数校验复用同一套（同一个 `Service.create`）—— 报错逐字一致。

    ⚠️ 2026-10-14 起 `image` 传字符串**不再是错误**（= 单张图），
    所以换一个**两端口径都必须拒**的请求来钉"同一套校验"：
    文生图（t2i）却带了输入图。
    """
    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x",
                                     "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"], "报错必须带可执行原因"


_ = (pytest, KEY_B)


def test_upscale_begin_is_idempotent(service, monkeypatch):
    """🔴 同一条任务**不能被启动两次超清队列**（2026-10-21 用户现场）。

    现场：一个 4K 请求（= 2k 生成 + 一次超分）在即梦上留下了 **1 张 2K + 2 张
    4K** —— 同一张 2k 被超分了两次。`_upscale_begin` 一进来就重置
    `i / tries / out`，所以第二次 begin 会**在原图上再提交一条超清腿**，
    而且账本清零后 `tries` 又从 1 开始（看起来只有一次，掩盖了重复）。
    """
    from app.store import TaskRecord

    rec = TaskRecord(
        task_id="t-idem", credential_id="c1", model="jimeng-t2i",
        cap_key="jimeng:t2i", status="in_progress",
        extra_json=json.dumps({"upscale": {"stage": "upscale", "i": 0,
                                           "tries": {"1:0": 1}}}))
    submitted: list[Any] = []
    monkeypatch.setattr(service, "_submit_upscale_leg",
                        lambda *a, **k: (submitted.append(1), (True, "sid", False))[1])

    plan = {"tier": "4k", "levels": 1, "src": [{"url": "u"}], "i": 0}
    service._upscale_begin(rec, None, [{"url": "u"}], plan, [])

    assert submitted == [], "计划已是 upscale ⇒ 不许再启动一轮（会重复超分）"


def test_upscale_pump_hard_caps_submissions(service, monkeypatch):
    """同一张的超清提交次数有**硬上限**（`预算 + 1`），任何路径都不许绕过。

    软判据（`_upscale_settle` 里的 tries <= budget）依赖账本没被重置；
    这条是独立兜底：账本说"已经试了 99 次"时，连提交都不发。
    """
    from app.store import TaskRecord

    rec = TaskRecord(task_id="t-cap", credential_id="c1", model="jimeng-t2i",
                     cap_key="jimeng:t2i", status="in_progress")
    submitted: list[Any] = []
    monkeypatch.setattr(service, "_submit_upscale_leg",
                        lambda *a, **k: (submitted.append(1), (True, "sid", False))[1])
    monkeypatch.setattr(service, "_upscale_finish", lambda *a, **k: None)

    plan = {"src": [{"url": "u"}], "i": 0, "tries": {"1:0": 99}, "notes": [],
            "out": [], "legs_failed": 0, "level": 1}
    service._upscale_pump(rec, plan)

    assert submitted == [], "超过硬上限就不该再提交"
    assert plan["i"] == 1 and plan["legs_failed"] == 1
