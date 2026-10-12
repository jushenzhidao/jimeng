#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁**作用域**回归：额度静默 = 按凭据；风控冷却 = 全局。

背景（2026-10-12）：`mark_quota_exhausted` 原先写的是全局 `_cooldown_until`
⇒ **一个账号积分不足，全站停 1200s**（实测 `quota_holds=1` 让 10 分钟里
592 个 tick 全被跳过，队列里别人的任务一条都发不出去）。
但上游 `ret=1006`（"积分不足**或**没有相关权益"）本来就是**账号属性**。

风控（`mark_risk_hit`）**保持全局** —— 它可能是 IP/设备级的，宁严勿松。
"""
from __future__ import annotations

import pytest

from app.errors import RiskControlError
from app.gate import UpstreamGate
import app.gate as gate_mod


class _Clock:
    """可推进的假时钟（门禁内部就是 `time.time()` + `time.sleep`）。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:      # pragma: no cover - 本文件用不到
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(gate_mod, "time", c)
    return c


@pytest.fixture
def gate(clock):
    return UpstreamGate("jimeng", cooldown=600.0)


# --------------------------------------------------------------- 额度：按凭据
def test_quota_silence_does_not_touch_the_global_cooldown(gate):
    gate.mark_quota_exhausted(1200.0, key="sess-A")
    st = gate.stats()
    assert st["cooling_for"] == 0, "额度耗尽不是风控 —— 全局冷却必须保持 0"
    assert st["quota_silent"] == 1
    assert st["quota_cooling_max"] == pytest.approx(1200.0, abs=1.0)


def test_quota_silence_is_scoped_to_its_own_credential(gate):
    gate.mark_quota_exhausted(1200.0, key="sess-A")
    assert gate.quota_cooling_for("sess-A") > 0
    assert gate.quota_cooling_for("sess-B") == 0, "别的凭据不受连坐"
    assert gate.quota_silent_keys() == {"sess-A"}


def test_other_credential_can_still_acquire(gate):
    """这条是"不再连坐"的**直接判据**：静默一个号，另一个号照样放行。"""
    gate.mark_quota_exhausted(1200.0, key="sess-A")
    gate.acquire()                     # 全局无冷却 ⇒ 不该抛（只有风控才拦 acquire）
    assert gate.stats()["admitted"] == 1


def test_no_key_is_its_own_bucket(gate):
    """匿名/无 sessionid 的任务（`key=None`）自成一只桶，别把别人拖下水。"""
    gate.mark_quota_exhausted(1200.0, key=None)
    assert gate.quota_cooling_for(None) > 0
    assert gate.quota_cooling_for("sess-A") == 0


def test_quota_silence_expires_and_gets_pruned(gate, clock):
    gate.mark_quota_exhausted(100.0, key="sess-A")
    clock.t += 101.0
    assert gate.quota_cooling_for("sess-A") == 0
    st = gate.stats()
    assert st["quota_silent"] == 0
    assert st["quota_cooling_max"] == 0.0
    assert gate.quota_silent_keys() == set()


def test_repeated_quota_marks_take_the_longer_window(gate, clock):
    """同一个号连着报 1006 ⇒ 取**更晚**的那个到点时刻，别被短窗口覆盖。"""
    gate.mark_quota_exhausted(1200.0, key="sess-A")
    clock.t += 60.0
    gate.mark_quota_exhausted(300.0, key="sess-A")
    assert gate.quota_cooling_for("sess-A") == pytest.approx(1140.0, abs=1.0)


def test_zero_seconds_is_a_noop(gate):
    gate.mark_quota_exhausted(0.0, key="sess-A")
    assert gate.stats()["quota_silent"] == 0
    assert gate.stats()["quota_holds"] == 0


def test_quota_holds_counts_every_hit_even_repeated(gate):
    """计数器是"发生过几次"，不是"当前几个" —— 与 `quota_silent` 分工不同。"""
    gate.mark_quota_exhausted(600.0, key="sess-A")
    gate.mark_quota_exhausted(600.0, key="sess-A")
    st = gate.stats()
    assert st["quota_holds"] == 2
    assert st["quota_silent"] == 1


# --------------------------------------------------------------- 风控：全局
def test_risk_hit_still_blocks_everyone(gate):
    """风控是全局的（可能是 IP/设备级）⇒ `acquire()` 必须拦，且冷却期可见。"""
    gate.mark_risk_hit()
    assert gate.stats()["cooling_for"] > 0
    with pytest.raises(RiskControlError):
        gate.acquire()
    with pytest.raises(RiskControlError):
        gate.acquire()


def test_risk_and_quota_do_not_leak_into_each_other(gate, clock):
    gate.mark_quota_exhausted(1200.0, key="sess-A")
    gate.mark_risk_hit()
    st = gate.stats()
    assert st["quota_silent"] == 1 and st["cooling_for"] == pytest.approx(600.0,
                                                                         abs=1.0)
    # 风控到期后，额度静默仍在（两者互不影响）
    clock.t += 601.0
    st = gate.stats()
    assert st["cooling_for"] == 0
    assert st["quota_silent"] == 1
