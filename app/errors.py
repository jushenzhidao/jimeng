#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误分类体系 —— 把上游五花八门的失败收敛成可判定的对外错误。

分类原则（每条都对应一次实测）：
  · 请求本身写错（我们能判定）      -> 400 invalid_request_error
  · 上游凭据缺失/失效（`1015`）     -> 503 capability_unavailable（**部署问题，不是调用方的错**）
  · 风控命中（`1018/1019/1021/2035`）-> 429 risk_control_error（**不可重试**）
  · 频率/并发限流（`1010/1057/2020`）-> 429 rate_limit_error（可退避重试）
  · 积分/日额度耗尽（`1006/4001/121101`）-> 429 quota_exhausted（**重试无效**）
  · 内容审核/版权（`1063/1159/2003…`）-> 451 content_policy_violation
  · 上游 5xx / 非 JSON / WAF 页       -> 502 upstream_error
  · 上游超时                          -> 504 upstream_timeout_error

🔴 两条最要紧的区分，都吃过亏：
  1. **风控 ≠ 限流**：都是 429，但风控重试会**延长标记**（retryable=False），
     限流退避即可（retryable=True）。混成一个码会让调用方把"别再打"读成"等会儿再打"。
  2. **额度耗尽 ≠ 限流**：都是 429，但额度耗尽重试一万次也不会变好。
"""
from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """适配层错误基类，自带对外错误体所需的元信息。"""

    status_code: int = 502
    err_type: str = "upstream_error"
    err_code: str | None = None
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        upstream: str | None = None,
        retry_after: float | None = None,
        **detail: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.upstream = upstream
        self.retry_after = retry_after
        self.detail = {k: v for k, v in detail.items() if v is not None}

    def to_error(self) -> dict[str, Any]:
        """OpenAI 形态的错误体（只给它自己知道的值，不补 None 字段）。"""
        err: dict[str, Any] = {
            "message": self.message,
            "type": self.err_type,
            "code": self.err_code,
        }
        if self.param:
            err["param"] = self.param
        if self.retry_after is not None:
            err["retry_after"] = round(self.retry_after, 1)
        if self.detail:
            err["detail"] = dict(self.detail)
        return {"error": err}

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{type(self).__name__}: {self.message}"


class InvalidParameterError(AdapterError):
    status_code = 400
    err_type = "invalid_request_error"
    err_code = "invalid_parameter"


class ContentPolicyError(AdapterError):
    """内容审核 / 版权拦截 —— 换个提示词才有用，重试原样请求无意义。

    🔴 HTTP **451**（Unavailable For Legal Reasons，2026-10-09 用户口径）：
    "内容因合规原因不可用"正是这个码的语义 —— 与 400（请求本身写错）
    区分开，调用方能凭状态码直接分辨"参数错了"和"内容不让生成"。
    """

    status_code = 451
    err_type = "content_policy_violation"
    err_code = "content_policy_violation"


class RiskControlError(AdapterError):
    """命中上游风控。**retryable=False**：持续施压会延长标记。"""

    status_code = 429
    err_type = "risk_control_error"
    err_code = "risk_control_challenge"

    #: 上游原始 ret 码（如 `1018`）。**仅供服务内部判定冷却作用域**。
    #: 🔴 刻意**不做成 `detail`**：对外错误体不带内部枚举（2026-10-12 口径，
    #: 见 `docs/INTERFACE.md` §6）。派发层要按码分流（账号级 vs IP/设备级），
    #: 但调用方不需要知道上游的码。
    upstream_code: int | None = None
    retryable = False


class UpstreamRateLimitError(AdapterError):
    status_code = 429
    err_type = "rate_limit_error"
    err_code = "upstream_rate_limited"
    retryable = True


class QueueOverloadedError(AdapterError):
    """**本服务**的队列过载 —— 该凭据排队的任务已达上限。

    ⚠️ 与 `UpstreamRateLimitError`（**上游**限流）刻意分开：这个错跟即梦无关，
    是我们在**入队口**做的背压（2026-10-14 "队列堵死"的根治）。
    两者同为 429 且都可退避重试，但排查方向完全不同：
      · `queue_overloaded` ⇒ 等自己的任务跑完（或提并发），**上游是好的**；
      · `upstream_rate_limited` ⇒ 上游说慢点，**别加人**。
    """

    status_code = 429
    err_type = "rate_limit_error"
    err_code = "queue_overloaded"
    retryable = True


class UpstreamQuotaError(AdapterError):
    """积分/日额度耗尽 —— 与限流同为 429，但**重试无效**。"""

    status_code = 429
    err_type = "rate_limit_error"
    err_code = "upstream_quota_exhausted"
    retryable = False


class UpstreamUnavailableError(AdapterError):
    status_code = 502
    err_type = "upstream_error"
    err_code = "upstream_unavailable"
    retryable = True


class EmptyResultError(AdapterError):
    """上游报**成功**却**零产物** —— 本服务不交付空成功（用户口径 2026-10-10）。

    🔴 与 `UpstreamUnavailableError`（上游故障·可重试）刻意分开：
    零产物不是"上游打不通"，是"上游说好了却没交货" —— 调用方要的
    不是"可重试"的分类学，是一个**没有东西**的明确宣告。

    🔴 **HTTP 451**（2026-10-12 用户口径：空图多由上游内容审核所致）：
    审核把产物整批掐掉、但上游状态机仍报成功 —— 这种"报成功却交不出
    合规产物"归到"内容因合规原因不可用"一侧，用 451 让调用方一眼分流成
    **别原样重试**。`retryable=False` 与 451 一致（同 `ContentPolicyError`）。

    ⚠️ **刻意不复用 `ContentPolicyError`**，尽管对外状态码相同：
      · `err_type` / `err_code` 保持独立（`empty_result_error` /
        `upstream_empty_result`）—— 服务端日志与 `/stats` 才能把
        "上游真审核拒"与"上游空产物"分开看；
      · `service._advance` 的负缓存只在 `isinstance(err, ContentPolicyError)`
        时记录 ⇒ 空图**不进负缓存**（一次性的空产物不该把 prompt 钉死 24h）。
    同步端点（`POST /v1/images/generations`）按 `err_type` 精确识别它并抛
    这个码（`view()` 的"失败也回 200"只服务异步查询族，见 `app/main.py`）。
    """

    status_code = 451
    err_type = "empty_result_error"
    err_code = "upstream_empty_result"
    retryable = False


class UpstreamTimeoutError(AdapterError):
    status_code = 504
    err_type = "upstream_timeout_error"
    err_code = "upstream_timeout"
    retryable = True


class CapabilityUnavailableError(AdapterError):
    """上游凭据未配置 —— **部署问题，不是调用方的参数错误**，故 503 而非 401。"""

    status_code = 503
    err_type = "capability_unavailable"
    err_code = "upstream_not_configured"
    retryable = False


class CapabilityNotWiredError(AdapterError):
    """能力**已注册**，但服务里没有它的提交实现 —— 内部接线遗漏。

    🔴 刻意**不可重试**，而且必须与"上游异常"分开报。教训（2026-09-23 实测）：
    这条路径此前是 `assert cap.jimeng_tool` ⇒ 抛 `AssertionError`，被兜底逻辑
    归成 `upstream_unavailable`（**可重试**），于是：
      · `attempts` 白涨到 2（每次派发都再炸一次）；
      · 日志/响应里显示"上游不可用"，**排障方向被完全带偏**
        （真因是本地一行分支没写）。
    内部 bug 就该报成内部 bug。
    """

    status_code = 500
    err_type = "internal_error"
    err_code = "capability_not_wired"
    retryable = False


class AuthError(AdapterError):
    """调用方自己的 Key 不对（本服务的门），与上游凭据无关。"""

    status_code = 401
    err_type = "authentication_error"
    err_code = "invalid_api_key"
    retryable = False


class TaskNotFoundError(AdapterError):
    """任务不存在，或**不属于当前这把 Key**。

    刻意不区分这两种情况：区分开就等于告诉攻击者"这个 id 是存在的"。
    """

    status_code = 404
    err_type = "invalid_request_error"
    err_code = "task_not_found"
    retryable = False


class TaskStateError(AdapterError):
    """任务当前状态不允许这个操作（如对未完成任务做删除）。"""

    status_code = 409
    err_type = "invalid_request_error"
    err_code = "task_state_conflict"
    retryable = False


class SyncUnavailableError(AdapterError):
    """同步接口在当前部署形态下**无法履行** —— 没有任务的推进者。

    触发条件：后台协调器线程没有在跑（`COORDINATOR_ENABLED=0`、或启动异常）。
    此时同步等待注定熬到预算耗尽，还会让调用方误以为"任务在跑" ⇒
    快速 503 说清原因，而不是白等 300s。

    🔴 与 `CapabilityUnavailableError`（上游凭据未配）刻意分开：
    那说的是"上游打不了"，这里说的是"本服务没有执行者" ——
    排障方向与处置动作完全不同（配凭据 vs 开协调器 / 改用异步端点）。
    """

    status_code = 503
    err_type = "capability_unavailable"
    err_code = "sync_unavailable"
    retryable = False


__all__ = [
    "AdapterError", "InvalidParameterError", "ContentPolicyError",
    "RiskControlError", "UpstreamRateLimitError", "UpstreamQuotaError",
    "UpstreamUnavailableError", "UpstreamTimeoutError", "EmptyResultError",
    "CapabilityUnavailableError", "CapabilityNotWiredError",
    "SyncUnavailableError", "AuthError", "TaskNotFoundError",
    "TaskStateError",
]
