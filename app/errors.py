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


def quota_exhausted(quiet_s: float = 0.0, *,
                    waited_s: float | None = None,
                    upstream: str = "jimeng") -> UpstreamQuotaError:
    """「这个号没额度」的**统一**错误对象（三个出口共用，文案不许漂移）。

    🔴 2026-10-21（用户口径「积分不足应该立刻报错返回」）三处调用：
      · `Service.create` —— 受理口直接拒（不建任务、不排队、零计费）；
      · `main._sync_generate` —— 同步面**早于**积压闸，保证错误码精确；
      · `Coordinator._fail_stale_quota_task` —— 把静默期里遗留的僵尸 queued
        判失败（否则它会顶高 `oldest_wait_s`，让全站同步请求 429）。

    `waited_s` 给了 = 僵尸清账口径（任务已被判失败）；不给 = 受理拒绝口径。
    两种口径都刻意**不带 `retry_after`**：额度是按上游重置的，编一个"等 N 秒
    再来"就是伪造事实（且会诱导调用方做无效重试 —— `retryable=False`）。

    🔴 **文案末尾那句英文是给 new-api 的自动禁用钩子，别删**（2026-10-21）：
    new-api 的 `AutomaticDisableStatusCodeRanges` 默认**只有 401**
    （429 不在内），所以"额度耗尽 ⇒ 网关自动禁用该渠道"只能靠
    `AutomaticDisableKeywords` 兜 —— 默认列表里正是 OpenAI 那句
    `You exceeded your current quota`（匹配是 **message 子串 + 不区分大小写**，
    见 `service.ShouldDisableChannel`）。没有这句的症状：号已经没额度，
    网关却还继续把请求派进来，每条都白跑一趟（静默失效，最难查）。
    ⚠️ 前提：new-api 侧 `AutomaticDisableChannelEnabled`（运营设置→监控）
    与渠道 `auto_ban` 都开着。
    """
    if waited_s is not None:
        return UpstreamQuotaError(
            f"该账号的即梦积分/日额度已耗尽（上游 ret=1006：积分不足或没有"
            f"相关权益）：排队已等满 {waited_s:.0f}s 仍无法提交 ⇒ 判失败。"
            f"**重试不会变好**，需充值或等额度重置。"
            f"（You exceeded your current quota）",
            upstream=upstream)
    return UpstreamQuotaError(
        f"该账号的即梦积分/日额度已耗尽（上游 ret=1006：积分不足或没有"
        f"相关权益），**重试不会变好** —— 本服务已对该凭据静默 "
        f"{quiet_s:.0f}s，期间不再向它提交任何生成。"
        f"请充值，或等额度重置后再试。"
        f"（You exceeded your current quota）",
        upstream=upstream)


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

    🔴 **HTTP 500**（2026-10-21 用户口径："同步接口不要返回空图或者 `{}`，
    这个直接报 500"）。旧的 451（2026-10-12 口径：空图多由上游审核所致）
    **作废** —— 451 会被一部分网关归到"内容合规"一侧：既不重试、也不报错，
    甚至有客户端把它渲染成空响应 `{}`，调用方照样读成"拿到了东西"。
    空产物是**服务端没交付**，就该按服务端错报：500 不会被误读。
    `retryable=False` 保持不变（同样的 prompt 再打一次，多半还是空）。

    ⚠️ **刻意不复用 `ContentPolicyError`**（两者现在都是失败，但码不同）：
      · `err_type` / `err_code` 保持独立（`empty_result_error` /
        `upstream_empty_result`）—— 服务端日志与 `/stats` 才能把
        "上游真审核拒"与"上游空产物"分开看；
      · `service._advance` 的负缓存只在 `isinstance(err, ContentPolicyError)`
        时记录 ⇒ 空图**不进负缓存**（一次性的空产物不该把 prompt 钉死 24h）。
    同步端点（`POST /v1/images/generations`）按 `err_type` 精确识别它并抛
    这个码（`view()` 的"失败也回 200"只服务异步查询族，见 `app/main.py`）。
    """

    status_code = 500
    err_type = "empty_result_error"
    err_code = "upstream_empty_result"
    retryable = False


class SyncTimeoutError(AdapterError):
    """**同步等待**预算耗尽、但任务仍在跑 —— 504（2026-10-21 用户口径）。

    口径："同步任务就别 202 了" —— 同步调用方是一锤子买卖，202 给它的是
    "既不是结果也不是错误"的东西，还得自己实现转异步轮询。

    🔴 与 `UpstreamTimeoutError` 刻意分开：那个是"上游迟迟不回话"，
    这个是"上游好好的、只是没在 `SYNC_MAX_WAIT` 内跑完"。
    任务**没丢、没取消**：message 里带 task_id，可去异步端点接着查；
    原样重试会命中**受理去重**（同凭据 + 同指纹 + 上一条仍在途）
    ⇒ 复用同一条任务，不会重复计费。
    """

    status_code = 504
    err_type = "timeout_error"
    err_code = "sync_timeout"
    retryable = True


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
