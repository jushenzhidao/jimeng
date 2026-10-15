#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4k 请求改道「2k 生成 + 超清就地放大」（2026-10-12）。

**用户口径**：请求清晰度 **> 2k** 的**图片生成**任务（t2i / i2i）一律改走
「2k 生成 + 超清就地放大」以降低消耗；超分腿失败则**交付 2k 原图 + 响亮留痕**
（不判任务失败）；非 1:1 按**输入比例就地放大**交付（尺寸 = 2k 槽的 2×）。

**重试**（2026-10-12 收尾加）：超清腿是**免费**的，且它是"请求 >2k"的**唯一**
清晰度来源 ⇒ 遇到**瞬时**失败（提交抛异常 / 上游终态无产物）先按
`UPSCALE_MAX_RETRIES`（默认 **1**）重投同一张，**用尽才退回 2k**。
**确定性**失败（缺 `item_id`、id 非数字）不重试 —— 重试必然同样失败。

实测依据见 `docs/UPSTREAM.md` §21：
· 超清的**引用形态**成立（`item_id` + `origin_history_id`，**不带** `origin_image`）；
· 超清 = **固定 2×**（源 2560×1440 → 5120×2880），**不吸附**到 4k 槽；
· 4k 是**独立计费档**（直出更贵，非会员账号上余额 17 时连提交都被 1006 拒）；
· 超清**免费**。

⚠️ 这些用例**一条真实上游请求都不发**（全走 `FakeJimeng`）。
"""
from __future__ import annotations

import pytest

from app.service import Service
from app.upstream.jimeng import GeneratedImage, TaskState
from app.upstream.jimeng.capabilities import slot_for_ratio_in
from tests.conftest import (
    AUTH,
    FakeConfigCache,
    drive,
    failed_state,
    submitted_state,
)

BASE = "/async/v1/images/generations"

#: Seedream 5.0 Lite 服务端**真实** `resolution_map`（`docs/UPSTREAM.md` §11 实测值）。
#: 🔴 关键点：**4k 不是 2k 的整数倍** —— 16:9 是 2560×1440 → 5404×3040（2.11×）。
#: 所以"超清 2× 能不能顶上 4k 档"只在 1:1 上恰好成立。
V50_RMAP: dict = {
    "2k": {"image_ratio_sizes": [
        {"ratio_type": 1, "width": 2048, "height": 2048},   # 1:1
        {"ratio_type": 2, "width": 1728, "height": 2304},   # 3:4  竖
        {"ratio_type": 3, "width": 2560, "height": 1440},   # 16:9 横
        {"ratio_type": 4, "width": 2304, "height": 1728},   # 4:3  横
        {"ratio_type": 5, "width": 1440, "height": 2560},   # 9:16 竖
        {"ratio_type": 6, "width": 1664, "height": 2496},   # 2:3  竖
        {"ratio_type": 7, "width": 2496, "height": 1664},   # 3:2  横
        {"ratio_type": 8, "width": 3024, "height": 1296},   # 21:9
    ]},
    "4k": {"image_ratio_sizes": [
        {"ratio_type": 1, "width": 4096, "height": 4096},
        {"ratio_type": 2, "width": 3520, "height": 4693},
        {"ratio_type": 3, "width": 5404, "height": 3040},
        {"ratio_type": 4, "width": 4693, "height": 3520},
        {"ratio_type": 5, "width": 3040, "height": 5404},
        {"ratio_type": 6, "width": 3328, "height": 4992},
        {"ratio_type": 7, "width": 4992, "height": 3328},
        {"ratio_type": 8, "width": 6197, "height": 2656},
    ]},
}

#: 即梦 `history_record_id` 是**数字**（`origin_history_id` 直接照传）。
HIST = "44853933660428"


@pytest.fixture
def service(settings, store, fake_jimeng, fake_uploader, fake_vod):
    """本模块专用：能力表**带真实的 v50 分辨率表** ⇒ 4k 改道才会生效。

    其余用例（conftest 的 `service`）拿到 `rmap=None`（= 能力表读不到），
    改道**惰性不生效** ⇒ 那些用例的行为与本特性上线前**完全一致**
    （这是刻意的：不为了一个降本特性去扰动既有的 443 条断言）。
    """
    return Service(settings, store=store, client=fake_jimeng,
                   uploader=fake_uploader, vod=fake_vod,
                   cfg=FakeConfigCache(rmap=V50_RMAP))


def _create(client, **body):
    body.setdefault("model", "jimeng-t2i")
    body.setdefault("prompt", "飞上天")
    r = client.post(BASE, json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def _gen_ok(urls: list[str], *, hist: str = HIST, items=(777,),
            size: tuple[int, int] = (2048, 2048)) -> TaskState:
    """一次**成功的生成腿**：带 history id 与产物 item_id（超清引用形态的原料）。"""
    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False, cost=44)
    st.history_record_id = hist
    st.images = [
        GeneratedImage(url=u, width=size[0], height=size[1], format="png",
                       item_id=(items[i] if i < len(items) else None))
        for i, u in enumerate(urls)]
    return st


def _upscale_ok(url: str, *, size: tuple[int, int] = (4096, 4096)) -> TaskState:
    """一次**成功的超清腿**（产物尺寸 = 源的 2×）。"""
    st = TaskState(submit_id="upstream-submit-id", status=50,
                   status_name="success", finished=True, failed=False, cost=9)
    st.history_record_id = HIST
    st.images = [GeneratedImage(url=url, width=size[0], height=size[1],
                                format="png", item_id=888)]
    return st


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------


def test_4k_size_is_rerouted_to_2k_then_upscaled(
        client, client_state, fake_jimeng, service):
    """4k 尺寸 → 落到**同比例的 2k 槽** → 引用形态超清 → 交付放大图。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        _upscale_ok("https://cdn/up.png"),
    ]
    tid = _create(client, size="4096x4096")

    rec = service.store.get(tid)
    assert rec.size == "2048x2048", "4k 请求必须落到**同比例的 2k 槽**"
    assert rec.resolution_tier == "2k", "落库/提交/负缓存键都用**实际**档位"
    note = " ".join(rec.degradations)
    assert "已自动改走" in note, "改道必须在 degradations 里可见（降级必须可见）"
    assert "image_basic_v5_4k" not in note, \
        "已改道就不该再照旧吓唬调用方'会被按 4k 额外扣费'"

    drive(client_state, 1)                    # 建任务 + 第 1 次轮询（非终态）
    assert service.store.get(tid).status == "in_progress"
    assert not fake_jimeng.of("edit"), "生成腿还没成功，不该提前发超清腿"

    client_state.coordinator.tick()           # 2k 生成成功 ⇒ 立刻接超清腿
    edits = fake_jimeng.of("edit")
    assert len(edits) == 1, "生成成功后必须**自动**接上超清腿"
    assert edits[0]["tool"] == "normal_hd"
    assert edits[0]["item_id"] == 777
    assert edits[0]["origin_history_id"] == int(HIST)
    rec = service.store.get(tid)
    assert rec.status == "in_progress", "超清腿未终态前**不许**判成功"
    assert rec.upstream_submit_id == fake_jimeng.submitted[-1], "轮询对象已切到超清腿"

    client_state.coordinator.tick()           # 超清腿终态 ⇒ 收尾
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/up.png"]
    assert any("4k 改道完成" in d for d in rec.degradations)


def test_the_billing_anchor_survives_the_upscale_leg(
        client, client_state, fake_jimeng, service):
    """🔴 `upstream_submit_id` 会被**超清腿**覆盖（在途轮询要查它）⇒ 真扣费那条
    生成腿的 id 必须另存 `gen_submit_id`。

    否则 4k 改道之后，"这次到底扣没扣"就**没有 submit_id 可查**了 ——
    而本仓库的对账纪律就是"按 submit_id 回 `user_credit_history` 查"。
    线上实测（2026-10-12）：库里那个字段变成了超清腿 id、账单里查不到；
    真扣的 3 积分挂在生成腿 id 上，只能翻日志。
    """
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        _upscale_ok("https://cdn/up.png"),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 1)
    gen_sid = service.store.get(tid).gen_submit_id
    assert gen_sid == fake_jimeng.submitted[0], "**派发时**就要记下生成腿的 id"

    client_state.coordinator.tick()           # 2k 生成成功 ⇒ 接超清腿
    rec = service.store.get(tid)
    assert rec.upstream_submit_id == fake_jimeng.submitted[-1] != gen_sid, \
        "前提：轮询字段确实切到了超清腿（且两者本就不同）"
    assert rec.gen_submit_id == gen_sid, "超清腿**不许**覆盖计费锚点"

    client_state.coordinator.tick()           # 超清腿终态 ⇒ 收尾
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert rec.gen_submit_id == gen_sid, "收尾后锚点仍指向**真扣费**那条生成腿"
    assert rec.upstream_submit_id != gen_sid, "而在途字段指向超清腿（免费）"


def test_direct_generation_anchor_is_the_very_same_leg(
        client, client_state, fake_jimeng, service):
    """没改道时两者**相同** —— 锚点不是"另造一个值"，它就是生成腿本身。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/a.png"], items=(11,)),
    ]
    tid = _create(client, size="2048x2048")
    drive(client_state, 2)
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert rec.gen_submit_id == rec.upstream_submit_id == fake_jimeng.submitted[0]


def test_each_image_gets_its_own_upscale_leg(
        client, client_state, fake_jimeng, service):
    """n=2 ⇒ **每张一条**超清腿（引用形态一次只引用一张作品），逐张推进。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/a.png", "https://cdn/b.png"], items=(11, 22)),
        _upscale_ok("https://cdn/a-up.png"),
        _upscale_ok("https://cdn/b-up.png"),
    ]
    tid = _create(client, size="4096x4096", n=2)
    drive(client_state, 4)

    edits = fake_jimeng.of("edit")
    assert [e["item_id"] for e in edits] == [11, 22], \
        "两张各引用自己的 item_id（不是拿第一张连发两次）"
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == [
        "https://cdn/a-up.png", "https://cdn/b-up.png"]


def test_4k_suffix_is_rerouted_and_says_pixel_identical(
        client, service, fake_jimeng):
    """`-4k` 后缀（不带 size）⇒ 1:1，2k 槽就是默认的 2048²，交付 4096² 与 4k 档**完全一致**。

    🔴 这条覆盖的是**第二条入口**：后缀形态下 `size` 是默认的 2048x2048，
    若只按 `size` 吸附判档，这条入口会被整条漏掉（不改道、照付 4k 的钱）。
    """
    fake_jimeng.states = [submitted_state()]
    tid = _create(client, model="jimeng-t2i-4k")

    rec = service.store.get(tid)
    assert rec.size == "2048x2048"
    assert rec.resolution_tier == "2k"
    note = " ".join(rec.degradations)
    assert "已自动改走" in note
    assert "与 4k 档声明的像素一致" in note, \
        "1:1 下交付尺寸与 4k 档完全相同 —— 这一点要明说，别让调用方以为被降了"


def test_non_square_4k_says_the_delivered_size_is_not_the_4k_slot(
        client, service, fake_jimeng):
    """16:9 的 4k 契约是 5404×3040，而「2k 槽 ×2」是 5120×2880 ⇒ 必须**明说不同**。

    这是"按输入比例就地放大"口径的代价：比例保住了、像素**不等于**声明的 4k 槽。
    静默交付就是"让调用方按 A 的预期拿到 B" ⇒ 必须写进 degradations。
    """
    fake_jimeng.states = [submitted_state()]
    tid = _create(client, size="5404x3040")

    rec = service.store.get(tid)
    assert rec.size == "2560x1440", "按**输入比例**取 2k 槽"
    note = " ".join(rec.degradations)
    assert "5120x2880" in note and "5404x3040" in note
    assert "不等于" in note


def test_i2i_4k_is_also_rerouted(client, service, fake_jimeng):
    """图生图（blend）也在覆盖范围内（用户口径：t2i + i2i 都覆盖）。"""
    fake_jimeng.states = [submitted_state()]
    img = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
           + "5" * 32 + "~tplv-x.png?sig=1")
    tid = _create(client, model="jimeng-i2i", image=[img], size="4096x4096")

    rec = service.store.get(tid)
    assert rec.size == "2048x2048"
    assert rec.resolution_tier == "2k"
    assert "已自动改走" in " ".join(rec.degradations)


# ---------------------------------------------------------------------------
# 失败与不适用：都必须**可见**，且不许把能交付的图丢掉
# ---------------------------------------------------------------------------


def test_upscale_leg_failure_delivers_the_2k_original(
        client, client_state, fake_jimeng, service):
    """超分腿失败 ⇒ **交付 2k 原图 + 响亮留痕**，任务仍是 success（用户口径）。

    ⚠️ 2026-10-12 收尾后语义收窄为「**重试用尽**才退回 2k」：瞬时失败会先按
    `UPSCALE_MAX_RETRIES`（默认 1）重投同一张。这条用例因此给**两次连续失败**
    （一次吃重试、一次吃尽预算），验证"退回 2k"这个**最终**语义没变。
    """
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        failed_state("generate_failed"),
        failed_state("generate_failed"),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 4)

    rec = service.store.get(tid)
    assert rec.status == "success", "超分腿失败**不许**判任务失败（2k 图已到手）"
    assert [i["url"] for i in rec.images] == ["https://cdn/gen.png"]
    assert len(fake_jimeng.of("edit")) == 2, \
        "默认预算 1 ⇒ 一共只发 2 次（首发 + 1 次重试），不许无限重试"
    note = " ".join(rec.degradations)
    assert "超清未成功" in note and "2k 原图" in note
    assert "重试" in note, "试过重试也要留痕（降级可见的同一条纪律）"
    assert "未能完成超分" in note, "收尾时要给出'几张没放大'的总计"
    #（2026-10-15 文案统一：国内改道与 GPT Image 超分链共用"未能完成超分"）


# ---------------------------------------------------------------------------
# 超清腿的有界重试（2026-10-12 收尾）
# ---------------------------------------------------------------------------


def test_a_transient_upscale_failure_is_retried_and_then_delivered(
        client, client_state, fake_jimeng, service):
    """**瞬时**失败（上游终态无产物）重投同一张，成功则照常交付**放大图**。

    依据：新号实测 6 次放大里 **1 次**上游 `generate_failed`（`ret=0` 受理成功 +
    `item_list: []`；老号 4/4 成功，合计 9/10）⇒ **偶发**。这条腿**免费**，
    所以"先重试"的边际成本是 0，而它是"请求 >2k"的唯一清晰度来源。
    """
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        failed_state("generate_failed"),
        _upscale_ok("https://cdn/up.png"),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 4)

    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/up.png"], \
        "重试成功后必须交付**放大图**，不是退回 2k"
    edits = fake_jimeng.of("edit")
    assert len(edits) == 2, "同一次失败必须真的重投（且只多投 1 次）"
    assert edits[1]["item_id"] == edits[0]["item_id"], "重试的是**同一张**"
    note = " ".join(rec.degradations)
    assert "重试" in note, "重试必须留痕"
    assert "未能完成超分" not in note, "重试成功就不该再报'未能完成超分'"


def test_retry_budget_zero_restores_the_immediate_fallback(
        client, client_state, fake_jimeng, service):
    """`UPSCALE_MAX_RETRIES=0` ⇒ 回到**旧行为**：一次失败即退 2k、不重投。"""
    service.settings.upscale_max_retries = 0
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        failed_state("generate_failed"),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 3)

    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/gen.png"]
    assert len(fake_jimeng.of("edit")) == 1, "预算 0 ⇒ 不许重投"
    assert "重试" not in " ".join(rec.degradations), "没重试就不该出现重试留痕"


def test_a_deterministic_failure_is_never_retried(
        client, client_state, fake_jimeng, service):
    """**确定性**失败（本地产物没有 `item_id`）⇒ 一次都不重投（重试必然同样失败）。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(None,)),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 2)

    assert not fake_jimeng.of("edit"), "缺 item_id 时连首发都不该发"
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/gen.png"]
    note = " ".join(rec.degradations)
    assert "item_id" in note and "重试" not in note, \
        "确定性失败要写明原因，且**不许**重试"


def test_missing_item_id_falls_back_to_2k_with_the_reason(
        client, client_state, fake_jimeng, service):
    """产物没有 `item_id` ⇒ 引用形态无从谈起 ⇒ 不盲发，退回 2k 原图并**说明原因**。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(None,)),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 2)

    assert not fake_jimeng.of("edit"), "缺 item_id 时不该发超清腿（发了也是必失败）"
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/gen.png"]
    assert any("item_id" in d for d in rec.degradations), "失败原因要落进留痕"


def test_direct_4k_when_capability_table_is_unreadable(
        settings, store, fake_jimeng, fake_uploader, fake_vod):
    """能力表读不到 ⇒ **不改道**（宁可照付 4k 的钱，也**不猜**一个尺寸）+ 明说没改。"""
    svc = Service(settings, store=store, client=fake_jimeng,
                  uploader=fake_uploader, vod=fake_vod, cfg=FakeConfigCache())
    rec = svc.create({"model": "jimeng-t2i", "prompt": "x", "size": "4096x4096"},
                     credential="cred-1")

    assert rec.size == "4096x4096", "读不到能力表就必须按请求档原样提交"
    assert rec.resolution_tier is None
    note = " ".join(rec.degradations)
    assert "image_basic_v5_4k" in note, "没改道时要照旧指名真实计费项"
    assert "未能" in note and "4k" in note


# ---------------------------------------------------------------------------
# 🔴 回归：受理期**必须**从凭据 bundle 拿能力表（2026-10-12 线上事故）
# ---------------------------------------------------------------------------


def test_accept_reads_the_table_from_the_credential_bundle(
        settings, store, fake_jimeng, fake_uploader, fake_vod):
    """受理期的 cfg **必须**来自**凭据 bundle**，不能依赖 `self.cfg`。

    事故现场：生产 `Service(settings)` 构造时**不带 cfg**（`main.py:191`），
    而 `self.cfg` 只在**派发 / 轮询 / 预审**的作用域里被 `_push_cred` 换入
    ⇒ **受理期间 `self.cfg` 恒为 None**（实测：3 条 4k 受理期间
    `get_common_config` 调用数 = **0**）⇒ `_plan_upscale` 在
    `if self.cfg is None` 上直接退出 ⇒ **4k 改道静默失效**。
    更坏的是它**有时**又非 None（别的线程换入的 bundle 尚未换出）⇒
    同一请求的成败取决于**别的线程的时序**，不可复现。

    ⇒ 本用例刻意**不注入 `cfg=`**（与生产同构），只把假 cfg 放进凭据池。
    """
    svc = Service(settings, store=store, uploader=fake_uploader, vod=fake_vod)
    assert svc.cfg is None, "前提：受理期 self.cfg 为空（与生产同构）"
    #: 凭据池里那一份才是受理期**该用**的（生产由 `bundle_for()` 现建/取缓存）。
    #: 这里预置成"缓存已命中"，与生产第二次起的行为一致，且**不发真网络**。
    svc._cred_pool["sess-1"] = (fake_jimeng, fake_uploader, fake_vod,
                                FakeConfigCache(rmap=V50_RMAP))

    rec = svc.create({"model": "jimeng-t2i", "prompt": "x", "size": "4096x4096"},
                     credential="cred-1", sessionid="sess-1")
    assert rec.size == "2048x2048", "受理期必须从凭据 bundle 拿到能力表并改道"
    assert rec.resolution_tier == "2k"
    assert "已自动改走" in " ".join(rec.degradations)


def test_no_sessionid_keeps_direct_4k_and_says_so(
        settings, store, fake_jimeng, fake_uploader, fake_vod):
    """没有任何凭据来源 ⇒ 读不到表 ⇒ **直出 4k + 响亮留痕**（不是静默）。

    这是上面那条的**反面**：修好之后仍必须保留"读不到就照付 4k 的钱、
    但绝不猜一个尺寸"的保守语义。
    """
    svc = Service(settings, store=store, uploader=fake_uploader, vod=fake_vod)
    rec = svc.create({"model": "jimeng-t2i", "prompt": "x", "size": "4096x4096"},
                     credential="cred-1")
    assert rec.size == "4096x4096"
    assert rec.resolution_tier is None
    assert "本次**未能**改走" in " ".join(rec.degradations)


def test_4k_suffix_on_a_model_without_4k_is_rejected_at_accept(
        settings, store, fake_jimeng, fake_uploader, fake_vod):
    """`-4k` 后缀 + 该模型服务端**没声明** 4k ⇒ 受理当场 400。

    这条钉住 `_check_tier` **也**走凭据 bundle —— 它在受理期曾同样因
    `self.cfg is None` 而**静默不校验**（读不到就当这回事不存在），
    表现为"以为买了 4k、实际按上游行为走"。
    """
    only2k = {"2k": V50_RMAP["2k"], "1.5k": V50_RMAP["2k"]}
    svc = Service(settings, store=store, uploader=fake_uploader, vod=fake_vod)
    svc._cred_pool["sess-2"] = (fake_jimeng, fake_uploader, fake_vod,
                                FakeConfigCache(rmap=only2k))

    with pytest.raises(Exception) as ei:
        svc.create({"model": "jimeng-t2i-4k", "prompt": "x"},
                   credential="cred-1", sessionid="sess-2")
    assert "4k" in str(ei.value) and "不被该模型支持" in str(ei.value)


def test_switch_off_keeps_direct_4k(
        settings, store, fake_jimeng, fake_uploader, fake_vod):
    """`UPSCALE_4K_ENABLED=0` ⇒ 回到旧行为（直出 4k），但留痕仍然写明没改道。"""
    svc = Service(settings.replace(upscale_4k_enabled=False), store=store,
                  client=fake_jimeng, uploader=fake_uploader, vod=fake_vod,
                  cfg=FakeConfigCache(rmap=V50_RMAP))
    rec = svc.create({"model": "jimeng-t2i", "prompt": "x", "size": "4096x4096"},
                     credential="cred-1")

    assert rec.size == "4096x4096"
    assert rec.resolution_tier is None
    assert "本次**未能**改走" in " ".join(rec.degradations)


# ---------------------------------------------------------------------------
# 判据本体（纯函数）：比例匹配与"拒绝乱猜"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bucket,w,h,expect", [
    ("2k", 4096, 4096, (2048, 2048)),      # 方形：按 1:1 落到 2k 槽
    ("2k", 5404, 3040, (2560, 1440)),      # 宽幅：按 16:9 落到 2k 槽
    ("4k", 2048, 2048, (4096, 4096)),
    ("4k", 2560, 1440, (5404, 3040)),      # 非整数倍也要精确命中 4k 槽
    ("2k", 1440, 2560, (1440, 2560)),      # 9:16 竖版 ⇒ 落到**竖**槽
    ("4k", 3040, 5404, (3040, 5404)),      # 竖版的 4k 请求要精确命中竖槽
    ("2k", 3024, 1296, (3024, 1296)),      # 21:9 这种表内非主流比例也要认
])
def test_slot_for_ratio_matches_the_same_ratio(bucket, w, h, expect):
    assert slot_for_ratio_in(V50_RMAP, bucket, w, h) == expect


def test_slot_for_ratio_refuses_rather_than_guessing():
    """🔴 读不到 / 档位不存在 / 比例配不上 ⇒ 一律 None（**拒绝猜**）。

    最后一条是真的：5:3（2000×1200 ≈ 1.667）**不在**这张表里，它离最近声明的
    3:2（1.5）有 0.167 的比例差（> 0.02 阈值）—— 若"就近给一个"，调用方就会
    拿到一个 3:2 的图却以为买的是 5:3。
    """
    assert slot_for_ratio_in(None, "4k", 4096, 4096) is None
    assert slot_for_ratio_in({}, "4k", 4096, 4096) is None
    assert slot_for_ratio_in(V50_RMAP, "8k", 4096, 4096) is None
    assert slot_for_ratio_in(V50_RMAP, "4k", 0, 0) is None
    assert slot_for_ratio_in(V50_RMAP, "2k", 2000, 1200) is None


def test_slot_for_ratio_keeps_orientation():
    """🔴 回归：3:4 与 4:3 的**短长边比同值 0.75** ⇒ 用 `min/max` 匹配时
    两者距离相同、谁在数组里靠前谁赢，竖图会被判成横图槽。
    必须按带方向的 `w/h` 匹配（2026-10-12 被本文件抓出的真 bug）。"""
    assert slot_for_ratio_in(V50_RMAP, "2k", 1728, 2304) == (1728, 2304)  # 3:4 竖
    assert slot_for_ratio_in(V50_RMAP, "2k", 2304, 1728) == (2304, 1728)  # 4:3 横
    assert slot_for_ratio_in(V50_RMAP, "4k", 3520, 4693) == (3520, 4693)  # 3:4 竖
    assert slot_for_ratio_in(V50_RMAP, "4k", 4693, 3520) == (4693, 3520)  # 4:3 横
    assert slot_for_ratio_in(V50_RMAP, "2k", 1664, 2496) == (1664, 2496)  # 2:3 竖
    assert slot_for_ratio_in(V50_RMAP, "2k", 2496, 1664) == (2496, 1664)  # 3:2 横
