#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下游节奏闸门（**同步实现**，与协调器的线程模型一致）。

为什么必须有这一层：上游的槽位是「占到底」的。即梦、文心、qwen 都有按节奏触发的
风控/限流（即梦的 `1018/1019/1021`），直连转发会在第 N 个请求就开始报错。

三类约束（都从 settings 读）：
  L1 并发上限   —— 由 `TaskStore.count_active() < JM_CONCURRENCY` 实现
                   （**不在这里**：并发数放在库里数，重启后依然正确；见 coordinator.py）
  L2 节奏器     两次「建任务」的最小间隔 + 60s 滑动窗口上限（防瞬时冲高）
  L3 熔断冷却   命中风控后进入冷却，冷却期内**不发任何请求**

L3 刻意选择「快速失败」而不是「排队等待」：风控是风险评分型，持续施压会延长标记，
把请求挂在队列里等冷却结束等于换个姿势施压。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .errors import RiskControlError, UpstreamRateLimitError


@dataclass
class GateStats:
    admitted: int = 0
    rejected: int = 0
    risk_hits: int = 0
    #: 额度耗尽（与风控命中**刻意分开计数**：前者与我们的行为无关，后者是我们触发的）
    quota_holds: int = 0
    waits: int = 0
    total_wait_s: float = 0.0
    last_at: float = 0.0
    cooling_for: float = 0.0
    #: 🔴 处于**额度静默**的凭据数（按凭据，不是全站）。与 `cooling_for`
    #:（风控，全局）**分开**暴露 —— 两个数的处置办法完全不同。
    quota_silent: int = 0
    #: 这些额度静默里最长的剩余秒数
    quota_cooling_max: float = 0.0
    #: 🔴 处于**风控静默**的凭据数（按凭据，见 `mark_risk_silent`）。
    #: 与全局 `cooling_for` 是**两层**：全局冷却到期后，这一层继续把
    #: "刚被上游惩罚的那个账号"挡在派发之外 —— 否则队首恒是它，会被垄断。
    risk_silent: int = 0

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "rejected": self.rejected,
            "risk_hits": self.risk_hits,
            "quota_holds": self.quota_holds,
            "waits": self.waits,
            "total_wait_s": round(self.total_wait_s, 2),
            "cooling_for": round(self.cooling_for, 1),
            "quota_silent": self.quota_silent,
            "quota_cooling_max": round(self.quota_cooling_max, 1),
            "risk_silent": self.risk_silent,
        }


class UpstreamGate:
    """按上游分桶的节奏闸门。线程安全。"""

    def __init__(self, name: str, *, min_interval: float = 0.0,
                 per_minute: int = 0, cooldown: float = 0.0,
                 max_wait: float = 120.0) -> None:
        self.name = name
        self.min_interval = max(0.0, min_interval)
        self.per_minute = max(0, per_minute)
        self.cooldown = max(0.0, cooldown)
        self.max_wait = max(0.0, max_wait)

        self._lock = threading.Lock()
        self._window: deque[float] = deque()
        self._last_at = 0.0
        #: 风控冷却（**全局**，见 `mark_risk_hit`）
        self._cooldown_until = 0.0
        #: 额度静默（**按凭据**，见 `mark_quota_exhausted`）
        self._quota_until: dict[str | None, float] = {}
        #: 风控静默（**按凭据**，见 `mark_risk_silent`）—— 与全局冷却并存
        self._risk_silent: dict[str, float] = {}
        self._stats = GateStats()

    # ---------------------------------------------------------------- 内部

    def _cooling_for(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def _pace(self) -> float:
        """节奏器：返回实际等待秒数。**记账只在真正放行时做一次**。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                while self._window and now - self._window[0] > 60.0:
                    self._window.popleft()

                need = 0.0
                if self._last_at and self.min_interval:
                    need = max(need, self.min_interval - (now - self._last_at))
                if self.per_minute and len(self._window) >= self.per_minute:
                    need = max(need, 60.0 - (now - self._window[0]) + 0.05)

                if need <= 0:
                    self._last_at = now
                    self._window.append(now)
                    if waited > 0:
                        self._stats.waits += 1
                        self._stats.total_wait_s += waited
                    return waited

                if waited + need > self.max_wait:
                    self._stats.rejected += 1
                    # 🔴 2026-10-12：只带 `retry_after`（→ `Retry-After` 头），
                    # **不带** `wait_so_far` / `min_interval` / `per_minute`
                    # —— 那三个会进 `error.detail`，"每分钟放几个、窗口多大"
                    # 是我方节流容量，属"对客户端不暴露太多"；
                    # "还要等多久"已由 `Retry-After` 说清（且它是真值，不靠猜）。
                    raise UpstreamRateLimitError(
                        f"{self.name} 上游节奏已满，请稍后重试",
                        upstream=self.name,
                        retry_after=need,
                    )
            step = min(need, 2.0)
            time.sleep(step)
            waited += step

    # ---------------------------------------------------------------- 生命周期

    def acquire(self) -> float:
        """取得一次「发请求」的许可；返回等待秒数。失败抛 AdapterError 子类。"""
        cooling = self._cooling_for()
        if cooling > 0:
            with self._lock:
                self._stats.rejected += 1
            raise RiskControlError(
                f"{self.name} 上游处于风控冷却中，{cooling:.0f}s 内不会接受请求；"
                f"请勿重试（持续施压会延长风控标记），必要时人工过一次验证",
                upstream=self.name,
                retry_after=cooling,
            )
        waited = self._pace()
        with self._lock:
            self._stats.admitted += 1
        return waited

    def mark_risk_hit(self) -> None:
        """命中上游风控 —— 进入冷却期。"""
        with self._lock:
            self._stats.risk_hits += 1
            if self.cooldown > 0:
                self._cooldown_until = time.time() + self.cooldown

    def mark_quota_exhausted(self, seconds: float, *,
                             key: str | None = None) -> None:
        """额度耗尽 → **该凭据**进入静默期（`seconds` 秒内不再派发它的任务）。

        与 `mark_risk_hit()` 刻意分开，两件事性质不同：
          · 风控命中 = **我们的节奏**触发了上游保护机制，计 `risk_hits`；
          · 额度耗尽 = 与我们的行为无关，只是一个**不会因重试而变好的计数器**，
            计 `quota_holds`。

        🔴 2026-10-12 修：静默**按凭据存**（`key` = 调用方的 sessionid）。
        此前它写的是全局 `_cooldown_until` ⇒ **一个账号积分不足，全站停 1200s**
        （实测：`quota_holds=1` 让 10 分钟里 592 个 tick 全被跳过，队列里
        别人的任务一条都发不出去）。而上游 `ret=1006`（"积分不足**或**没有
        相关权益"）本来就是**账号属性**，与别的调用方毫无关系。
        风控（`mark_risk_hit`）**保持全局** —— 它可能是 IP/设备级的，宁严勿松。

        时长刻意**有界**：不做"锁到明天"—— 上游重置时刻未必是本地零点，
        静默期到点自然重试，能自愈。
        """
        if seconds <= 0:
            return
        with self._lock:
            self._stats.quota_holds += 1
            self._quota_until[key] = max(self._quota_until.get(key, 0.0),
                                         time.time() + seconds)

    def quota_cooling_for(self, key: str | None) -> float:
        """该凭据的**额度静默**剩余秒数（0 = 不在静默期）。"""
        now = time.time()
        with self._lock:
            until = self._quota_until.get(key, 0.0)
            if until <= now:
                self._quota_until.pop(key, None)
                return 0.0
            return until - now

    def quota_silent_keys(self) -> set:
        """当前处于额度静默的**凭据集合**（顺手清掉过期的）。"""
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._quota_until.items() if v <= now]:
                self._quota_until.pop(k, None)
            return set(self._quota_until)

    def mark_risk_silent(self, seconds: float, *,
                         key: str | None = None) -> None:
        """风控命中 → **该凭据**额外进入静默期（**不替代**全局冷却）。

        🔴 2026-10-14（线上实测）：只有全局冷却时会发生**队首垄断** ——
        上游 `ret=1018`（`account punish limit ai generate`，**账号级**）命中后
        全局冷却 600s；冷却一到期，协调器按 `order="oldest"` 取出的队首恰好是
        被罚账号那条任务 ⇒ 立刻再命中 ⇒ 再全局冷却 600s……实测 14:09 起
        每 10 分钟一轮，全站零成功、47 条任务积压，而队列里**别的账号**
        （从未命中过风控）一条都发不出去。

        这一层用来打破循环：全局冷却照旧，只是冷却到期后**不拿同一个账号
        反复去撞墙**。对上游的总请求量因此**只减不增** —— 是收紧不是放宽。
        """
        if seconds <= 0 or not key:
            return
        with self._lock:
            self._risk_silent[key] = max(self._risk_silent.get(key, 0.0),
                                         time.time() + seconds)

    def risk_cooling_for(self, key: str) -> float:
        """该凭据的**风控静默**剩余秒数（0 = 不在静默期）。"""
        now = time.time()
        with self._lock:
            until = self._risk_silent.get(key, 0.0)
            if until <= now:
                self._risk_silent.pop(key, None)
                return 0.0
            return until - now

    def risk_silent_keys(self) -> set:
        """当前处于风控静默的**凭据集合**（顺手清掉过期的）。"""
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._risk_silent.items() if v <= now]:
                self._risk_silent.pop(k, None)
            return set(self._risk_silent)

    def reset_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0

    def stats(self) -> dict:
        with self._lock:
            # `cooling_for` **只表示风控冷却**（全局）—— 协调器据它决定
            # "这一轮谁都不发"。额度静默别混进来，见 `mark_quota_exhausted`。
            self._stats.cooling_for = self._cooling_for()
            self._stats.last_at = self._last_at
            now = time.time()
            quiet = {k: v for k, v in self._quota_until.items() if v > now}
            if len(quiet) != len(self._quota_until):
                # 键是调用方给的（可以很多）⇒ 不扫就无界增长
                self._quota_until.clear()
                self._quota_until.update(quiet)
            self._stats.quota_silent = len(quiet)
            self._stats.quota_cooling_max = max(
                (v - now for v in quiet.values()), default=0.0)
            quiet_risk = {k: v for k, v in self._risk_silent.items() if v > now}
            if len(quiet_risk) != len(self._risk_silent):
                # 与额度静默同理：键是调用方给的，不扫就无界增长
                self._risk_silent.clear()
                self._risk_silent.update(quiet_risk)
            self._stats.risk_silent = len(quiet_risk)
            return {"name": self.name,
                    "min_interval": self.min_interval,
                    "per_minute": self.per_minute,
                    **self._stats.as_dict()}


def build_gate(settings) -> UpstreamGate:
    """按配置构造即梦闸门。"""
    return UpstreamGate(
        "jimeng",
        min_interval=settings.jm_min_interval,
        per_minute=settings.jm_per_minute,
        cooldown=settings.jm_cooldown,
        max_wait=settings.jm_max_wait,
    )


__all__ = ["UpstreamGate", "GateStats", "build_gate"]
