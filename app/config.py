#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jimeng-service 配置。

刻意不引入 pydantic-settings：全部配置项都有一处显式声明 + 一个默认值 +
一句「为什么是这个默认值」，用 stdlib 解析反而更好审计（依赖越少，行为面越小）。

两条纪律：
  1. **每个旋钮都必须有人读**（`tests/test_config.py::test_settings_knobs_are_all_wired`
     会逐条断言）—— 没人读的配置项就是假配置，会让运维以为改了会生效。
  2. **默认值必须能说出依据**：要么是实测值，要么是刻意的策略选择。
     拿不出依据的，宁可不给默认值（启动即报错）。
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on", "y", "t"}


class ConfigError(RuntimeError):
    """配置本身有问题 —— 启动即失败，绝不静默退回某个默认值。

    静默降级是最坏的一种失败：它会让「任务不丢」「鉴权开着」这类承诺
    在没人注意的时候悄悄失效。
    """


def _s(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None else v.strip()


def _i(key: str, default: int) -> int:
    raw = _s(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} 必须是整数，实得 {raw!r}") from e


def _f(key: str, default: float) -> float:
    raw = _s(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} 必须是数字，实得 {raw!r}") from e


def _b(key: str, default: bool) -> bool:
    raw = _s(key)
    if not raw:
        return default
    return raw.lower() in _TRUE


def _csv(key: str) -> tuple[str, ...]:
    raw = _s(key)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass
class Settings:
    # ------------------------------------------------------------ 上游凭据
    jimeng_cookie: str = ""
    jimeng_workspace_id: int | None = None
    jimeng_base_url: str = "https://jimeng.jianying.com"

    # ------------------------------------------------------------ 国际站（Dreamina SG）
    #: 🔴 2026-10-15 国际版适配。站点**常量**在 `upstream/jimeng/site.py`
    #: （域名/aid 等，不是部署配置）；这里只放**跟账号走**的三个值：
    #: `region` / `store-country-code`（国际版抓包样本账号是 PH 区，
    #: 别区账号用这两个覆盖）与工作台 id（国际版抓包带了 workspace_id）。
    #: 三个都可选：缺省 = 按抓包样本值 / 不带 workspace_id。
    jimeng_sg_region: str = ""
    jimeng_sg_store_cc: str = ""
    jimeng_sg_workspace_id: int | None = None

    #: 🔴 2026-10-15（用户口径："不展示degradations"）：**响应体默认不吐
    #: `degradations` 字段**。降级留痕照常**落库**（服务端日志/任务记录可见），
    #: 只是不再透出给调用方 —— 内部细节挡在服务端。`SHOW_DEGRADATIONS=1`
    #: 可恢复旧行为（调试/联调用）。
    show_degradations: bool = False

    #: 🔴 国际站（SG）写链路的浏览器代签桥（2026-10-15）：shark 风控要求
    #: 浏览器运行时会话 ⇒ SG 写请求转发 sg-bridge 的 UI 驱动（异步 handle），
    #: 轮询仍走直连读接口。空 = 未部署 bridge（SG 写请求按原路径走——会被
    #: shark 拒）。单实例部署形态：compose 内网 `http://sg-bridge:8300`。
    sg_bridge_url: str = ""

    # ------------------------------------------------- 国际站账号登录（2026-10-19）
    #: 🔴 用户口径："国际版即梦 增加鉴权方式 bearer 账号|密码"。
    #: 开 ⇒ Bearer 允许 `邮箱|密码` 形态：服务端调 CapCut passport 登录，
    #: 换回 sessionid 后走**原有全部链路**（对下游完全透明）。
    #: 关 ⇒ 该形态一律 401，提示改用 sessionid（保留开关是为了
    #: "密码进内存"这件事在合规上可一键关掉）。
    sg_login_enabled: bool = True
    #: 登录结果缓存 TTL（秒）。`0` = 用模块默认 12h。
    #: 上游 sessionid 本身 60 天（Max-Age=5184000），但风控/改密会提前失效，
    #: 缓存短一点换"自愈快"：最坏情况是每 12h 多一次登录往返。
    sg_login_cache_ttl: int = 0
    #: 缓存条目上限（LRU）。多账号部署可调大。
    sg_login_cache_max: int = 64

    # ------------------------------------------------------------ 对外鉴权
    #:🔴 2026-10-03 起**不再参与鉴权**（sessionid 透传：Bearer 就是凭据）。
    #: 保留字段仅为不破坏现有 `.env`；这里刻意**加一处读取**（见
    #: `auth_enabled` 的注释）以免它变成"看起来能配、其实没读"的假配置。
    api_keys: tuple[str, ...] = ()

    # ------------------------------------------------------------ 节奏闸门
    #: 同时在上游跑的生成任务数。默认 1 是策略选择（见 .env.example 的注释）。
    jm_concurrency: int = 1
    jm_min_interval: float = 0.0
    jm_per_minute: int = 0
    jm_cooldown: float = 600.0
    jm_max_wait: float = 120.0

    # ------------------------------------------------------------ 轮询
    #: 🔴 上游回执自带 `polling_config.interval_seconds = 30` —— 那才是它期望的节奏。
    #: 原值 2.0s 等于**比它密 15 倍**：既纯浪费请求，又是被限流的现实风险源。
    #: 取 10s 作折中（请求少 5 倍，成图检测延迟最多 +10s）；要更省可设 30。
    #: ⚠️ 改大它要同步满足 `COORDINATOR_LEASE >= 2×本值`（见下方校验）。
    jimeng_poll_interval: float = 10.0
    #: 建任务之后、**第一次轮询之前**的等待。默认 0.2s。
    #:
    #: 🔴 这个值曾经是 3.0，是整条链路上**最大的单点浪费**：实测一次超清任务
    #: 端到端 5.83s，其中 **3.0s（52%）** 纯粹是在等这个宽限期结束 ——
    #: 而上游其实在提交后 1s 就出图了。
    #:
    #: 为什么现在可以这么小：`fetch_many` 对"上游还没落库的 id"会返回
    #: `status=0 / init` 态的 **非终态**（不是错误），`poll_many` 见到非终态
    #: 只刷新时间戳、下一轮再问。所以**早问一次是零代价的**，晚问才是代价。
    #: 留 0.2s 只是为了别在提交返回的同一毫秒里去打（徒增一次空转）。
    poll_grace: float = 0.2
    task_timeout: float = 1800.0
    #: 🔴 2026-10-14（"队列堵死"根治）：**单凭据**允许排队的任务数上限。
    #: `0` = 不限（默认，保持既有行为不变）。
    #: 为什么要它：队列原先**没有任何背压** —— 实测一个调用方灌 181 条就把并发位
    #: 吃到"只有它的任务在跑"，别人的提交全排在后面等几小时（用户原话：
    #: "这么长，没看到请求打到 web"）。到达上限后新提交直接
    #: `429 queue_overloaded`（带 `Retry-After`）—— 把"排队几小时"
    #: 变成"立刻知道该等一会儿"，而不是先收下再让人干等。
    max_queued_per_credential: int = 0

    #: 🔴 2026-10-15（用户口径"减少无效重试"）：**受理去重** —— 同一凭据 +
    #: 同一请求指纹 + 上一条**仍在途**（queued / in_progress）⇒ 直接复用那条任务
    #: （不新建、**不重复计费**、不占新的队列位）。
    #: 为什么必须补：调用方（网关）把 `202 + task_id` 当成"没有图"去重投 ——
    #: 线上实测同一秒连发 4~5 条**一模一样**的请求，每条都建一个**真实生成任务**
    #: （每次都可能计费），还把队列灌满、让所有人的同步请求一起降级。
    #: ⚠️ **只覆盖"在途"**：上一条已到终态后照常新建 —— 那时的重复提交更可能是
    #: "我还想要一张"（要第二张请用 `n=2` 或换 `seed`），不是重试。
    #: 关掉（`ACCEPT_DEDUPE_ENABLED=0`）即回到"来多少建多少"的旧行为。
    accept_dedupe_enabled: bool = True
    #: 去重窗口（秒）：只认 `created_at >= now - 本值` 的在途任务。
    #: 默认 600 = 与 `TASK_TIMEOUT` 同量级（在途任务的自然寿命），
    #: 免得一条卡死的僵尸任务在它整个寿命里吞掉所有重复提交。**0 = 只看非终态**。
    accept_dedupe_window_s: int = 600
    #: 🔴 2026-10-21（线上实测）：**内容级去重**。
    #: 现场：调用方每次重试都把同一张图**重新上传**到 S3，拿到新的预签名 URL
    #: ⇒ 指纹（含 URL 原文）每次都不同 ⇒ 去重永不命中 ⇒ 同一个「2k 生成 +
    #: 超清」被受理 N 次、各跑一遍（页面上出现多条一样的 4K 结果）。
    #: 现在：指纹不命中时，用**不含图**的弱指纹找近邻，再比对**图片内容**。
    accept_dedupe_by_content: bool = True
    #: 内容级去重的回看窗口（秒）。比"在途"宽 —— 线上重试时前一条往往已经
    #: 成功（4K 只要 60~70s，调用方 60s 超时后重试）。默认 90s。
    content_dedupe_window_s: int = 90

    # ------------------------------------------------------------ 协调器
    coordinator_enabled: bool = True
    #: 🔴 **默认关**：自动续生成（`action=2`）的判据**尚未确定** ——
    #: 实测「有的 history 能续、有的不能」，而区分它们的条件**还没找到**
    #: （排除清单见项目记忆）。判据确定前**不要开**，否则就是按一个错判据
    #: 去花真实的生成额度。关掉时行为 = 退回「用成功的图补齐」。
    continue_enabled: bool = False
    coordinator_tick: float = 1.0
    coordinator_lease: float = 30.0

    # ------------------------------------------------- 4k 改道（2k + 超清）
    #: 🔴 2026-10-12（用户口径）：请求**清晰度 > 2k** 的图片任务一律改走
    #: 「2k 生成 + 超清就地放大」以降本。依据（`docs/UPSTREAM.md` §21）：
    #: · 4k 是**独立计费档**，直出明显更贵 —— 非会员账号上实测余额 17 时
    #:   **连提交都被 `ret=1006` 拒**（"积分不足**或**没有相关权益"）；
    #: · 超清**免费**（UI 标价 + 实测零出账 + 用户口径，三者吻合）；
    #: · 超清服务端实测**固定 2×**（源 2560×1440 → 5120×2880），比例与输入一致。
    #: 关掉即回到"直出请求档位"的旧行为（仍然照旧留痕告知 4k 更贵）。
    upscale_4k_enabled: bool = True

    #: 超清腿**失败后**允许的重试次数 —— **只作用于这条免费的腿**。
    #: 🔴 2026-10-12 收尾实测：新号 6 次放大里 **1 次**上游 `generate_failed`
    #: （`ret=0` 受理成功 + `item_list: []`；老号 4/4 成功，合计 9/10）⇒ **偶发**。
    #: 超清**免费**（两个账号账单实锤零出账，见 `docs/UPSTREAM.md` §21），
    #: 而它是"请求 >2k"的**唯一**清晰度来源 ⇒ 「先重试、再退回 2k」严格优于
    #: 「一失败就退回 2k」（边际成本 0，最坏多一次免费调用）。
    #: 只重试**瞬时**失败：提交抛异常 / 上游终态无产物；
    #: **确定性**的前置条件不满足（缺 `item_id`、id 非数字）不重试 —— 重试必然同样失败。
    #: 设 **0** = 回到「一失败即退 2k」的旧行为。
    upscale_max_retries: int = 1

    # ------------------------------------------------------------ 同步接口
    #: `POST /v1/images/generations`（创建 + 轮询合并）的**总等待预算**（秒）：
    #: 预算内到终态就直接回最终体；超预算降级回 `202 + task_id`（调用方转异步轮询）。
    #: 默认 300 是用户明确要求的墙钟上限。
    #: 🔴 必须**小于** worker/网关的超时：`GUNICORN_TIMEOUT`（gunicorn_conf.py，
    #: 默认 360）与生产 nginx 的 `proxy_read_timeout`（需 ≥ 300 + 余量）——
    #: 否则跑满预算的请求会被掐断，客户端拿到的是断连而不是我们构造的降级响应。
    sync_max_wait: float = 300.0
    #: 🔴 2026-10-21（用户口径）：同步链**快速失败** —— 队列最老等待已达同步预算时，
    #: 这单必然等不到结果，直接在受理口 429，别让它白等 300s。
    sync_backlog_guard: bool = True
    #: 🔴 2026-10-21（线上实测）：图片上传（ImageX 三段式）单段超时。
    #: 原先是**写死 60s** ⇒ 一次限流可以把协调器线程卡住 60s（`dispatch` 是
    #: 串行的），期间所有任务既不派发也不轮询。收紧到 20s：正常 10MB 图
    #: 1~3s 就传完，20s 已经宽松；超时快速失败比占着线程划算。
    upload_timeout: float = 20.0

    # -------------------------------------------------- 内容审核负缓存
    #: 🔴 2026-10-02：被上游审核拒绝过的 (能力, 模型, prompt, 输入图, 档位)
    #: 短期内**不再提交上游**。为什么：审核拒绝是**确定性**的，原样重试
    #: 必然再被拒，**而且每次都计费**（实测失败 message 里就写着
    #: "该任务已被上游计费"）⇒ 不缓存= 调用方反复重试、反复扣钱。
    #: TTL 默认 **24 小时**（2026-10-03 用户口径从 6h 调长）：审核策略很少变，
    #: 而重复提交同一违规素材的代价是**每次都计费**
    #: ⇒ 宁可拦久一点。设为 0 即**关闭**负缓存。
    neg_cache_ttl: float = 86400.0
    #: 有界 LRU 条数（无界缓存 = 内存泄漏）。
    neg_cache_max: int = 2048
    #: 🔴 2026-10-15 用户口径「**按道理是不拒绝客户请求的**」+「**4xx 的失败立即返回**」：
    #: 命中负缓存时**受理层不再拒绝**（不再在受理口抛 451），而是把那条任务
    #: **受理即终态**判成同一个 4xx 失败：
    #:   · `fail`（默认）：**不提交上游**（`upstream_submit_id` 为空 ⇒ 零计费），
    #:     但失败**立即**可见 —— 同步端点毫秒级回 451，不再排队/等预算/降级 202；
    #:   · `warn`：只在 `degradations` 留痕、**照常提交上游**
    #:     —— 彻底不拦，但会**重新为注定失败的请求付费**（调用方明确要"照常提交"时才用）。
    neg_cache_action: str = "fail"

    # ------------------------------------------------ prompt 决策预审（Jev）
    #: 🔴 2026-10-09：prompt **前置**内容审查闸（Jev 决策模型，见
    #: `prompt_guard.py`）。即梦没有文字预审接口，prompt 违规只能在提交后
    #: 被拒且照样计费 ⇒ 用决策模型在受理时就地拦。判定结果**缓存**
    #: （同 prompt 不重复调决策服务）；**远端失败默认放行**（fail-open，
    #: 与素材预审同一取向）+ degradations 留痕。
    guard_enabled: bool = True
    #: 🔴 决策服务地址与 Key。Key 走 env（`JEV_API_KEY`），**绝不硬编码**。
    jev_base_url: str = "https://jev.bocha.cn/v1"
    jev_api_key: str = ""
    jev_model: str = "bocha-jev-v1"
    jev_timeout_s: float = 8.0
    #: noul ≥ 阈值 ⇒ 拦。实测校准：违规全本 0.99 / 良性泳装 0.02，
    #: 0.5 两侧边距都足够宽（泛化问法只有 0.62，题目必须用审核口径版）。
    guard_block_threshold: float = 0.5
    guard_cache_ttl: float = 86400.0
    guard_cache_max: int = 4096

    # ------------------------------------------------------------ 持久化
    #: 任务库 DSN。生产用 PostgreSQL；`:memory:` 或文件路径（SQLite）留给测试与联调。
    task_db: str = "postgresql+psycopg://jimeng:jimeng@127.0.0.1:5432/jimeng"
    #: 连接池。任务接口本身很轻，池子不需要大；但协调器是长驻线程，
    #: `pre_ping` 必须开 —— 否则 PG 侧重启/空闲断开会让我们拿到死连接。
    task_db_pool_size: int = 5
    task_db_max_overflow: int = 10
    task_db_pool_recycle: int = 1800
    task_db_pool_pre_ping: bool = True
    task_db_connect_timeout: int = 10
    task_retention_days: int = 7

    # ------------------------------------------------------------ 输入图
    max_download_bytes: int = 32 * 1024 * 1024
    max_input_bytes: int = 20 * 1024 * 1024
    normalize_uploads: bool = True
    normalize_max_side: int = 4096
    normalize_max_bytes: int = 4 * 1024 * 1024

    # ------------------------------------------------------------ 可观测性
    #: Logfire write token。留空 = 只在本地留 span、不上报（启动时会说明原因）。
    otel_token: str = ""
    otel_service_name: str = "jimeng-service"
    otel_environment: str = ""
    #: 1 = 把上游原始报文绑成 span 属性（默认开：观测面**不脱敏**，
    #: 上游明细原样上报 —— 见 observability 模块 docstring 的口径一节）。
    otel_capture_upstream: bool = True
    #: 脱敏开关。**默认 0（不脱敏）**。
    #: ⚠️ 打开它只会启用 logfire SDK 自带的 scrubber，而那个 scrubber 按
    #: **值子串**命中 `credential`/`token`/`auth`，会把即梦的 TOS 预签名产物 URL
    #: （必含 `X-Tos-Credential=`）整条打成 `[Scrubbed due to 'Credential']`
    #: ⇒ 面板直接不可读。本模块自身**在任何设置下都不改写上报内容**。
    otel_scrubbing: bool = False

    # ------------------------------------------------------------ 服务
    host: str = "0.0.0.0"
    port: int = 8200
    log_level: str = "INFO"

    #: 装配期一次性算出的告警（启动日志里打出来），便于测试断言
    startup_warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 派生

    @property
    def upstream_configured(self) -> bool:
        """服务是否具备"受理并推进任务"的能力。

        🔴 2026-10-08 语义变更（用户口径"**不从环境变量取 sessionid，
        是用 bearer <key> 鉴权**"）：**恒为 True**。

        为什么必须恒真 —— 原来它判`bool(JIMENG_SESSIONID or COOKIE)`，
        而这个开关卡着**三处**要害：
        · `coordinator.tick()` 里`if not upstream_configured: return`
          ⇒ env 空着**协调器整轮不推进**，任务永远 queued；
        · `Service.create` 受理时直接抛 `capability_unavailable`；
        · 启动自检还会报"JIMENG_SESSIONID 未配置"。

        透传后凭据由**每个请求的 Bearer** 带来，服务自己不需要持有⇒
        用 env 是否为空来判断"能不能干活"是**错的**（env 空 ≠ 不能干活）。
        真实可用性现在体现在**派发时**：拿不到凭据会报
        `upstream_not_configured`，那才是准确的位置。
        """
        return True

    @property
    def auth_enabled(self) -> bool:
        """是否启用对外鉴权。**恒为 True**（2026-10-03sessionid 透传）。

        🔴 实踩的坑：原先是 `bool(self.api_keys)`。我在清理线上 `.env` 时把
        `API_KEYS` 整行注释掉了 ⇒ 它变False ⇒ `require_key` 走
        `credential_of(None)` ⇒ **Bearer 被完全忽略**、所有任务以
        `anonymous` 落库 ⇒ 跨凭据隔离失效（谁都能查谁的任务）。
        而现象是"生图请求挂住到超时"，**日志里没有任何报错**，
        协调器 ticks 也正常 ⇒ 极难定位。

        为什么恒真：透传模式下"鉴权"**已由 Bearer 本身承担**
        （它就是上游凭据），不需要任何额外配置项决定是否开启。
        `api_keys` 字段保留仅为不破坏现有 `.env`（它已不参与鉴权）。
        """
        return True

    @property
    def db_target(self) -> str:
        """交给 `TaskStore` 的 PostgreSQL DSN。"""
        return self.task_db

    def replace(self, **kw) -> "Settings":
        """返回一个改了若干字段的副本。

        ⚠️ dataclass 没有 Pydantic 的 `model_copy()` —— 本仓用 dataclass，
        所以派生配置（测试里造"未配凭据"这类部署状态、运维脚本做变体）统一用它。
        `validate()` 不会被自动重跑：改完校验类字段请自行再调一次。
        """
        return dataclasses.replace(self, **kw)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls) -> "Settings":
        ws_raw = _s("JIMENG_WORKSPACE_ID")
        st = cls(
            jimeng_cookie=_s("JIMENG_COOKIE"),
            jimeng_workspace_id=int(ws_raw) if ws_raw else None,
            jimeng_base_url=_s("JIMENG_BASE_URL", "https://jimeng.jianying.com"),
            jimeng_sg_region=_s("JIMENG_SG_REGION", ""),
            jimeng_sg_store_cc=_s("JIMENG_SG_STORE_CC", ""),
            jimeng_sg_workspace_id=(
                int(sgws) if (sgws := _s("JIMENG_SG_WORKSPACE_ID", "")) else None),
            show_degradations=_s("SHOW_DEGRADATIONS", "") in ("1", "true", "yes"),
            sg_bridge_url=_s("SG_BRIDGE_URL", "").rstrip("/"),
            sg_login_enabled=_b("SG_LOGIN_ENABLED", True),
            sg_login_cache_ttl=_i("SG_LOGIN_CACHE_TTL", 0),
            sg_login_cache_max=_i("SG_LOGIN_CACHE_MAX", 64),
            api_keys=_csv("API_KEYS"),
            jm_concurrency=_i("JM_CONCURRENCY", 1),
            jm_min_interval=_f("JM_MIN_INTERVAL", 0.0),
            jm_per_minute=_i("JM_PER_MINUTE", 0),
            jm_cooldown=_f("JM_COOLDOWN", 600.0),
            jimeng_poll_interval=_f("JIMENG_POLL_INTERVAL", 10.0),
            poll_grace=_f("POLL_GRACE", 0.2),
            task_timeout=_f("TASK_TIMEOUT", 1800.0),
            max_queued_per_credential=_i("MAX_QUEUED_PER_CREDENTIAL", 0),
            accept_dedupe_enabled=_b("ACCEPT_DEDUPE_ENABLED", True),
            accept_dedupe_by_content=_b("ACCEPT_DEDUPE_BY_CONTENT", True),
            content_dedupe_window_s=_i("CONTENT_DEDUPE_WINDOW_S", 90),
            accept_dedupe_window_s=_i("ACCEPT_DEDUPE_WINDOW_S", 600),
            coordinator_enabled=_b("COORDINATOR_ENABLED", True),
            continue_enabled=_b("CONTINUE_ENABLED", False),
            upscale_4k_enabled=_b("UPSCALE_4K_ENABLED", True),
            upscale_max_retries=_i("UPSCALE_MAX_RETRIES", 1),
            coordinator_tick=_f("COORDINATOR_TICK", 1.0),
            coordinator_lease=_f("COORDINATOR_LEASE", 30.0),
            sync_max_wait=_f("SYNC_MAX_WAIT", 300.0),
            sync_backlog_guard=_b("SYNC_BACKLOG_GUARD", True),
            upload_timeout=_f("UPLOAD_TIMEOUT", 20.0),
            neg_cache_ttl=_f("NEG_CACHE_TTL", 86400.0),
            neg_cache_max=int(_f("NEG_CACHE_MAX", 2048)),
            neg_cache_action=_s("NEG_CACHE_ACTION", "fail").strip().lower(),
            guard_enabled=_b("PROMPT_GUARD_ENABLED", True),
            jev_base_url=_s("JEV_BASE_URL", "https://jev.bocha.cn/v1"),
            jev_api_key=_s("JEV_API_KEY"),
            jev_model=_s("JEV_MODEL", "bocha-jev-v1"),
            jev_timeout_s=_f("JEV_TIMEOUT", 8.0),
            guard_block_threshold=_f("GUARD_BLOCK_THRESHOLD", 0.5),
            guard_cache_ttl=_f("GUARD_CACHE_TTL", 86400.0),
            guard_cache_max=int(_f("GUARD_CACHE_MAX", 4096)),
            task_db=_s("TASK_DB",
                       "postgresql+psycopg2://jimeng:jimeng@127.0.0.1:5432/jimeng"),
            task_retention_days=_i("TASK_RETENTION_DAYS", 7),
            max_download_bytes=_i("MAX_DOWNLOAD_BYTES", 32 * 1024 * 1024),
            max_input_bytes=_i("MAX_INPUT_BYTES", 20 * 1024 * 1024),
            normalize_uploads=_b("NORMALIZE_UPLOADS", True),
            normalize_max_side=_i("NORMALIZE_MAX_SIDE", 4096),
            normalize_max_bytes=_i("NORMALIZE_MAX_BYTES", 4 * 1024 * 1024),
            otel_token=_s("LOGFIRE_TOKEN"),
            otel_service_name=_s("OTEL_SERVICE_NAME", "jimeng-service"),
            otel_environment=_s("LOGFIRE_ENVIRONMENT"),
            otel_capture_upstream=_b("OTEL_CAPTURE_UPSTREAM", True),
            otel_scrubbing=_b("OTEL_SCRUBBING", False),
            host=_s("HOST", "0.0.0.0"),
            port=_i("PORT", 8200),
            log_level=_s("LOG_LEVEL", "INFO").upper(),
        )
        st.validate()
        return st

    # ------------------------------------------------------------------ 校验

    def validate(self) -> None:
        if self.jm_concurrency < 1:
            raise ConfigError("JM_CONCURRENCY 必须 >= 1")
        if self.task_timeout <= 0:
            raise ConfigError("TASK_TIMEOUT 必须 > 0")
        if self.max_queued_per_credential < 0:
            raise ConfigError("MAX_QUEUED_PER_CREDENTIAL 必须 >= 0（0 = 不限）")
        if self.neg_cache_action not in ("fail", "warn"):
            raise ConfigError("NEG_CACHE_ACTION 只能是 fail / warn"
                              "（fail = 受理不拒但立即终态失败、不提交；"
                              "warn = 只留痕、照常提交）")
        if self.accept_dedupe_window_s < 0:
            raise ConfigError(
                "ACCEPT_DEDUPE_WINDOW_S 必须 >= 0（0 = 只看非终态）")
        if self.jimeng_poll_interval <= 0:
            raise ConfigError("JIMENG_POLL_INTERVAL 必须 > 0")
        if self.task_retention_days < 1:
            raise ConfigError("TASK_RETENTION_DAYS 必须 >= 1")
        if self.task_db_pool_size < 1:
            raise ConfigError("TASK_DB_POOL_SIZE 必须 >= 1")
        if self.normalize_max_side < 64:
            raise ConfigError("NORMALIZE_MAX_SIDE 太小（<64），会毁图")
        if self.coordinator_lease < self.jimeng_poll_interval * 2:
            # 租约比一次轮询还短 ⇒ 每轮都换主，等于没有选主
            raise ConfigError("COORDINATOR_LEASE 必须 >= 2×JIMENG_POLL_INTERVAL")
        if self.sync_max_wait <= 0:
            raise ConfigError("SYNC_MAX_WAIT 必须 > 0")

        self.startup_warnings = []
        # 🔴 2026-10-03：原先这里对"API_KEYS 为空"告警（"对外鉴权已关闭"）。
        # 透传后 `auth_enabled` 恒真 ⇒ 那个分支永不成立，留着就是**误导**。
        #
        # 下面这行是**真实读取**（不是凑数）：`api_keys` 已不参与鉴权，
        # 但 `.env` 里往往还留着它 —— 明确告诉运维"这行已无效、别再改它"，
        # 比留一个没人读的字段让人反复纠结要好。
        if self.api_keys:
            self.startup_warnings.append(
                "API_KEYS 已被 sessionid 透传取代 —— 该配置**不再参与鉴权**"
                "（凭据 = Authorization: Bearer <sessionid>），可以删掉了。")

        if self.jm_concurrency > 4:
            self.startup_warnings.append(
                f"JM_CONCURRENCY={self.jm_concurrency} 超过实测验证过的上限（4）。"
                "上游是否容忍未知，且并发直接放大积分消耗速率与风控暴露面。")
        # 同步预算 vs worker 超时的**交叉校验**（两个值都在 env 里，能对就对）。
        # GUNICORN_TIMEOUT 不设时 gunicorn_conf.py 用默认 360（> 300 天然安全），
        # 所以只校验"显式设了、且不大于同步预算"这种必然出事的组合。
        gw_raw = _s("GUNICORN_TIMEOUT")
        if gw_raw:
            try:
                gw = float(gw_raw)
            except ValueError:
                gw = 0.0
            if 0 < gw <= self.sync_max_wait:
                self.startup_warnings.append(
                    f"GUNICORN_TIMEOUT={gw_raw} 不大于 SYNC_MAX_WAIT="
                    f"{self.sync_max_wait}：跑满预算的同步请求会被 worker 判超时"
                    f"杀掉（客户端拿到断连，而不是降级响应）。"
                    f"请把 GUNICORN_TIMEOUT 提到同步预算之上。")


__all__ = ["Settings", "ConfigError"]
