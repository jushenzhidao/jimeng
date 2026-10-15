#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**受理去重** —— 2026-10-15 用户口径："减少无效重试"。

线上现场（nginx 访问日志 + 任务库，同一秒）：

    POST /v1/images/generations  400  407   ×5
    POST /v1/images/generations  202  837   ×3

调用方（网关）把 `202 + task_id`（或一个它不认识的 400）当成"没有拿到图"，
**同一秒连发 4~5 条一模一样的请求**。JSON 端点每条都新建一个**真实生成任务**
⇒ ① 每条都可能真花钱；② 队列被灌满（实测 `oldest_wait_s` 涨到 2891s / 48 分钟）
⇒ 所有人的同步请求一起降级。

服务侧的杠杆只有一个：**重复的"在途"提交不再重复建任务**。
判据 = 同一凭据 + 同一请求指纹 + 上一条**仍在途**（queued / in_progress）。

⚠️ 刻意**只覆盖在途**：上一条到终态之后的重复提交更可能是"我还想要一张"
（要第二张请用 `n=2` 或换 `seed`），那时照常新建 —— 见下面第 4 个用例。
"""
from __future__ import annotations

from tests.conftest import AUTH, AUTH_B, ok_state

BASE = "/async/v1/images/generations"


def _post(c, body, headers=AUTH):
    r = c.post(BASE, json=body, headers=headers)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def test_duplicate_in_flight_request_reuses_the_same_task(client, fake_jimeng):
    """同一请求在**上一条还在途**时重投 ⇒ **复用同一条任务**。

    这是本能力的全部意义：重投不再产生新任务 ⇒ 不重复计费、不涨队列。
    （用例里协调器不跑，所以第一条一直停在 `queued` —— 正好是线上"队列堵死
    时调用方疯狂重投"的那个状态。）
    """
    body = {"model": "jimeng-t2i", "prompt": "同一句话", "n": 1}

    first = _post(client, body)
    again = _post(client, body)
    third = _post(client, body)

    assert again == first, "在途重复提交必须复用同一条任务（否则就是重复计费）"
    assert third == first, "连续重投多少次都该收敛到同一条"
    # 锚点：库里只有**一条**任务 —— 这才是"没有重复计费"的硬证据
    assert client.get(BASE, headers=AUTH).json()["total"] == 1
    # 受理路径本来就不该碰上游（这里是"没多碰一次"）
    assert fake_jimeng.calls == [], f"受理不该调上游，实得 {fake_jimeng.kinds()}"


def test_dedupe_is_per_credential(client):
    """按**凭据**去重：别人的同一句话不该拿到我的 task_id。"""
    body = {"model": "jimeng-t2i", "prompt": "同一句话"}
    mine = _post(client, body, AUTH)
    theirs = _post(client, body, AUTH_B)
    assert theirs != mine, "不同凭据各自建任务（去重不该跨调用方串号）"


def test_a_different_request_is_never_deduped(client):
    """指纹不同的请求（换 prompt / 换 n / 换 seed / 换尺寸）一律照常新建。"""
    a = _post(client, {"model": "jimeng-t2i", "prompt": "甲"})
    b = _post(client, {"model": "jimeng-t2i", "prompt": "乙"})
    c2 = _post(client, {"model": "jimeng-t2i", "prompt": "甲", "n": 2})
    d = _post(client, {"model": "jimeng-t2i", "prompt": "甲", "seed": 42})
    assert len({a, b, c2, d}) == 4, "只要请求内容不同就必须是不同任务"


def test_dedupe_stops_once_the_previous_task_is_terminal(client, client_state,
                                                         fake_jimeng):
    """上一条**已到终态** ⇒ 不再去重（那是"我还想要一张"，不是重试）。

    这是本能力的**边界**，也是它敢默认开的理由：终态之后的重复请求照常新建，
    调用方不会莫名其妙少拿到一张。
    """
    body = {"model": "jimeng-t2i", "prompt": "同一句话"}
    first = _post(client, body)

    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    for _ in range(2):
        client_state.coordinator.tick()      # 第 1 轮建任务，第 2 轮轮询到终态
    assert client.get(f"{BASE}/{first}", headers=AUTH).json()["status"] == "success"

    second = _post(client, body)
    assert second != first, "前一条已终态 ⇒ 这次是新的生成请求，必须新建任务"
    assert client.get(BASE, headers=AUTH).json()["total"] == 2


def test_concurrent_duplicate_requests_create_only_one_task(service):
    """**并发**重投也必须只建一条 —— 检查与落库是**原子**的。

    为什么单独钉这一条：网关是**并行**重投的（实测同一秒 4~5 条），而
    `Service.create` 会经 `run_in_threadpool` 在**多线程**里跑 ⇒ 如果
    "查在途"与"落库"之间没有锁，两条会同时查不到、各落一条 —— 去重就白做了
    （该省的钱一分没省）。
    """
    import concurrent.futures as cf
    import threading

    body = {"model": "jimeng-t2i", "prompt": "并发同一句话"}
    barrier = threading.Barrier(8)          # 8 条尽量同时到

    def one(_i: int) -> str:
        barrier.wait()
        return service.create(body, credential="cred-race",
                              sessionid="sid-race").task_id

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(one, range(8)))

    assert len(set(ids)) == 1, (
        f"8 条并发同体请求必须收敛到**一条**任务，实得 {len(set(ids))} 条 "
        f"⇒ 检查与落库之间有竞态（去重形同虚设）")


def test_dedupe_can_be_turned_off(client_factory):
    """`ACCEPT_DEDUPE_ENABLED=0` ⇒ 回到"来多少建多少"（既有行为，便于回滚）。"""
    c = client_factory(accept_dedupe_enabled=False)
    body = {"model": "jimeng-t2i", "prompt": "同一句话"}
    assert _post(c, body) != _post(c, body)
    assert c.get(BASE, headers=AUTH).json()["total"] == 2


def test_dedupe_window_expires(client_factory, client_state):
    """窗口之外的"在途"任务不再参与去重 —— 防一条**卡死的僵尸任务**在它整个
    寿命里把后续所有重复提交都吞掉（那种"去重"会让调用方永远拿不到新任务）。"""
    import time as _time

    c = client_factory(accept_dedupe_window_s=30)
    body = {"model": "jimeng-t2i", "prompt": "窗口测试"}

    first = _post(c, body)
    assert _post(c, body) == first, "窗口内 ⇒ 复用（这就是去重本身）"

    # 把上一条"变老"到窗口之外（模拟卡死的在途任务）
    client_state.service.store.patch(first, created_at=int(_time.time()) - 600)
    assert _post(c, body) != first, (
        "窗口外 ⇒ 必须新建（否则一条僵尸任务会吞掉它之后的所有提交）")
