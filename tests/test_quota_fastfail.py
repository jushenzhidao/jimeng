#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分不足（上游 `ret=1006`）必须**立刻报错返回** —— 2026-10-21 用户口径。

线上现场（20:33~20:45，国内号 `49e3ace7…`）：

  ① 20:33:30 一条 i2i 提交被上游拒 `ret=1006 积分不足或没有相关权益`，
     `Service._on_dispatch_error` 给该凭据打**额度静默** 1200s
     （`min(JM_COOLDOWN*2, 3600)`，`retryable=False`）；
  ② 20:34:02 / 20:35:58 两条**同步** i2i（`priority=1`）照常受理，派发时
     因凭据在静默里被 `continue` 跳过 ⇒ **永远排不上**；
  ③ `_sync_generate` 等满 `SYNC_MAX_WAIT=300` 抛 504，而 504 **不取消任务**
     （"任务照跑"的语义）—— 可这一条**根本没在跑** ⇒ 永久 queued 僵尸
     （`attempts=0`、`upstream_submit_id` 空 = 零计费）；
  ④ 僵尸的 `created_at` 落在 `sync_backlog_guard` 的窗口内 ⇒
     `oldest_wait_s` 顶到 666s ⇒ **全站**同步请求 `429 queue_overloaded`
     （日志 20:42~20:43 每 170ms 一条 `sync backlog fast-fail`，别人陪葬）。

现在的三层防护（本文件逐条钉住）：

  · **受理口**（`Service.create`）：静默期直接 429 `upstream_quota_exhausted`，
    **不建任务、不排队、零计费**；
  · **同步面**（`main._sync_generate`）：额度错**早于**积压闸 —— 别用
    "等一会儿再试"（`queue_overloaded`）去描述一个"永远好不了"的状态；
  · **派发层**（`Coordinator._fail_stale_quota_task`）：静默期遗留下来的旧
    queued，等够一个同步预算就判失败 —— 清僵尸，别让它毒化全局闸门。
"""
from __future__ import annotations

import base64
from typing import Any

import pytest

from tests.conftest import AUTH, AUTH_B, KEY_A

SYNC = "/v1/images/generations"
ASYNC = "/async/v1/images/generations"

#: 最小 PNG（`media.sniff` 只认 magic bytes）—— data URI 在内存里解码，
#: 一个字节都不出网（测试纪律：零真实网络）。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


class _InlineCoordinator:
    """同步端点用的协调器替身：`running=True`，`wake()` 就地推进。

    与 `tests/test_api.py::_InlineCoordinator` 同构（那份是模块私有的，跨文件
    用不了）。本文件多数用例其实只用到 `running` —— 额度闸在 `create` **之前**
    就抛，一次上游往返都不会发生。
    """

    def __init__(self, real: Any, *, advances: int = 3) -> None:
        self.real = real
        self.advances = advances

    @property
    def running(self) -> bool:
        return True

    def wake(self) -> None:
        for _ in range(self.advances):
            self.real.tick()

    def stats(self) -> dict:
        return self.real.stats()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


@pytest.fixture
def sync_client(client, client_state):
    client_state.coordinator = _InlineCoordinator(client_state.coordinator)
    return client


# ---------------------------------------------------------------------------
# 受理口：立刻 429 `upstream_quota_exhausted`（不是 `queue_overloaded`）
# ---------------------------------------------------------------------------


def test_sync_rejects_immediately_when_quota_exhausted(sync_client,
                                                       client_state):
    """同步面：静默期**立刻**返回 429，且**不建任务**（不留僵尸）。"""
    svc = client_state.service
    svc.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "额度没了"},
                         headers=AUTH)

    assert r.status_code == 429, r.text
    err = r.json()["error"]
    assert err["code"] == "upstream_quota_exhausted", err
    # 🔴 绝不能报成"队列堵了"：那会让调用方以为等一会儿就好 —— 而它永远好不了
    assert err["code"] != "queue_overloaded", err
    assert "1006" in err["message"], "必须说清是上游额度（1006），不是我们的限速"
    assert svc.store.count() == 0, "受理口就该拒 —— 建了任务就又变僵尸"


def test_async_rejects_too_when_quota_exhausted(client, client_state):
    """异步面同样直接拒：额度是**确定性**失败，没有"先受理再告诉你"的道理。"""
    svc = client_state.service
    svc.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = client.post(ASYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers=AUTH)

    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "upstream_quota_exhausted", r.text
    assert svc.store.count() == 0


def test_quota_message_carries_the_newapi_disable_keyword(client, client_state):
    """message 必须含 new-api 的**默认自动禁用关键词** —— 否则网关不会停掉这个号。

    new-api 的 `AutomaticDisableStatusCodeRanges` 默认**只有 401**（429 不在内，
    见 `setting/operation_setting/status_code_ranges.go`），所以"额度耗尽 ⇒
    自动禁用渠道"只能靠 `AutomaticDisableKeywords` 兜底 —— 默认列表里有
    OpenAI 那句 `You exceeded your current quota`，匹配方式是 **message 子串 +
    不区分大小写**（`service.ShouldDisableChannel`）。

    删掉这句的症状：号已经没额度，网关**照派**，每条都白跑一趟 ——
    静默失效，比报错难查得多。所以钉成门禁。
    """
    client_state.service.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = client.post(ASYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers=AUTH)

    assert r.status_code == 429, r.text
    msg = r.json()["error"]["message"].lower()
    assert "you exceeded your current quota" in msg, msg


def test_quota_gate_is_per_credential(client, client_state):
    """静默按**凭据**：A 没额度不该连坐 B（与 `test_gate_quota_scope` 同口径）。"""
    client_state.service.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = client.post(ASYNC, json={"model": "jimeng-t2i", "prompt": "b1"},
                    headers=AUTH_B)

    assert r.status_code == 202, r.text


def test_unknown_model_still_reports_its_own_error(client, client_state):
    """参数错**不许**被额度 429 盖住（模型名解析失败 ⇒ 不拦，交给精确的 4xx）。"""
    client_state.service.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = client.post(ASYNC, json={"model": "no-such-model-xyz", "prompt": "x"},
                    headers=AUTH)

    assert r.status_code in (400, 404), r.text
    assert r.json()["error"]["code"] != "upstream_quota_exhausted", r.text


def test_audit_is_not_blocked_by_quota_silence(sync_client, client_state):
    """`jimeng-audit`（素材预审）豁免 —— 它不提交生成、不花额度。

    拦它就是误伤：预审本来是"提交前省钱"的第一道闸，号没额度时更需要它。
    """
    client_state.service.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = sync_client.post(SYNC, json={"model": "jimeng-audit",
                                     "image": [PNG_DATA_URI]}, headers=AUTH)

    # 空产物照旧 500 —— 说明走的是业务路径，没被额度闸拦（拦了会是 429）
    assert r.status_code == 500, r.text
    assert r.json()["error"]["code"] == "upstream_empty_result", r.text


# ---------------------------------------------------------------------------
# 同步面：额度错优先于积压闸（错误码必须指向真因）
# ---------------------------------------------------------------------------


def test_sync_reports_quota_before_backlog(sync_client, client_state,
                                           monkeypatch):
    """队列积压 **且** 号没额度时，报"没额度"——不是"队列堵了"。

    `queue_overloaded` 的可执行建议是"等/改用异步"，而额度耗尽等多久都没用。
    """
    monkeypatch.setattr("app.service.Service.queue_oldest_wait_s",
                        lambda self, *, within_s=None, priority_only=False: 9999)
    client_state.service.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    r = sync_client.post(SYNC, json={"model": "jimeng-t2i", "prompt": "x"},
                         headers=AUTH)

    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "upstream_quota_exhausted", r.text


# ---------------------------------------------------------------------------
# 派发层：清僵尸（否则 `oldest_wait_s` 被顶高 ⇒ 全站同步 429）
# ---------------------------------------------------------------------------


def test_dispatch_fails_stale_queued_task_of_silent_credential(client_state):
    """静默期里已等超过一个同步预算的 queued ⇒ 判失败，队列不再被毒化。"""
    svc = client_state.service
    rec = svc.create({"model": "jimeng-t2i", "prompt": "早就该死了"},
                     credential=KEY_A, sessionid=KEY_A)
    assert rec.status == "queued"
    #: 把它推到"等满一个同步预算（300s）"之前 —— 只有 `created_at` 能表达这件事
    svc.store.patch(rec.task_id, created_at=rec.created_at - 400)
    svc.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    client_state.coordinator.tick()

    got = svc.store.get(rec.task_id)
    assert got.status == "failure", got.status
    assert (got.error or {}).get("code") == "upstream_quota_exhausted", got.error
    assert svc.store.queue_snapshot()["queued"] == 0, "僵尸必须清掉"


def test_fresh_queued_task_survives_quota_silence(client_state):
    """刚受理的**不许**立刻判死：静默是**有界**的，窗口内到期它还该跑一次。"""
    svc = client_state.service
    rec = svc.create({"model": "jimeng-t2i", "prompt": "刚进来"},
                     credential=KEY_A, sessionid=KEY_A)
    svc.gate.mark_quota_exhausted(1200.0, key=KEY_A)

    client_state.coordinator.tick()

    got = svc.store.get(rec.task_id)
    assert got.status == "queued", "没等满预算就判失败 = 把可自愈的任务丢掉"
