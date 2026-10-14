#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""队列**入队背压**与**水位可见性** —— 2026-10-14 "队列堵死"的根治。

背景（线上实测）：队列此前**没有任何上限** —— 一个调用方灌 181 条就把并发位
吃到"只剩它的任务在跑"，别人的提交排到几小时后。用户原话：
"这么长，**没看到请求打到 web**"。而 `/stats` 里连"排了多少、最老的等了多久"
都看不到，只能翻库。

两条措施：
  · **入队背压**（`MAX_QUEUED_PER_CREDENTIAL`）：单凭据排队封顶，超了立刻
    `429 queue_overloaded` + `Retry-After`；
  · **水位可见**：`/stats` 暴露 `queue`（排队数 / 最老等待 / 按凭据分布）。
"""
from __future__ import annotations

from tests.conftest import AUTH, AUTH_B


def test_backpressure_is_off_by_default(client):
    """`MAX_QUEUED_PER_CREDENTIAL=0`（默认）⇒ 与改前一致：来多少收多少。"""
    for i in range(5):
        r = client.post("/async/v1/images/generations",
                        json={"model": "jimeng-t2i", "prompt": f"x{i}"},
                        headers=AUTH)
        assert r.status_code == 202, r.text


def test_backpressure_rejects_when_the_credential_queue_is_full(client_factory):
    """达到单凭据上限 ⇒ **429 `queue_overloaded`** + `Retry-After`。"""
    c = client_factory(max_queued_per_credential=2)
    body = {"model": "jimeng-t2i", "prompt": "x"}

    for i in range(2):
        r = c.post("/async/v1/images/generations", json=body, headers=AUTH)
        assert r.status_code == 202, f"第 {i + 1} 条应当被受理：{r.text}"

    r = c.post("/async/v1/images/generations", json=body, headers=AUTH)

    assert r.status_code == 429, r.text
    err = r.json()["error"]
    assert err["code"] == "queue_overloaded", err
    assert "上限" in err["message"]
    assert "retry_after" in err, "过载必须告诉调用方等多久（别编精确值，粗估即可）"
    # ⚠️ 与**上游**限流刻意不同码：排查方向完全不同（这里上游是好的）
    assert err["code"] != "upstream_rate_limited"


def test_backpressure_is_per_credential_not_global(client_factory):
    """背压按**凭据**算：一个号灌满了，不该连坐别的号。"""
    c = client_factory(max_queued_per_credential=1)
    body = {"model": "jimeng-t2i", "prompt": "x"}

    assert c.post("/async/v1/images/generations", json=body,
                  headers=AUTH).status_code == 202
    assert c.post("/async/v1/images/generations", json=body,
                  headers=AUTH).status_code == 429        # A 满了
    assert c.post("/async/v1/images/generations", json=body,
                  headers=AUTH_B).status_code == 202      # B 不受影响


def test_stats_exposes_the_queue_watermark(client):
    """`/stats` 必须能直接看到**排队水位**（此前没有任何出口）。"""
    for i in range(3):
        client.post("/async/v1/images/generations",
                    json={"model": "jimeng-t2i", "prompt": f"x{i}"}, headers=AUTH)

    st = client.get("/stats", headers=AUTH).json()
    q = st.get("queue")
    assert q is not None, "/stats 缺 queue 段 —— 运维又要靠翻库判断堵没堵"
    assert q["queued"] >= 3, q
    assert "oldest_wait_s" in q, "最老等待是最有用的先行指标（同步预算比它）"
    assert isinstance(q["by_credential"], list) and q["by_credential"]
    # 凭据必须**脱敏**（只留前 12 位 + 省略号），别把完整 sessionid 摆出来
    for row in q["by_credential"]:
        assert len(row["credential"]) <= 15, row
