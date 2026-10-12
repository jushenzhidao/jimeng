#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4k 请求改道「2k 生成 + 超清就地放大」（2026-10-12）。

**用户口径**：请求清晰度 **> 2k** 的**图片生成**任务（t2i / i2i）一律改走
「2k 生成 + 超清就地放大」以降低消耗；超分腿失败则**交付 2k 原图 + 响亮留痕**
（不判任务失败）；非 1:1 按**输入比例就地放大**交付（尺寸 = 2k 槽的 2×）。

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
    """超分腿失败 ⇒ **交付 2k 原图 + 响亮留痕**，任务仍是 success（用户口径）。"""
    fake_jimeng.states = [
        submitted_state(),
        _gen_ok(["https://cdn/gen.png"], items=(777,)),
        failed_state("generate_failed"),
    ]
    tid = _create(client, size="4096x4096")
    drive(client_state, 3)

    rec = service.store.get(tid)
    assert rec.status == "success", "超分腿失败**不许**判任务失败（2k 图已到手）"
    assert [i["url"] for i in rec.images] == ["https://cdn/gen.png"]
    note = " ".join(rec.degradations)
    assert "超清未成功" in note and "2k 原图" in note
    assert "未能超清" in note, "收尾时要给出'几张没放大'的总计"


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
