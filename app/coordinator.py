#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台协调器 —— **推进任务的唯一执行者**。

## 为什么必须有它（而不是"查询时惰性回查上游"）

即梦**没有回调**：`get_history_by_ids` 得我们自己轮询。于是有两件事
惰性轮询做不到：

1. **任务永远不会自己前进**：调用方不 `GET`，queued 的任务就永远排在队里 ——
   而队列里排着的任务连"建任务"都还没发生。惰性方案下，
   `POST` 之后必须有人 `GET` 才会真正开始生图，这违反直觉且会饿死任务。
2. **超时看门狗不会触发**：没人查就永远卡在 in_progress，永不判超时。

⇒ 本服务的协调器**默认开启**（与"可选、默认关闭"的通用做法相反），
因为在这里它不是增强，而是**链路的一环**。

## 并发上限放在"库里数"，不放在信号量里

`JM_CONCURRENCY` 的判据是 `store.count_by_status("in_progress")` ——
**不是** `asyncio.Semaphore`/`threading.BoundedSemaphore`。理由：进程重启后
信号量归零，而库里那 N 个任务其实还在上游跑；用信号量会让重启后**超发**。
用库计数则天然重启安全（且天然跨 worker）。

## 选主

多 worker 时靠 SQLite 租约保证只有一个在推进。没有它，两个进程会同时
提交同一个任务 —— 而建任务是**计费动作**，等于重复扣积分。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from .config import Settings
from .errors import quota_exhausted
from .observability import OBS
from .service import Service
from .store import TaskRecord

log = logging.getLogger(__name__)

#: 每轮处理多少个 in_progress 任务。有 JM_CONCURRENCY 兜着，这里给个宽松上界即可。
_POLL_BATCH = 32
#: 每轮**扫描**多少条排队任务。刻意大于派发额度：处于额度静默的凭据可能有
#: 任务堵在队首，窗口太小的话后面的正常任务永远轮不到（派发饥饿）。
_DISPATCH_SCAN = 64
#: 多久做一次维护（清理过期终态任务）
_MAINTENANCE_EVERY = 300.0


def _fair_batch(recs: list[TaskRecord], budget: int,
                cursor: int = 0) -> tuple[list[TaskRecord], int]:
    """按凭据轮转取 `budget` 条；返回 `(选中, 新 cursor)`。

    🔴 **v1 的缺陷**（2026-10-14 线上抓到，`v0.1.32`）：
    原先只在**单次调用内**轮转 —— 第 0 轮取"桶里的**第一个**凭据"。
    而生产常态是 `budget=1`（4 个并发位只空出 1 个）⇒ **永远取同一个凭据**，
    另一个凭据在**队首干等**：实测 `ticks=589 / dispatched=75`（89% 的 tick
    因并发满跳过），在途 4 位长期被同一账号占着，另一个凭据的任务等了 18 分钟
    一次都没被派发。用户的观察正是"我的任务在队首却不动"。

    **修法**：轮转**跨 tick 记忆**（`cursor` 在凭据序列上推进），于是
    `budget=1` 时也严格 A→B→A→B 交替。cursor 是**进程内状态**，重启归零
    —— 只影响"从谁开始"，不影响正确性（无公平性回归）。

    ⚠️ **单凭据独占时行为仍与原来一致**（只有一个桶，取出来就是 oldest 顺序）。
    """
    buckets: dict[str | None, list[TaskRecord]] = {}
    for rec in recs:
        buckets.setdefault(rec.upstream_sessionid, []).append(rec)
    keys = list(buckets)
    if not keys:
        return [], cursor

    out: list[TaskRecord] = []
    depth = 0
    n = len(keys)
    while len(out) < budget:
        progressed = False
        for step in range(n):
            pos = (cursor + step) % n
            items = buckets[keys[pos]]
            if depth < len(items):
                out.append(items[depth])
                progressed = True
                if len(out) >= budget:
                    #: 下一次从这里继续 —— 让"上一次派给了谁"影响"这次轮到谁"
                    return out, (pos + 1) % n
        if not progressed:            # 所有桶都取空了
            break
        depth += 1
    return out, cursor


class Coordinator:
    """单实例后台线程。`tick()` 是公开的 —— 测试直接调它，不启线程。"""

    def __init__(self, service: Service, settings: Settings,
                 *, owner: str | None = None) -> None:
        self.service = service
        self.settings = settings
        self.owner = owner or f"{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        #: 🔴 "有新任务了，别等下一个 tick" 的唤醒信号。
        #: 没有它的话，受理与派发之间的延迟平均是 tick/2、最坏是整整一个 tick
        #: （默认 1s）—— 而对一次 6s 级的任务，这 1s 白等得很明显。
        #: 用 Event 而不是把 tick 调小：**事件驱动零成本**，调小 tick 会让
        #: DB 查询次数按倍增加（每轮都要 count + list）。
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        #: 🔴 2026-10-21：**轮询独立线程**（不与派发共用）。
        #: 派发是同步的，一次慢上传会把整个 tick 卡住；拆开后慢派发只堵派发，
        #: 在途任务的推进（槽位释放）照常。
        self._poll_thread: threading.Thread | None = None
        #: 本进程当前是否持有协调器租约（派发线程每轮更新，轮询线程只读）。
        self._lease_ok = False
        self._last_maintenance = 0.0
        #: 累计统计（运维观测）
        self.ticks = 0
        self.skipped_lease = 0
        #: 公平轮转的"上一次派给了谁"游标（见 `_fair_batch`）。
        #: ⚠️ 没有它，`budget=1`（并发位只剩 1 个 —— 生产常态）时会**永远**
        #: 派给同一个凭据，另一个凭据饿死（`v0.1.32` 实测的缺陷）。
        self._rr_cursor = 0
        #: 🔴 **派发饥饿计数器**（2026-09-23 补）。运维症状是"任务一直排队、什么都不动"，
        #: 而此前 `stats()` 只能看到 ticks / skipped_lease —— 于是"并发额度被在途任务
        #: 占满"这条最常见的原因**完全不可见**（实测：一个在途任务 + 默认并发 1
        #: ⇒ 新任务排 15 分钟不动，日志零线索）。
        self.dispatch_full = 0        # 因"在途数已达并发上限"而跳过派发的轮次
        self.dispatch_cooling = 0     # 因"上游冷却中"而跳过派发的轮次
        #: 因"**该凭据**额度静默中"而跳过的任务数（按凭据，不是全站）
        self.dispatch_quota_silent = 0
        #: 因"**该凭据**风控静默中"而跳过的任务数（按凭据，不是全站）
        self.dispatch_risk_silent = 0
        self.dispatched = 0           # 真正派发出去的任务数

    # ------------------------------------------------------------------ 线程

    def start(self) -> None:
        if not self.settings.coordinator_enabled:
            OBS.warning("coordinator disabled", reason="COORDINATOR_ENABLED=0")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="jimeng-coordinator", daemon=True)
        self._thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="jimeng-poll", daemon=True)
        self._poll_thread.start()
        OBS.info("coordinator started", owner=self.owner,
                 tick_s=self.settings.coordinator_tick,
                 concurrency=self.settings.jm_concurrency)
        # 🔴 2026-10-03：原先这里会因"未配置 JIMENG_SESSIONID"告警并说
        # "任务不会被推进"，**已删除** —— 透传后服务不持有凭据，
        # `upstream_configured` 恒真，这个分支永远走不到；
        # 而留着它只会让运维以为"不配 env 就跑不了"（其实每个请求
        # 自己带 Bearer）。真实失败点已下移到派发时（upstream_not_configured）。

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()          # 立刻从 wait 里醒来，别在退出前白等一个 tick
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=timeout)
            self._poll_thread = None

    @property
    def running(self) -> bool:
        """后台线程是否**真的在跑** —— 同步接口据此判"当前有没有推进者"。

        🔴 为什么不是看 `settings.coordinator_enabled`：配置只说明**意图**，
        而决定任务能不能被推进的是"线程活着"这件事。声明开着、线程却没起来
        （启动期异常、或有人手工构造了 Coordinator 但没 `start()`）时，
        同步等待必然熬到预算耗尽，还让调用方以为"它在跑" —— 那是制造假能力。
        """
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        """叫醒循环：**有新任务进来了，不用等下一个 tick**。

        由受理路径调用（`main.py` 里 `create` 之后）。它是**纯优化**：
        丢了这次唤醒最多也只是慢一个 tick，正确性不受影响 ——
        因为"该派发谁"始终由库里的状态决定，而不是由谁叫过它决定。
        """
        self._wake.set()

    def _loop(self) -> None:
        if self.settings.upstream_configured:
            self._prewarm()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("coordinator tick 异常（已吞掉，下一轮继续）")
            # 要么被叫醒（有新任务），要么等满一个 tick。
            # 超时仍然保留：它兜住"唤醒信号丢了"这类情况。
            self._wake.wait(self.settings.coordinator_tick)
            self._wake.clear()

    def _poll_loop(self) -> None:
        """**独立的轮询线程**（2026-10-21 从 `_loop` 里拆出来）。

        🔴 为什么必须独立：`dispatch` 是同步调用，i2i 上传一次被限流就能把
        派发线程卡 60s；原先轮询和派发在同一个 tick 里 ⇒ 那 60s 里
        **在途任务也不推进**（线上铁证：`idle_s == running_s == 364`），
        槽位不释放，吞吐直接掉一半。
        `poll_many` 内部按 `JIMENG_POLL_INTERVAL` 节流，所以每秒调是安全的。
        """
        while not self._stop.is_set():
            try:
                self.tick_poll()
            except Exception:
                log.exception("coordinator 轮询轮异常（已吞掉，下一轮继续）")
            self._stop.wait(1.0)

    def _prewarm(self) -> None:
        """启动预热：把**与具体任务无关**的只读往返提前做掉。

        两样东西每个进程只需要一次，但原先都是"第一个任务才付"：
          · 模型/价格表（`get_common_config`，实测约 200ms）
          · 上传用的 STS（`get_upload_token`，实测约 440ms；本来就有双检锁共享）

        它只影响"重启后第一个任务"的延迟 —— 但那一刻恰好是**部署完立刻试用**的
        时刻，观感最差。两者都是只读、**不计费**。

        在**协调器线程**里做，所以不拖慢 HTTP 启动（`/healthz` 立刻可用）；
        任何失败都只告警 —— 预热是优化，绝不能成为启动的前提条件。
        """
        t0 = time.monotonic()
        warmed: list[str] = []
        try:
            if self.service.cfg is not None:
                self.service.cfg.snapshot()
                warmed.append("model_config")
        except Exception as e:  # noqa: BLE001
            log.warning("预热模型配置失败（不影响运行）：%s", e)
        try:
            if self.service.uploader is not None:
                self.service.uploader.token()
                warmed.append("upload_token")
        except Exception as e:  # noqa: BLE001
            log.warning("预热上传凭据失败（不影响运行）：%s", e)
        if warmed:
            OBS.info("startup prewarm", items=warmed,
                     elapsed_ms=round((time.monotonic() - t0) * 1000, 1))

    # ------------------------------------------------------------------ 单轮

    def tick(self) -> None:
        """一轮**完整**推进（测试/运维入口：同步跑完派发 + 轮询 + 维护）。

        生产上这两个阶段由**两个线程**分别驱动（`_loop` / `_poll_loop`），
        见 `tick_dispatch()` / `tick_poll()` —— 拆开是为了"慢派发不堵轮询"。
        """
        self.tick_dispatch()
        self.tick_poll()

    def tick_dispatch(self) -> None:
        """**派发线程**的一轮：抢租约 → 派发 → 维护。租约抢不到直接返回。"""
        self.ticks += 1
        if not self.settings.upstream_configured:
            return
        store = self.service.store
        if not store.acquire_lease("coordinator", self.owner,
                                   self.settings.coordinator_lease):
            self.skipped_lease += 1
            self._lease_ok = False
            return
        self._lease_ok = True
        try:
            self._dispatch_queued()
            self._maintenance()
        finally:
            store.release_lease("coordinator", self.owner)

    def tick_poll(self) -> None:
        """**轮询线程**的一轮：推进所有在途任务（不抢租约，只看 `_lease_ok`）。"""
        if not (self._lease_ok and self.settings.upstream_configured):
            return
        self._poll_running()

    # ------------------------------------------------------------------ 各段

    def _dispatch_queued(self) -> None:
        """把排队任务推给上游。**并发上限按库计数**（重启安全，见模块 docstring）。"""
        store = self.service.store
        running = store.count_by_status("in_progress")
        budget = self.settings.jm_concurrency - running
        if budget <= 0:
            # 响亮计数：这条路径此前是**静默 return**，而它的症状（"任务一直排队"）
            # 与"上游慢"长得一模一样 —— 运维只能靠猜。别在这里打日志（一 tick 一条），
            # 计数走 `stats()`，需要细节时看 `/stats`。
            self.dispatch_full += 1
            return
        # 冷却中就不要再去 acquire（gate 会抛，日志会被刷；而且那本来就是"别打"）
        if self.service.gate.stats()["cooling_for"] > 0:
            self.dispatch_cooling += 1
            return
        # 额度静默（`ret=1006`）是**按凭据**的：那是"这个账号积分不足/无权益"，
        # 与别的调用方毫无关系 ⇒ 只跳过该凭据的任务。2026-10-12 前这里是靠
        # `cooling_for` 做的**全站**停摆，一个号没额度就把所有人拖死。
        silent = self.service.gate.quota_silent_keys()
        #: 🔴 2026-10-14：风控静默（按凭据）与额度静默**分开计数**。
        #: 两者的处置办法不同（风控要过一次验证 / 等标记衰减，额度要充值），
        #: 合并进 `quota_silent` 会让 /stats 骗人。
        risk_silent = self.service.gate.risk_silent_keys()
        #: 先按 `oldest` 取一批候选，**再按凭据轮转**挑出这一轮该派发的
        #: —— 单凭据不得垄断并发位（见 `_fair_batch`；两处分工：过滤负责
        #: "谁不该派"，轮转负责"该派的怎么排序"）。
        cands: list[TaskRecord] = []
        for rec in store.list_by_status("queued", limit=_DISPATCH_SCAN,
                                        order="priority"):
            if rec.upstream_sessionid in silent:
                self.dispatch_quota_silent += 1
                self._fail_stale_quota_task(rec)
                continue
            if rec.upstream_sessionid in risk_silent:
                self.dispatch_risk_silent += 1
                continue
            cands.append(rec)

        #: 🔴 2026-10-21（用户口径"同步任务不做 queued 免得积压"）：
        #: **同步请求直接插到队首**，不参与按凭据轮转 —— 否则 800 条异步积压
        #: 会让每个同步请求都白等满预算（线上实测 849 条、最老等 5 天）。
        #: 并发预算不变（还是那几个槽），只是"谁先用"变了。
        sync_first = [r for r in cands if r.priority > 0][:budget]
        rest = [r for r in cands if r.priority <= 0]
        #: 其余按凭据轮转（**跨 tick 记忆游标** —— 否则 `budget=1` 时永远同一个凭据）。
        picked, self._rr_cursor = _fair_batch(
            rest, max(0, budget - len(sync_first)), self._rr_cursor)
        for rec in [*sync_first, *picked]:
            self.service.dispatch(rec)
            self.dispatched += 1

    def _fail_stale_quota_task(self, rec: TaskRecord) -> None:
        """额度静默里的排队任务：等够一个同步预算就**判失败**，别让它变僵尸。

        🔴 2026-10-21 线上实测：额度静默期间受理的同步请求不会被派发，
        `_sync_generate` 等满 `SYNC_MAX_WAIT` 抛 504，但 504 **不取消任务**
        （"任务照跑"的语义），可这一条**根本没在跑** ⇒ 任务永久 queued。
        它的 `created_at` 又落在同步闸门的窗口内 ⇒ `oldest_wait_s` 一路涨 ⇒
        **全站**同步请求 429（一个号没额度把所有调用方拖死）。

        为什么用"等待 ≥ 同步预算"而不是"一进静默就判死"：静默本身**有界**
        （`min(JM_COOLDOWN*2, 3600)`），窗口内到期的任务还该有机会真跑一次；
        而等超过同步预算的，调用方那边**早就**拿到 504 了，留着它只有害。
        """
        grace = max(60.0, float(self.settings.sync_max_wait))
        if time.time() - rec.created_at < grace:
            return
        self.service.fail_queued(rec, quota_exhausted(waited_s=grace))

    def _poll_running(self) -> None:
        """推进所有在途任务 —— **上游查询合并成一次**（见 `Service.poll_many`）。

        为什么要合并：`get_history_by_ids` 吃的是 `submit_ids`（复数），
        逐个查会让上游请求量随在途任务数线性增长。默认并发 1 时收益为零，
        但它是"把 `JM_CONCURRENCY` 提上去"的前提 ——
        否则提并发等于把上游请求量一起乘 N，而那正是风控最敏感的维度。
        """
        recs = self.service.store.list_by_status("in_progress", limit=_POLL_BATCH)
        if not recs:
            return
        self.service.poll_many(recs)

    def _maintenance(self) -> None:
        now = time.time()
        if now - self._last_maintenance < _MAINTENANCE_EVERY:
            return
        self._last_maintenance = now
        try:
            n = self.service.store.prune(
                retention_days=self.settings.task_retention_days)
            if n:
                OBS.info("pruned expired tasks", removed=n,
                         retention_days=self.settings.task_retention_days)
        except Exception:
            log.exception("任务清理失败")

    def stats(self) -> dict:
        return {"owner": self.owner, "ticks": self.ticks,
                "skipped_lease": self.skipped_lease,
                #: 派发侧：`dispatched` 长时间不涨而 `dispatch_full` 在涨
                #: ⇒ 并发额度被在途任务占满（把 JM_CONCURRENCY 提上去，或
                #: 等那条在途任务到 TASK_TIMEOUT）。
                "dispatched": self.dispatched,
                "dispatch_full": self.dispatch_full,
                "dispatch_cooling": self.dispatch_cooling,
                "dispatch_quota_silent": self.dispatch_quota_silent,
                "dispatch_risk_silent": self.dispatch_risk_silent,
                "running": self.running}


__all__ = ["Coordinator"]
