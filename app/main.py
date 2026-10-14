#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 装配：路由 + 统一错误信封 + lifespan（协调器）+ 埋点接线。

## 对外契约（冻结，见 `docs/INTERFACE.md`）

```
POST   /async/v1/images/generations         受理，只回一个 task_id
GET    /async/v1/images/generations/{id}    非终态回排队态；终态回 {data, created, usage}
POST   /v1/images/generations               同步：创建+轮询合并，预算内直接回结果
GET    /v1/models                           模型清单（OpenAI 形态）—— 🔓 免鉴权
```

🔴 **前缀即语义**（2026-09-23 起）：`/v1/*` = 同步语义，`/async/*` = 异步任务语义。
  · `GET /v1/models` —— 同步、无任务语义；**免鉴权**（发现性端点，见该路由 docstring）；
  · `POST /v1/images/generations` —— **真同步**生成：创建+轮询合并进一个请求，
    预算内（`SYNC_MAX_WAIT`，默认 300s）直接回最终结果；超预算降级回
    `202 + task_id`，调用方无缝转异步轮询。
`/async` 前缀留给**异步任务**语义（受理/查询/删除），它是那道护栏 ——
拦的是"把**异步**语义的端点挂到 `/v1` 被误读成同步 API"，不是禁止同步端点进 `/v1`。

`GET /healthz` 是**运维端点**，不属于对外契约：它零依赖、不触上游、不消耗积分
（容器 HEALTHCHECK 每 30s 打它）。它**既不上报 span、也不留日志** ——
两条通道都从 `observability.PROBE_PATHS` 一张表派生。
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from . import models
from .ark import ark_task_view as ark_view
from .ark import translate_ark_create as ark_translate
from .config import Settings
from .llms_txt import render as render_llms_txt
from .coordinator import Coordinator
from .errors import (
    AdapterError,
    AuthError,
    ContentPolicyError,
    EmptyResultError,
    InvalidParameterError,
    SyncUnavailableError,
)
from .observability import (
    OBS,
    excluded_urls,
    is_probe_path,
    setup_logging,
    should_log_path,
)
from .service import Service, view
from .store import TaskRecord

# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class GenerationRequest(BaseModel):
    """`POST /async/v1/images/generations` 的请求体。

    刻意 `extra="allow"`：未知/已知但不支持的字段由 `Service.create` 统一裁决
    （它分得清"认得但做不到"（进 `degradations`）与"写错了"（400）），
    在 schema 层报错就拿不到这个区分。

    🔴 **字段类型刻意宽松**（`Any` / 可空），把严格性全部交给 `Service.create`。
    原因：schema 层的报错是 Pydantic 的 422，格式固定且**不可执行** ——
    调用方传 `"image": "https://…"`（很常见的写法）会拿到一条机器味儿的
    `Input should be a valid list`，而不是我们那条"请写 `"image": ["…"]`"的提示。
    同理，`n: null` 这类"显式给了空值"必须等价于"没给"（用默认），
    不能因为 schema 填了个 `None` 就被判成参数错误。
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = Field(default=None, description="能力/模型名，如 jimeng-t2i（留空则按有无输入图推导）")
    prompt: str | None = Field(default=None, description="提示词")
    image: Any = Field(
        default=None,
        description="输入图：单张可传字符串（\"https://…\"），多张传数组"
                    "（[\"https://…\"]）；文生图可留空（或传 []）")
    size: str | None = Field(
        default=None,
        description="如 2048x2048（宽x高，像素）；也接受档位写法 "
                    "1k/1.5k/2k/4k（按 1:1 展开）")
    n: int | None = Field(default=None, description="出图张数（会吸附到该模型声明的合法取值）")
    seed: int | None = None
    negative_prompt: str | None = None


# （`VideoGenerationRequest` 已随 `/async/v1/videos` 端点一并移除 —— 2026-09-24）


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def _service(request: Request) -> Service:
    return request.app.state.service


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    if parts[0].lower() == "bearer":
        # 🔴 `Authorization: Bearer`（**只有 scheme、没有 token**）落到这里：
        # `split(None, 1)` 把尾随空白吃掉了 ⇒ 只剩 `["Bearer"]`。必须显式判掉，
        # 否则会把**字面量 `"Bearer"`** 当成一个 sessionid 凭证收下
        # （不是漏洞 —— 上游必然拒它 —— 但"空 token 变成合法凭证"明显反直觉，
        # 且让排障时看到一堆以 `Bearer` 为凭据的失败任务）。
        return None
    # 另一种合法形态：直接把 sessionid 明文放进 `Authorization`（不带 Bearer 前缀）。
    return raw.strip() or None


def require_key(request: Request) -> str:
    """校验调用方 Key，返回**凭证指纹**（不是明文）。

    · 未配置 `API_KEYS` ⇒ 鉴权关闭（dev），返回 `"anonymous"`；
    · 配置了 ⇒ 必须带 Bearer，且必须在白名单里。

    🔴 **所有业务端点（任何方法）都走它** —— 2026-09-23 收紧了此前的
    "GET 单条任务可不带 Key（`task_id` 即凭据）"放宽口径；门禁已从"只看 GET"
    升级为**全方法**扫描（POST/DELETE 才是会产生费用的写路径），理由见
    `get_generation` 的 docstring。**不鉴权的例外有两个**：
      · `/healthz` `/readyz` —— 判活必须无凭据可用；
      · `/v1/models` —— 发现性端点（2026-09-23 用户指令）：客户端在配置 Key
        之前就该能探清单（内容不含任何任务/凭据/内部状态）。
    例外名单由 `tests/test_api.py::test_every_business_route_requires_a_bearer`
    钉死。
    """
    settings: Settings = request.app.state.settings
    key = _bearer(request)
    if not settings.auth_enabled:
        return request.app.state.service.credential_of(None)
    if not key:
        raise AuthError("缺少 Authorization: Bearer <sessionid>")
    # 🔴 2026-10-03（用户口径"鉴权用 SESSIONID 就行"，纯透传）：
    # Bearer **就是即梦 sessionid**，直接当上游凭据用。
    # 为什么不再查白名单：
    # · 调用方换 sessionid 是**常态**（登录态会过期、要换号），白名单
    #   意味着"每次换号都要改服务配置"，这与服务化网关的定位冲突；
    # · 凭据本来就是调用方**自己带**的，服务不持有、不分发。
    # ⚠️ 代价（知情接受）：**任何持有即梦登录态的人都能调这个服务**，
    # 计费与风控都归他自己（我们的服务不代付）。
    #保留 `API_KEYS` 配置项但不再用于校验—— 免得它变成"看起来在生效、
    # 其实没读"的假配置（比没有更坏）。
    return request.app.state.service.credential_of(key)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        app.state.coordinator.start()
        try:
            yield
        finally:
            app.state.coordinator.stop()
            app.state.service.close()
            # 短命进程必须显式 flush，否则退出时最后一批 span 直接丢
            OBS.flush()
            logger.info("jimeng-service 已停止")

    # 版本号**单一事实源**在 app/__init__.py 的 __version__（发版流程只改那里）；
    # 这里曾经硬编码 "0.1.0"，与 __version__ 形成两处漂移 —— openapi.json 报旧版本就是它。
    from . import __version__  # noqa: PLC0415

    app = FastAPI(
        title="jimeng-service",
        version=__version__,
        description="即梦（jimeng.jianying.com）图片/视频生成的**异步**出口。",
        lifespan=lifespan,
    )

    service = Service(settings)
    app.state.settings = settings
    app.state.service = service
    app.state.coordinator = Coordinator(service, settings)

    if not service.store.ping():
        # 任务库是**事实源**：连不上就别装作能服务。启动期报错比运行期
        # "任务存不进去"要早得多，也便宜得多。刻意不做「连不上就退回内存」的降级。
        raise RuntimeError(
            f"任务库（PostgreSQL）连不上：{service.store.dsn}。"
            f"请检查 TASK_DB 与网络连通性。")

    _wire_observability(app, settings)
    _install_error_handlers(app)
    _install_request_logging(app)
    _install_routes(app)

    for w in settings.startup_warnings:
        logger.warning(w)
    logger.info("jimeng-service 装配完成 | " + json.dumps(service.status(),
                                                         ensure_ascii=False))
    return app


def _wire_observability(app: FastAPI, settings: Settings) -> None:
    """logfire + loguru 接线。**失败绝不影响服务启动。**"""
    try:
        OBS.init(settings)
        setup_logging(settings.log_level, obs=OBS)
    except Exception as e:  # noqa: BLE001
        print(f"[observability] 装配失败，已忽略：{type(e).__name__}: {e}")
        setup_logging(settings.log_level, obs=None)
        return

    if not OBS.sdk_configured:
        return
    try:
        import logfire  # noqa: PLC0415
        from .observability import PROBE_PATHS  # noqa: PLC0415

        # `excluded_urls` 由**路径表机械生成**（别手写：它是正则且上游用
        # `re.search` 子串匹配，写 "/" 会命中每一个 URL ⇒ 全站追踪静默关闭）
        url_regex = excluded_urls(PROBE_PATHS)
        logfire.instrument_fastapi(app, excluded_urls=url_regex,
                                   capture_headers=False)
        logger.debug(f"探活路径已从 span 中摘除：{url_regex}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"instrument_fastapi 失败，已忽略：{type(e).__name__}: {e}")


def _install_error_handlers(app: FastAPI) -> None:
    """所有 `AdapterError` → 统一错误信封。**HTTP 状态码来自错误类本身。**"""

    @app.exception_handler(AdapterError)
    async def _adapter_error(_r: Request, exc: AdapterError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            # `Retry-After` 是事实：上游说多久就多久。**没说就不给这个头**
            # —— 编一个数字等于伪造它。
            headers["Retry-After"] = str(int(max(1, round(exc.retry_after))))
        return JSONResponse(status_code=exc.status_code, content=exc.to_error(),
                            headers=headers)

    @app.exception_handler(InvalidParameterError)
    async def _invalid(_r: Request, exc: InvalidParameterError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_error())


def _install_request_logging(app: FastAPI) -> None:
    """每个请求一行摘要。**探活路径不打**（与 span 侧同源判据）。"""

    @app.middleware("http")
    async def _log_request(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        path = request.url.path
        # 探活既不上报 span、也不留日志 —— 只摘 span 会留下一半噪音
        quiet = not should_log_path(path) or is_probe_path(path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            if not quiet:
                logger.bind(request_id=rid, http_path=path).exception(
                    f"{request.method} {path} -> 未处理异常")
            raise
        response.headers["X-Request-Id"] = rid
        if not quiet:
            ms = round((time.perf_counter() - started) * 1000, 1)
            logger.bind(request_id=rid, http_path=path,
                        http_status=response.status_code,
                        duration_ms=ms).info(
                f"{request.method} {path} -> {response.status_code} ({ms}ms)")
        return response


def _chat_sse(cid: str, model: str, created: int,
              produce: Callable[[], str]) -> Iterator[str]:
    """**图片 chat** 的流式出口：把一次生成的文本按 `chat.completion.chunk` 吐出。

    🔴 形态必须逐字对齐 OpenAI SSE（`data: {json}\\n\\n` + 结尾 `data: [DONE]`）：
    客户端（new-api / LobeChat / NextChat…）是**按行扫 `data: ` 前缀**解析的，
    给它一坨普通 JSON，它会扫完全文、一个 chunk 都取不到 ⇒ 显示"空回复"。
    这正是 2026-10-14 线上那条"用 seedream 名测 chat 得到空回复"的直接成因。

    🔴 **先吐 role 帧、再去生成**：生成要几十秒，客户端在等首字节 —— 先发一帧
    证明"连接活着、正在生成"，避免客户端读超时提前断开。`produce` 是**阻塞**的
    （内部会跑完整个同步链路），由 Starlette 在线程池里迭代本生成器，不卡事件循环。

    ⚠️ 流式下响应头一旦发出就**改不动状态码** ⇒ 生成失败只能把错误**当正文**
    吐出去（客户端至少看得到原因，而不是一片空白）；非流式路径则照常抛 HTTP 错误。
    """
    def _ev(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    base: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": model}
    yield _ev({**base, "choices": [{"index": 0,
                                    "delta": {"role": "assistant", "content": ""},
                                    "finish_reason": None}]})
    try:
        text = produce()
    except AdapterError as e:
        text = f"生成失败：{e.to_error()['error']['message']}"
    except Exception as e:  # noqa: BLE001
        text = f"生成失败：{type(e).__name__}: {e}"
    yield _ev({**base, "choices": [{"index": 0, "delta": {"content": text},
                                    "finish_reason": None}]})
    yield _ev({**base, "choices": [{"index": 0, "delta": {},
                                    "finish_reason": "stop"}]})
    yield "data: [DONE]\n\n"


def _chat_text(rec: TaskRecord) -> str:
    """把一次生成的结果渲染成 chat 的 `content`（**Markdown 图片**）。

    走 chat 协议的图片模型，产物就是 `![](url)` —— 客户端（LobeChat/NextChat 等）
    会在对话流里直接渲染成图。多张就多行。
    """
    if rec.status != "success":
        err = (rec.error or {}).get("message") or "任务未成功结束"
        return f"生成失败：{err}"
    urls = [i.get("url") for i in (rec.images or []) if i.get("url")]
    if not urls:
        return "生成失败：上游没有返回任何图片（空产物）。"
    return "\n".join(f"![image]({u})" for u in urls)


def _extract_chat_inputs(body: dict[str, Any]) -> tuple[str, list[str]]:
    """从 OpenAI `messages` 里取出**最后一条 user 的文本**与**全部输入图**。

    图片 chat 的两种入口都在这里收敛：
      · 纯文本 messages（`content: "画一只猫"`）⇒ prompt，无输入图 ⇒ 文生图；
      · 多模态 messages（`content: [{"type":"text",…},{"type":"image_url",…}]`）
        ⇒ 文本进 prompt、图片进垫图列表 ⇒ 图生图。

    图片只认 OpenAI 的 `image_url` 形态（`{"url": …}` 或裸字符串）；
    `data:` URL 也照收 —— 下游 `media.load_one` 本来就吃 data URI。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return "", []
    prompt = ""
    images: list[str] = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        parts = content if isinstance(content, list) else [content]
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict):
                if part.get("type") == "image_url" or "image_url" in part:
                    iu = part.get("image_url")
                    url = iu.get("url") if isinstance(iu, dict) else iu
                    if isinstance(url, str) and url.strip():
                        images.append(url.strip())
                elif part.get("text") is not None:
                    texts.append(str(part.get("text")))
            elif isinstance(part, str):
                texts.append(part)
        joined = "\n".join(t.strip() for t in texts if t and t.strip()).strip()
        if joined:
            prompt = joined          # 取**最后一条**有文本的 user 消息
    return prompt, images


def _sync_generate(svc: Service, coordinator: Any, settings: Settings,
                   body: dict[str, Any], *, credential: str,
                   sessionid: str | None) -> TaskRecord:
    """**同步**跑完一次生成：受理 → 叫醒推进 → 等到终态。返回 `TaskRecord`。

    多个门面共用这一个核心（`/v1/images/generations` 同步出图、图片 chat）——
    语义只有一处定义，避免"某个门面少判了一种失败"这类分叉。

    ⚠️ **阻塞**：会等到终态或 `SYNC_MAX_WAIT`（默认 300s）。调用它的 HTTP 端点
    必须是同步 `def`，或在线程池里跑（见各调用处）。

    终态失败里两种直接转抛 HTTP 错误（2026-10-10 用户口径，判据是**落库的**
    `error.type`，不做 message 字符串匹配）：
      · **内容审核拒绝** → 451 `ContentPolicyError`；
      · **空图**（上游报成功却零产物）→ 451 `EmptyResultError`。
    其余失败保持"任务完成了、结果是失败"的语义，返回 rec 由调用方渲染。
    """
    if not coordinator.running:
        # 配置只说明意图，线程活着才算数（与 Coordinator.running 同口径）：
        # 没有推进者就快速 503，别让调用方白等一整个预算。
        raise SyncUnavailableError(
            "后台协调器未在运行（COORDINATOR_ENABLED=0？）—— 没有任务推进者，"
            "同步等待无法履行。请改用异步端点 POST /async/v1/images/generations，"
            "或开启协调器后重试。")
    rec = svc.create(body, credential=credential, sessionid=sessionid)
    coordinator.wake()
    rec = svc.wait_terminal(rec.task_id, credential,
                            max_wait=settings.sync_max_wait)
    if rec.status == "failure":
        stored = rec.error or {}
        etype = stored.get("type")
        if etype == ContentPolicyError.err_type:
            raise ContentPolicyError(
                stored.get("message") or "内容审核未通过",
                **(stored.get("detail") or {}))
        if etype == EmptyResultError.err_type:
            raise EmptyResultError(
                stored.get("message") or "上游报成功但零产物",
                **(stored.get("detail") or {}))
    return rec


def _install_routes(app: FastAPI) -> None:
    # ------------------------------------------------------------- 受理
    @app.post("/async/v1/images/generations", status_code=202)
    async def create_generation(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """受理一次生成，**只回一个 task_id**。

        请求内**零上游往返**：建任务（计费动作）交给后台执行者，受节奏闸门约束。
        图片拉取同样在后台 —— 拉取失败会体现为任务 `failure`（附原因），
        而不是让受理请求随上游网络抖动。
        """
        svc: Service = request.app.state.service
        rec = svc.create(body.model_dump(), credential=credential,
                         sessionid=_bearer(request))

        # 叫醒协调器：不然这条任务要等到下一个 tick 才被发现（默认最多白等 1s）。
        # 纯优化 —— 唤醒丢了也只是慢一个 tick，"该派发谁"始终由库里的状态决定。
        request.app.state.coordinator.wake()

        # 202 + 一个 id。不返回状态/时间戳之类的附加信息 —— 调用方要的是"拿着它去轮询"。
        return JSONResponse(
            status_code=202, content={"task_id": rec.task_id},
            headers={"Location": f"/async/v1/images/generations/{rec.task_id}"})

    # ------------------------------------------------- 同步生成（创建+轮询合并）
    @app.post("/v1/images/generations")
    def create_generation_sync(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """**同步**出图：创建 + 轮询合并进一个请求，预算内直接给最终结果。

        ⚠️ 刻意用 `def` 而**不是** `async def`：本函数会阻塞等待至多
        `SYNC_MAX_WAIT` 秒（默认 300）—— 在 `async def` 里这样等会卡死整个
        事件循环（同 worker 的其它请求全部排队）。FastAPI 会把同步端点丢进
        线程池执行，阻塞因而只影响这一个请求。**改动签名前先想清这一点。**

        语义（与异步族共用同一判定，`view()` 仍是唯一出口）：
          · 预算内到终态 → 直接回终态体（成功 200；失败 200 + `status: failure`）；
          · 预算耗尽仍在跑 → **202 + `task_id`** + `Location` 指向异步查询端点
            —— 调用方无缝转异步轮询；任务不会丢、也不会被取消；
          · 没有推进者（协调器线程没在跑）→ **503**（`sync_unavailable`）快速
            失败，而不是让调用方白等 —— 那种部署形态下任务根本不会被推进。

        🔴 2026-10-10 用户口径 —— 终态失败里**两种直接抛 HTTP 错误**，
        不再回 200 + failure 体：
          · **空图**（上游报成功却零产物）→ **451**（`EmptyResultError`；
            2026-10-12 口径：空图多由上游内容审核所致，故与审核同类归 451）；
          · **内容审核拒绝** → **451**（`ContentPolicyError`，换 prompt/素材
            才有用，原样重试必再拒）。
        判据是落库的 `error.type`（`_fail` 存的就是 `to_error()["error"]`），
        不做 message 字符串匹配。错误体**只含** `message` / `type` / `code`：
        不回显模型名、能力 key、上游状态码与计数、上游 id（2026-10-12 口径：
        对客户端不暴露太多）—— 排查现场只进服务端日志，见
        `service.empty_result_error()`。
        其余失败（风控/限流/额度/上游故障…）
        保持 `view()` 的 200 + failure 体 —— 它们的语义是"任务完成了、
        结果是失败"，且部分带 Retry-After，转成裸 HTTP 码反而丢信息。

        内部链路与异步受理完全一致：落库 → 叫醒协调器 → 等库里的状态变化。
        等待期间**不发任何上游请求**（推进是协调器线程的职责，这里只读库）。
        """
        rec = _sync_generate(request.app.state.service,
                             request.app.state.coordinator,
                             request.app.state.settings,
                             body.model_dump(), credential=credential,
                             sessionid=_bearer(request))

        status_code, payload = view(rec)
        headers: dict[str, str] = {}
        if status_code == 202:
            # 预算耗尽但任务仍在跑：告诉调用方"去哪继续查"。
            # 202 是**降级**而不是失败 —— 任务没丢，转异步轮询即可。
            headers["Location"] = f"/async/v1/images/generations/{rec.task_id}"
        return JSONResponse(status_code=status_code, content=payload,
                            headers=headers)

    # ------------------------------------------------------------- 查询
    @app.get("/async/v1/images/generations/{task_id}")
    async def get_generation(
        request: Request,
        task_id: str,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """查任务。**需要 `Authorization: Bearer <key>`。**

        · 非终态 → **202** + `{task_id, status}`（调用方据此继续轮询）；
        · 成功 → **200** + `{data: [{url}], created, usage}`；
        · 失败 → **200** + `{task_id, status: "failure", error}`；
        · 不存在 **或不属于该 Key** → **404**（**本地拦，不发上游请求**；
          两者刻意合并成同一个 404 —— 区分开就等于告诉别人"这个 id 存在"）。

        🔴 **鉴权口径 2026-09-23 收紧**（此前是"`task_id` 即凭据、可不带 Key"）：
        现在**所有业务 GET 都要 Bearer**，且**按 Key 指纹校验属主**
        （内部映射见 `Service.get_for_credential`）。
        收紧的理由是可预测性：读/写/删三者的可见范围此前不一致
        （读能靠 id 分享、写与删不能），调用方很容易以为"有 id 就能读"，
        而 id 一旦泄漏 —— 例如贴进工单/聊天记录 —— 就等价于泄漏了产物。
        要分享产物请用产物的 `url`，不要分享任务的 `task_id`。
        """
        svc: Service = request.app.state.service
        rec = svc.get_for_credential(task_id, credential)
        status_code, payload = view(rec)
        return JSONResponse(status_code=status_code, content=payload)

    # ------------------------------------------------------------- 列表 / 删除
    @app.get("/async/v1/images/generations")
    async def list_generations(
        request: Request,
        limit: int = 50,
        credential: str = Depends(require_key),
    ) -> dict:
        """本 Key 名下的任务列表。**只列自己的** —— 只按服务过滤会把别人的任务列给你。"""
        return request.app.state.service.list_for_credential(
            credential, limit=max(1, min(limit, 200)))

    @app.delete("/async/v1/images/generations/{task_id}")
    async def delete_generation(
        request: Request,
        task_id: str,
        credential: str = Depends(require_key),
    ) -> dict:
        """删除任务。**未终态的任务会响亮失败（400）** —— 即梦没有取消端点，
        本地删掉只会让"还在跑并继续计费"变成看不见的事。"""
        return request.app.state.service.delete_for_credential(task_id, credential)

    # ------------------------------------------------- Ark 契约门面（方舟形态）
    # 🔴 2026-09-24 用户拍板：**视频唯一入口 = 方舟门面**。原生
    # `/async/v1/videos/*` 三路由已整体移除（受理/查询/删除），视频任务
    # 一律从 `/api/v3/contents/generations/tasks` 进（方舟 SDK 兼容）。
    @app.post("/api/v3/contents/generations/tasks", status_code=200)
    async def ark_create_task(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """创建视频生成任务 —— **火山方舟原生契约**（`model` + `content[]`）。

        请求/响应逐字段对齐方舟《创建视频生成任务》；底层翻译到即梦
        Seedance 链路（翻译规则与降级留痕见 `app/ark.py`）。
        鉴权沿用本服务 API Key（`Authorization: Bearer <key>`，形态同方舟）。
        """
        ark_body = body.model_dump()
        our, degradations = ark_translate(ark_body)
        svc: Service = request.app.state.service
        rec = svc.create(our, credential=credential,
                         sessionid=_bearer(request), video=True,
                         preset_degradations=degradations,
                         ark_model=str(ark_body.get("model") or ""))
        request.app.state.coordinator.wake()
        # 方舟创建响应只回 {"id": ...}
        return JSONResponse(status_code=200, content={"id": rec.task_id})

    @app.get("/api/v3/contents/generations/tasks/{task_id}")
    async def ark_get_task(
        request: Request,
        task_id: str,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """查询视频生成任务 —— **火山方舟原生契约**（请求/响应形态同方舟，
        但**鉴权按本服务的 Bearer 口径**：方舟 SDK 本来就会带
        `Authorization: Bearer <api_key>`，所以客户端无需改动）。"""
        svc: Service = request.app.state.service
        rec = svc.get_for_credential(task_id, credential)
        return JSONResponse(status_code=200, content=ark_view(rec))

    # ------------------------------------------------------- 发现面（给人/给 LLM）
    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        """根路径落地页（**给人看**）。纯 API 服务的裸 404 会被当成"打不开"。"""
        items = models.catalog()
        rows = "".join(
            f"<tr><td><code>{i['id']}</code></td><td>{i.get('title', '')}</td>"
            f"<td>{i.get('media', '')}</td><td>{'✅' if i.get('requires_prompt') else '—'}</td></tr>"
            for i in items)
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jimeng-service · 即梦生成出口</title>
<style>body{{margin:0;font:15px/1.7 -apple-system,"PingFang SC",sans-serif;background:#f6f7f9;color:#1a1a1a}}
.wrap{{max-width:760px;margin:0 auto;padding:44px 22px}}.card{{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:18px 20px;margin-bottom:16px}}
h1{{font-size:25px;margin:0 0 6px}}h2{{font-size:15px;color:#0b57d0;margin:0 0 8px}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}td,th{{border-bottom:1px solid #eef0f3;padding:5px 6px;text-align:left}}
code{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}}a{{color:#0b57d0;text-decoration:none}}
pre{{background:#0f172a;color:#e6edf3;padding:12px 14px;border-radius:8px;overflow:auto;font-size:12.5px}}
@media (prefers-color-scheme:dark){{body{{background:#0b0f14;color:#e6edf3}}.card{{background:#111823;border-color:#1e2836}}
h2{{color:#7cb0ff}}a{{color:#7cb0ff}}td,th{{border-color:#1e2836}}}}</style></head><body><div class="wrap">
<h1>jimeng-service</h1>
<p>即梦（jimeng web 端）生成出口 —— <strong>图片 + 视频</strong>，同步与异步任务两套形态。<br>
本页只是入口说明；服务本身是 <strong>API</strong>（并无网页 UI）。<strong>建任务即计费</strong>，详见说明书。</p>
<div class="card"><h2>怎么调</h2>
<pre>curl -sS &lt;本机地址&gt;/v1/images/generations \
  -H "Authorization: Bearer $JIMENG_API_KEY" -H 'Content-Type: application/json' \
  -d '{{"model":"jimeng-t2i","prompt":"一只橘猫在窗台上"}}'</pre>
<ul><li>异步任务：<code>POST /async/v1/images/generations</code> → <code>GET /async/v1/images/generations/{{task_id}}</code></li>
<li>字节风格兼容面：<code>/api/v3/contents/generations/tasks</code></li></ul></div>
<div class="card"><h2>探路（均免鉴权）</h2>
<ul><li><a href="/llms.txt">/llms.txt</a> —— 给 LLM/Agent 的说明书（能力表 / 计费 / 错误表）</li>
<li><a href="/v1/models">/v1/models</a> · <a href="/healthz">/healthz</a> · <a href="/readyz">/readyz</a></li></ul></div>
<div class="card"><h2>能力（{len(items)} 项，由注册表派生）</h2>
<table><tr><th>model</th><th>名称</th><th>媒介</th><th>需 prompt</th></tr>{rows}</table></div>
</div></body></html>""")

    @app.get("/llms.txt", include_in_schema=False)
    async def llms_txt(request: Request) -> PlainTextResponse:
        """给 LLM / Agent 的说明书（llmstxt.org 约定）——**免鉴权**，内容从注册表派生。"""
        settings: Settings = request.app.state.settings
        return PlainTextResponse(render_llms_txt(settings),
                                 media_type="text/markdown; charset=utf-8")

    # ------------------------------------------------------------- 模型
    @app.get("/v1/models")
    async def list_models() -> dict:
        """本服务对外宣告的能力清单（OpenAI 形态）。**刻意公开：不需要 Bearer。**

        🔴 **清单只有 `/v1/models` 这一条路径**（2026-09-23 起取消了 `/async/v1/models`）。
        它与 `POST /v1/images/generations`（同步生成，创建+轮询合并）同属
        `/v1` 的**同步语义**族；OpenAI 风格的客户端/插件按惯例探的就是
        `/v1/models`。

        🔴 **2026-09-23 起不鉴权（用户指令）**：它是**发现性端点**——客户端在
        配置 Key 之前先探"这服务有什么能力"是常规做法，而且内容只有能力的
        公开描述（没有任何任务、凭据、内部状态）。**其余端点仍一律要 Bearer**
        （含全部 GET）。例外名单的钉死处：`tests/test_api.py` 的
        `test_every_business_route_requires_a_bearer`（`_PUBLIC_PATHS`）。

        只列**没有已知缺陷**的能力；刻意缺席的（细节修复）不在这里 ——
        那就是"制造假能力"。
        文生图族额外带 `upstream_models`（面板名 → 上游模型 → **按模型**的实测价）。
        """
        return {"object": "list", "data": models.catalog()}

    # ------------------------------------- 兼容面：OpenAI 对话端点（**图片 chat**）
    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        credential: str = Depends(require_key),
    ) -> Response:
        """OpenAI **对话端点** —— 本服务用它承载**图片 chat**（2026-10-14 用户口径）。

        为什么有这个端点：OpenAI 风格客户端/网关（new-api、LobeChat、NextChat…）
        只认 `/v1/chat/completions`。"图片 chat" = 借 chat 协议拿生成结果，
        客户端才能把产物**直接渲染在对话流里**。

        🔴 **真出图、真计费**（2026-10-14 口径变更，此前是"只回说明、不建任务"
        的占位）：本端点**真的发起生成** —— 走与 `/v1/images/generations`
        **同一套**同步链路（`_sync_generate`：受理 → 推进 → 等终态），把产物以
        **Markdown 图片**（`![image](url)`）写进 `message.content`。
        ⚠️ **建任务即计费**：拿渠道"测试/连通性"按钮打本端点也会真的花钱。
        要零成本探活请用 `GET /healthz`。

        输入怎么来（两种 messages 形态都支持）：
          · 纯文本 `content: "画一只橘猫"` ⇒ `prompt`，无输入图 ⇒ 文生图；
          · 多模态 `content: [{type:text},{type:image_url}]` ⇒ 图片转**垫图**
            ⇒ 图生图（OpenAI 的 `image_url` 形态，`data:` URL 也收）。
        `model` 缺省时：带图 ⇒ `jimeng-i2i`，不带图 ⇒ `jimeng-t2i`
        —— chat 请求本来就不带出图族的那些参数，缺省时必须替它定一个，
        否则会撞上"未指定 model 且有多个能力可选"的 400。

        `stream`：`true` ⇒ 回标准 SSE（**先发 role 帧占位**，生成完成再发 content
        帧，`data: [DONE]` 收尾）；否则回整包 `chat.completion`。
        ⚠️ 流式下响应头一旦发出就改不动状态码 ⇒ 生成失败只能把错误**当正文**
        吐出去；非流式则照常抛 HTTP 错误（451/429/502…，与出图端点逐字一致）。

        ⚠️ 归属：它是**同步语义**端点（一问一答、返回前等到结果），与
        `/v1/models`、`POST /v1/images/generations` 同族 ⇒ 允许挂在 `/v1`；
        白名单见 `tests/test_api.py::_V1_SYNC_ENDPOINTS`。
        """
        body: Any = {}
        try:
            body = await request.json()
        except Exception:      # noqa: BLE001 —— 空体/非 JSON 也要应答（探针常发空体）
            body = {}
        if not isinstance(body, dict):
            body = {}

        model = str(body.get("model") or "").strip()
        prompt, images = _extract_chat_inputs(body)
        if not model:
            # 带图 ⇒ 图生图；不带图 ⇒ 文生图。各只有一个自然落点，
            # 不必让调用方为了"能跑通"塞一个它不关心的 model 名。
            model = "jimeng-i2i" if images else "jimeng-t2i"

        cid = f"chatcmpl-jimeng-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        our_body: dict[str, Any] = {"model": model, "prompt": prompt,
                                    "image": images}
        args = (request.app.state.service, request.app.state.coordinator,
                request.app.state.settings, our_body)
        kwargs = {"credential": credential, "sessionid": _bearer(request)}

        if body.get("stream"):
            # StreamingResponse 在线程池里迭代这个**同步**生成器 ⇒ `_sync_generate`
            # 的阻塞（最长 SYNC_MAX_WAIT）不卡事件循环（同 `def` 端点的道理）。
            def _produce() -> str:
                return _chat_text(_sync_generate(*args, **kwargs))

            return StreamingResponse(
                _chat_sse(cid, model, created, _produce),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache",
                         # 中间若过 nginx，别让它把这一小段 SSE 缓冲到请求结束
                         # 才吐（症状同样是"客户端收不到 chunk、显示空白"）。
                         "X-Accel-Buffering": "no"})

        # 非流式：阻塞跑在**线程池**里 —— 直接在 `async def` 里 await 一个阻塞调用
        # 会卡死整个事件循环，同 worker 的其它请求全部排队。
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        rec = await run_in_threadpool(_sync_generate, *args, **kwargs)
        return JSONResponse(status_code=200, content={
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": _chat_text(rec)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    # ------------------------------------------------------------- 运维
    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针。**零依赖、不触上游、不消耗积分。**

        🔴 **刻意不鉴权**：它是容器 HEALTHCHECK 与编排层判活的入口，
        要求带 Key 就等于"探针挂了服务才看起来挂"，会把故障定位引向错误方向。
        它也**既不上报 span、也不留日志**（见 `observability.PROBE_PATHS`）。
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针：**依赖项不通就报 503**，让编排层不要往这里导流量。

        查两件真正决定"能不能接活"的事：任务库可连、上游凭据已配。
        注意它比 `/healthz` 贵（会 ping 一次 DB），所以**不要**拿它当容器
        HEALTHCHECK —— 那个用 `/healthz`。

        与 `/healthz` 同样**刻意不鉴权**（编排层在凭据还没就位时就得能读它 ——
        "未配置上游凭据"正是它要报的 503 之一）。响应里**不含任何密钥**，
        DSN 是掩码后的。
        """
        svc: Service = request.app.state.service
        if not svc.store.ping():
            return JSONResponse(status_code=503, content={
                "status": "not_ready", "reason": "任务库（PostgreSQL）不可连",
                "dsn": svc.store.dsn,
            })
        if not svc.settings.upstream_configured:
            return JSONResponse(status_code=503, content={
                "status": "not_ready",
                "reason": "未配置 JIMENG_SESSIONID，受理会返回 503",
            })
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.get("/stats")
    async def stats(request: Request, credential: str = Depends(require_key)) -> dict:
        """运行状态（闸门统计 / 任务计数 / 能力表缓存 / 观测状态 / 存储健康）。

        🔴 **2026-09-23 起要 Bearer**：它不是对外契约端点，但内容是内部的
        （闸门计数、DSN 掩码、能力表缓存状态、协调器派发计数），
        此前**完全开放** —— 而公网入口是 80 端口，等于把这些挂在公网上。
        """
        svc: Service = request.app.state.service
        return {**svc.status(),
                "coordinator": request.app.state.coordinator.stats(),
                "store": svc.store.stats()}


app_factory = create_app  # 便于测试与 uvicorn 直接引用


if __name__ == "__main__":  # pragma: no cover
    # 变量名就叫 `settings`：静态门禁是按 `*.settings.X` 收集配置读取点的，
    # 叫 `_s` 会让 host/port 被误判成"没人读的死旋钮"。
    settings = Settings.from_env()
    uvicorn.run("app.main:create_app", factory=True,
                host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())
