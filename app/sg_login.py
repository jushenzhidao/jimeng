#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际站（Dreamina SG）账号登录 —— `Bearer 账号|密码` 的落地实现。

## 为什么需要（2026-10-19 用户口径："国际版即梦 增加鉴权方式 bearer 账号|密码"）

现状是 **sessionid 纯透传**：调用方要自己去浏览器里登录、把 cookie 里的
`sessionid` 抠出来当 Bearer。这有两个现实痛点：

  · sessionid 是 60 天有效的长串密文，换号/过期都要人手动重取；
  · 上层网关的凭据管理习惯是"账号密码"（可轮换、可托管），不是 cookie。

于是本模块把「账号密码」收敛成 sessionid —— 对下游（`bundle_for` 及以上）
**完全透明**：它只看到一把普通的 sessionid，站点探测/计费/属主校验原样复用。

## 抓包事实（2026-10-19，Playwright 全链路 + 纯 HTTP 复现，两轮实证）

登录端点**不在** `mweb-api-sg.capcut.com`（`/mweb/v1/login/*` 全部 404），
而在 **CapCut passport**：

    POST https://login-row.www.capcut.com/passport/web/email/login/
        ?aid=513641&account_sdk_source=web&sdk_version=2.1.10-tiktok&language=en
    body(form):
        mix_mode=1&email=<hex(x^5)>&password=<hex(x^5)>&fixed_mix_mode=1

七条实测结论，每条都决定了实现：

  1. 邮箱/密码是**逐字节 XOR 0x05 再 hex**（`'G'^5='B'`、`'o'^5='j'` 双向验过）；
     不是 base64、不是 md5 —— 猜错就是 `1009 密码不匹配`。
  2. **不需要** msToken / X-Bogus / X-Gnarly / verifyFp / webid：实测
     `webid=""`、`verifyFp` 缺省、零预热直接 POST 也成功拿到 sessionid
     ⇒ 省掉"先 GET 首页拿 cookie"那一步（少一次往返、少一个失败面）。
  3. 成功 = HTTP 200 + `Set-Cookie: sessionid=<...>; Domain=capcut.com;
     Max-Age=5184000`（60 天），body 里带 `user_id` / `name` / `avatar_url`。
  4. **失败也是 HTTP 200**：`{"message":"error","data":{"error_code":1009,
     "description":"Username or password doesn't match our records..."}}`
     ⇒ **绝不能靠 HTTP 状态码判成败**（这是本模块最容易写错的一条）。
  5. 有验证码挑战时 `data.captcha` 非空且**没有 sessionid**
     ⇒ 自动登录到此为止（不猜验证码），对外报风控。
  6. 登录端点有多个入口域名（`login-row.www` / `login.us` / `www`），
     按顺序回落 —— 抓包里实际走的是 `login-row.www`。
  7. 账号地区由服务端写回 `store-country-code`（本样本 `ph`），
     **不参与登录判定**；`region` 只出现在 mweb 的 query 上。

## 设计

| 关注点 | 做法 | 为什么 |
|---|---|---|
| 缓存键 | `账号小写 + sha256(密码)[:16]` | 内存里**不存明文密码**；同账号换密码 = 换 key |
| TTL | `min(SG_LOGIN_TTL, 上游 Max-Age)`，默认 12h | 上游给 60 天，但风控/改密会提前失效；12h 是"少登录"与"快自愈"的折中 |
| 并发 | 每账号一把 `asyncio.Lock` + 双检 | 网关会同一秒连发 4~5 条同凭据请求（项目记忆），不锁就是 5 次登录 |
| 失效 | `invalidate(sessionid)` 挂钩 1015 | 派发期拿到 login error ⇒ 删缓存，下个请求自动重登 |
| 日志 | 只打**脱敏邮箱**，永不打密码 | 密码一旦进日志就等于泄露 |

⚠️ **单进程假设**：`WORKERS=1`（见 gunicorn_conf.py），缓存是进程内字典。
多 worker 时各自登录一次 —— 正确性不受影响，只是多几次登录往返。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from .config import Settings
from .errors import (
    AuthError,
    RiskControlError,
    UpstreamUnavailableError,
)

log = logging.getLogger(__name__)

#: `账号|密码` 的分隔符。**只在"左半边像邮箱"时才认**，见 `parse_account_token`。
ACCOUNT_SEP = "|"

#: 登录端点候选（按抓包顺序回落）。三个域名是同一套 passport 的不同接入点。
LOGIN_HOSTS: tuple[str, ...] = (
    "https://login-row.www.capcut.com",
    "https://login.us.capcut.com",
    "https://www.capcut.com",
)
LOGIN_PATH = "/passport/web/email/login/"

#: 国际站 aid（与 `upstream/jimeng/site.py` 的 `SG_SITE.aid` 同值）。
#: 刻意不从 site.py import：登录走 capcut.com passport，与 mweb API 是两个体系，
#: 将来 passport 的 aid 变了对 mweb 侧没有影响（反之亦然）。
LOGIN_AID = "513641"

#: passport 前端 SDK 版本（抓包逐字）。写死而不是从页面读 —— 页面改版时
#: 这里失败会立刻暴露，而不是"某天登录悄悄开始 1009"。
SDK_VERSION = "2.1.10-tiktok"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

#: 凭据编码的 XOR 掩码（逐字节验证过，见模块 docstring 第 1 条）。
_XOR = 0x05

#: 默认缓存 TTL（秒）。上游 sessionid 的 Max-Age 是 60 天，取 12h 作默认。
DEFAULT_TTL = 12 * 3600

#: 账号密码不匹配的 passport 错误码（实测）。
_ERR_BAD_CREDENTIALS = 1009

_MAX_AGE_RE = re.compile(r"Max-Age=(\d+)", re.I)


# ---------------------------------------------------------------------------
# 解析 & 编码（纯函数，可单测）
# ---------------------------------------------------------------------------

def encode_credential(raw: str) -> str:
    """passport 的"加密" = **逐字节 XOR 0x05，再 hex 小写**。

    实测对照：
      `"GozmanTonelli914@outlook.com"` → `426a7f68646b516a6b6069696c3c3431456a7071696a6a6e2b666a68`
      `"aassdd"`                       → `646476766161`
    """
    return "".join(format(b ^ _XOR, "02x") for b in raw.encode())


def parse_account_token(raw: str | None) -> tuple[str, str] | None:
    """`账号|密码` ⇒ `(账号, 密码)`；其余形态（sessionid）⇒ `None`。

    判据（两条，缺一不可）：

      · 含 `|`；
      · **`|` 左半边像邮箱**（含 `@` 且无空白）。

    为什么判邮箱而不是"含 `|` 就算"：
      sessionid 是 base64url/hex，本来就不含 `|`，但**上游哪天换了编码**
      或调用方手滑，把一把有效 sessionid 当成"账号密码"送去登录的代价是
      **401 + 一次多余的登录尝试**（可能触发风控计数）。邮箱判据把误判面
      收窄到"既是合法 sessionid 又长得像邮箱"，实际上不可能。

    密码允许含 `|`（只按第一个 `|` 切）—— 谁规定密码不能有竖线。
    """
    if not raw or ACCOUNT_SEP not in raw:
        return None
    account, _, password = raw.partition(ACCOUNT_SEP)
    account = account.strip()
    if "@" not in account or any(c.isspace() for c in account):
        return None
    if not password:
        return None
    return account, password


def mask_account(account: str) -> str:
    """日志用脱敏：`abc***@outlook.com`。**永远不要打印密码**。"""
    name, _, domain = account.partition("@")
    if not domain:
        return account[:2] + "***"
    return f"{name[:3]}***@{domain}"


def cache_key(account: str, password: str) -> str:
    """缓存键：账号小写 + 密码的 sha256 前 16 位。

    ⚠️ 刻意**不存明文密码**在 key 里 —— 缓存字典可能被 `/stats` 或
    `repr()` 带出来（进程崩栈、调试器），明文密码不该出现在那些地方。
    """
    digest = hashlib.sha256(password.encode()).hexdigest()[:16]
    return f"{account.strip().lower()}|{digest}"


@dataclass(frozen=True)
class _Session:
    sessionid: str
    expires_at: float          # time.monotonic() 基准


class _TransportFailure(Exception):
    """登录端点**连不上**（不是"密码错"）。用于在候选域名间回落。"""


def _auth_failed(message: str) -> AuthError:
    """构造 401：**上游账号密码**失败，不是"本服务的 API Key 错"。

    `AuthError` 的默认 `err_code` 是 `invalid_api_key`（`require_key` 那条门用）。
    这里覆写成 `upstream_login_failed` —— 否则调用方会拿着一个"api_key 无效"
    的错误码去查自己的 Key，而真正错的是账号密码。
    """
    err = AuthError(message, param="authorization")
    err.err_code = "upstream_login_failed"
    return err



# ---------------------------------------------------------------------------
# 登录器
# ---------------------------------------------------------------------------

class SgAccountLogin:
    """`账号|密码` → sessionid（带缓存 / 并发去重 / 失效自愈）。

    生命周期挂在 `Service` 上（`Service.close()` 里 `aclose()`）；
    调用入口是 `Service.sessionid_for_account()`（异步）。
    """

    def __init__(self, settings: Settings) -> None:
        #: 与 `Service` 同名同义（`self.settings`）—— `test_no_dead_settings_knobs`
        #: 的扫描器按 `xxx.settings.<field>` 记录读取点，改名会变成"假配置"。
        self.settings = settings
        self._cache: "OrderedDict[str, _Session]" = OrderedDict()
        #: 🔴 **跨线程**互斥（`threading.Lock`，不是 asyncio）：
        #: 缓存会被两条线程碰 —— 请求线程（`sessionid_for`）与**协调器线程**
        #: （`_note_auth_failure` → `invalidate`）。用 asyncio.Lock 护不住，
        #: 且一边迭代 `_cache.items()` 一边插入会抛
        #: "dictionary changed size during iteration"。
        #: 锁内只做字典操作（无 IO），不会成为瓶颈。
        self._mu = threading.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._client_obj: httpx.AsyncClient | None = None
        self._max = max(1, int(settings.sg_login_cache_max))
        #: 观测计数（`/stats` 的 `login` 段）。
        self.hits = 0
        self.misses = 0
        self.failures = 0
        self.relogins = 0

    # ---------------------------------------------------------------- 公共

    @property
    def enabled(self) -> bool:
        return bool(self.settings.sg_login_enabled)

    async def sessionid_for(self, account: str, password: str) -> str:
        """取该账号的 sessionid（命中缓存则零网络往返）。

        🔴 并发去重的**必要性**：实测网关会同一秒连发 4~5 条同凭据请求
        （见项目记忆"受理去重"的背景）。不锁的话每条都发一次登录，
        既慢又给 passport 送风控素材。
        """
        if not self.enabled:
            raise AuthError(
                "本部署未启用「账号|密码」鉴权（SG_LOGIN_ENABLED=0）；"
                "请改用即梦 sessionid 作为 Bearer。",
                param="authorization")
        key = cache_key(account, password)
        sid = self._hit(key)
        if sid is not None:
            return sid
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            # 双检：等锁期间别人可能已经登录好了
            sid = self._hit(key)
            if sid is not None:
                return sid
            with self._mu:
                self.misses += 1
            try:
                sess = await self._login(account, password)
            except Exception:
                with self._mu:
                    self.failures += 1
                raise
            self._remember(key, sess)
            log.info("SG 账号登录成功 account=%s sid=%s… ttl=%.0fs",
                     mask_account(account), sess.sessionid[:8],
                     sess.expires_at - time.monotonic())
            return sess.sessionid

    def invalidate(self, sessionid: str | None) -> bool:
        """把某把 sessionid 从缓存踢掉（收到上游 1015 时调用）。

        返回是否真的删了 —— 让调用方能在日志里区分"过期的是缓存的号"
        还是"调用方自己带来的 sessionid"。
        """
        if not sessionid:
            return False
        with self._mu:
            dead = [k for k, v in self._cache.items() if v.sessionid == sessionid]
            for k in dead:
                self._cache.pop(k, None)
            if dead:
                self.relogins += 1
        return bool(dead)

    def stats(self) -> dict:
        now = time.monotonic()
        with self._mu:
            alive = sum(1 for v in self._cache.values() if v.expires_at > now)
            return {
                "enabled": self.enabled,
                "cached": alive,
                "hits": self.hits,
                "misses": self.misses,
                "failures": self.failures,
                "invalidations": self.relogins,
            }

    async def aclose(self) -> None:
        if self._client_obj is not None:
            try:
                await self._client_obj.aclose()
            except Exception:      # noqa: BLE001,S110 —— 关闭失败不影响退出
                pass
            self._client_obj = None

    # ---------------------------------------------------------------- 内部

    def _client(self) -> httpx.AsyncClient:
        """懒建共享 AsyncClient。

        🔴 `trust_env=False`：与 `JimengClient` 同口径 —— 不让宿主环境的
        `HTTP_PROXY` 悄悄接管（本机沙箱里那条代理连 localhost 都会走，
        线上则可能根本没有代理变量）。
        """
        if self._client_obj is None:
            self._client_obj = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                trust_env=False,
                follow_redirects=False,
                headers={
                    "user-agent": USER_AGENT,
                    "accept": "application/json, text/plain, */*",
                    "referer": "https://www.capcut.com/",
                },
            )
        return self._client_obj

    def _ttl(self) -> float:
        ttl = float(self.settings.sg_login_cache_ttl or 0)
        return ttl if ttl > 0 else float(DEFAULT_TTL)

    def _hit(self, key: str) -> str | None:
        """缓存命中就返回 sessionid（顺带 LRU 提升与 `hits` 计数）。

        ⚠️ 过期的条目**不在这里删** —— 留着让 `_login` 覆盖（少一次
        "删了又被写回"的抖动）；`stats()` 的 `cached` 只数未过期的。
        """
        now = time.monotonic()
        with self._mu:
            hit = self._cache.get(key)
            if hit is None or hit.expires_at <= now:
                return None
            self._cache.move_to_end(key)
            self.hits += 1
            return hit.sessionid

    def _remember(self, key: str, sess: _Session) -> None:
        with self._mu:
            self._cache[key] = sess
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
            # 锁表也做个上限：账号轮换频繁的部署不该无限长
            if len(self._locks) > self._max * 4:
                stale = [k for k in self._locks if k not in self._cache]
                for k in stale[: len(stale) // 2]:
                    self._locks.pop(k, None)

    async def _login(self, account: str, password: str) -> _Session:
        """按候选域名顺序登录；只有**网络层**失败才换下一个域名。

        业务失败（密码错 / 风控）**不回落** —— 换个域名再试一次密码只会
        给 passport 多送一次失败计数。
        """
        last: Exception | None = None
        #: 每个域名为什么失败 —— **逐条带进最终错误**。
        #: 为什么值得占这几行：passport 改版（路径/编码/域名）时的第一现场
        #: 只有这里。把 "login-row: HTTP 404（端点可能已下线/改版）" 直接写进
        #: 错误体，改版当天就能定位到该改 `LOGIN_PATH` 还是 `_XOR`，
        #: 而不是对着一个笼统的"不可达"从头复现一遍。
        failures: list[str] = []
        for host in LOGIN_HOSTS:
            try:
                return await self._post(host, account, password)
            except _TransportFailure as e:
                last = e
                failures.append(str(e))
                continue
        # 计数统一由 `sessionid_for` 的 except 记（这里再记会双计）
        raise UpstreamUnavailableError(
            "登录国际站失败：passport 端点均不可达（"
            f"{mask_account(account)}）：" + "；".join(failures)
            + "。若是上游改版，请更新 app/sg_login.py 的 LOGIN_PATH / 编码方式；"
              "临时可改用 sessionid 作 Bearer。",
            upstream="jimeng") from last

    async def _post(self, host: str, account: str, password: str) -> _Session:
        url = host + LOGIN_PATH
        params = {
            "aid": LOGIN_AID,
            "account_sdk_source": "web",
            "sdk_version": SDK_VERSION,
            "language": "en",
            # 抓包里还有 verifyFp / webid / browser_* —— 实测**全部可省**
            # （见模块 docstring 第 2 条），少一个参数就少一个失败面。
        }
        form = {
            "mix_mode": "1",
            "email": encode_credential(account),
            "password": encode_credential(password),
            "fixed_mix_mode": "1",
        }
        try:
            r = await self._client().post(url, params=params, data=form)
        except httpx.HTTPError as e:
            raise _TransportFailure(f"{host}: {type(e).__name__}") from e

        # 🔴 先看 cookie：成功响应里 sessionid 一定在 Set-Cookie 上，
        # body 只是附加信息。**不要**先解析 body 再判 status —— 失败也是 200。
        sid = r.cookies.get("sessionid")
        if sid:
            return _Session(sessionid=sid, expires_at=time.monotonic() + self._ttl())

        if r.status_code in (404, 405, 410):
            raise _TransportFailure(
                f"{host}: HTTP {r.status_code}（端点可能已下线/改版）")
        if r.status_code >= 500:
            raise _TransportFailure(f"{host}: HTTP {r.status_code}")

        try:
            payload = r.json()
        except ValueError:
            raise _TransportFailure(
                f"{host}: 非 JSON 响应（HTTP {r.status_code}）") from None
        data = payload.get("data") or {}
        code = data.get("error_code")
        desc = (data.get("description") or payload.get("message") or "").strip()

        # 验证码挑战：**没有** sessionid + captcha 非空 ⇒ 自动登录到此为止
        if data.get("captcha"):
            raise RiskControlError(
                f"登录国际站需要人机验证（captcha），无法自动完成"
                f"（账号 {mask_account(account)}）；"
                "请改用浏览器登录后以 sessionid 作 Bearer。",
                upstream="jimeng")

        if code == _ERR_BAD_CREDENTIALS:
            raise _auth_failed(
                f"上游账号或密码不正确（账号 {mask_account(account)}）；"
                "账号密码形态是 `Authorization: Bearer <邮箱>|<密码>`。")

        raise _auth_failed(
            f"登录国际站被拒绝（账号 {mask_account(account)}，"
            f"error_code={code}）{('：' + desc) if desc else ''}；"
            "请核对账号密码，或改用 sessionid 作 Bearer。")


__all__ = [
    "ACCOUNT_SEP", "LOGIN_HOSTS", "LOGIN_PATH", "DEFAULT_TTL",
    "encode_credential", "parse_account_token", "mask_account", "cache_key",
    "SgAccountLogin",
]
