# -*- coding: utf-8 -*-
"""`Bearer 账号|密码`（国际站登录）的门禁（2026-10-19）。

钉住四类最容易塌的事：

1. **编码/解析 = 抓包事实**：`encode_credential` 对齐真实抓包的两条向量
   （email / password），`parse_account_token` 不把 sessionid 误判成账号；
2. **成败判据**：passport **失败也回 HTTP 200**，只能看 `Set-Cookie: sessionid`
   与 `data.error_code` —— 用错判据会让"密码错"静默变成"任务失败"；
3. **缓存与并发**：同账号连续调用零网络往返；5 条并发只登录 1 次；
   1015 后 `invalidate` 能让下一次重新登录；
4. **错误语义**：密码错 ⇒ 401 `AuthError`；captcha ⇒ 429 `RiskControlError`；
   端点不可达 ⇒ 502 `UpstreamUnavailableError`（且会在域名间回落）。

零真实上游：全部走 `httpx.MockTransport`。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.errors import AuthError, RiskControlError, UpstreamUnavailableError
from app.sg_login import (
    LOGIN_HOSTS,
    SgAccountLogin,
    cache_key,
    encode_credential,
    mask_account,
    parse_account_token,
)

EMAIL = "GozmanTonelli914@outlook.com"
PWD = "aassdd"


# ---------------------------------------------------------------------------
# ① 编码 / 解析 —— 抓包向量
# ---------------------------------------------------------------------------

def test_encode_credential_matches_the_capture():
    """两条都是 2026-10-19 真实登录请求里的原值。改 XOR 掩码会立刻红。"""
    assert encode_credential(EMAIL) == (
        "426a7f68646b516a6b6069696c3c3431456a7071696a6a6e2b666a68")
    assert encode_credential(PWD) == "646476766161"
    assert encode_credential("") == ""


@pytest.mark.parametrize("raw, expect", [
    (f"{EMAIL}|{PWD}", (EMAIL, PWD)),
    ("a@b.c|x", ("a@b.c", "x")),
    # 密码允许含 |（只切第一个）
    ("a@b.c|p|w|d", ("a@b.c", "p|w|d")),
    # 账号两侧空白被吃掉
    (" a@b.c |x", ("a@b.c", "x")),
])
def test_parse_account_token_positive(raw, expect):
    assert parse_account_token(raw) == expect


@pytest.mark.parametrize("raw", [
    None,
    "",
    "eyJhbGciOiJIUzI1NiJ9.payload.sig",      # 典型 sessionid
    "a1b2c3d4e5f6|",                          # 空密码
    "not-an-email|pwd",                       # 左边不像邮箱
    "has space@x.com|pwd",                    # 账号含空白
    "|pwd",
])
def test_parse_account_token_negative(raw):
    """**sessionid 绝不能被误判**：误判 = 一次白跑的登录 + 401。"""
    assert parse_account_token(raw) is None


def test_mask_account_never_leaks_password():
    assert mask_account(EMAIL) == "Goz***@outlook.com"
    assert PWD not in mask_account(EMAIL)


def test_cache_key_hides_password():
    k = cache_key(EMAIL, PWD)
    assert PWD not in k
    assert k.startswith(EMAIL.lower() + "|")
    # 换密码 = 换 key（否则改密后一直用旧凭据）
    assert cache_key(EMAIL, PWD + "x") != k
    # 账号大小写不敏感
    assert cache_key(EMAIL.upper(), PWD) == k


# ---------------------------------------------------------------------------
# ② 登录器行为
# ---------------------------------------------------------------------------

def _ok_response(sid: str = "sid-from-passport") -> httpx.Response:
    return httpx.Response(
        200,
        headers=[
            ("set-cookie",
             f"sessionid={sid}; Path=/; Domain=capcut.com; Max-Age=5184000; HttpOnly"),
            ("content-type", "application/json"),
        ],
        json={"data": {"user_id": 1, "name": "u"}, "message": ""},
    )


def _bad_pwd_response() -> httpx.Response:
    body = {"data": {"captcha": "", "desc_url": "",
                     "description": "Username or password doesn't match our records. Try again.",
                     "error_code": 1009},
            "message": "error"}
    return httpx.Response(200, json=body)


def _captcha_response() -> httpx.Response:
    body = {"data": {"captcha": "verify_xxx", "error_code": 1031}, "message": "error"}
    return httpx.Response(200, json=body)


def _login_with(handler, *, settings: Settings | None = None) -> SgAccountLogin:
    """把登录器的 httpx 客户端换成 MockTransport（不打真网络）。"""
    login = SgAccountLogin(settings or Settings())
    login._client_obj = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False)
    return login


def test_login_success_and_cache_hit():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.host)
        # body 必须是 passport 的 form 且已编码
        body = req.content.decode()
        assert "mix_mode=1" in body and "fixed_mix_mode=1" in body
        assert encode_credential(EMAIL) in body
        assert PWD not in body          # 明文绝不上路
        return _ok_response()

    login = _login_with(handler)

    async def run():
        sid1 = await login.sessionid_for(EMAIL, PWD)
        sid2 = await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return sid1, sid2

    sid1, sid2 = asyncio.run(run())
    assert sid1 == sid2 == "sid-from-passport"
    assert len(calls) == 1, "第二次应命中缓存，不许再登录"
    assert calls[0].startswith("login-row")


def test_bad_credentials_maps_to_401():
    login = _login_with(lambda req: _bad_pwd_response())

    async def run():
        with pytest.raises(AuthError) as ei:
            await login.sessionid_for(EMAIL, "wrong")
        await login.aclose()
        return ei.value

    err = asyncio.run(run())
    assert err.status_code == 401
    # 不能沿用 AuthError 默认的 `invalid_api_key`（那会让调用方去查自己的 Key）
    assert err.err_code == "upstream_login_failed"
    assert "密码" in err.message
    assert "wrong" not in err.message          # 不回声密码


def test_captcha_maps_to_risk_control():
    login = _login_with(lambda req: _captcha_response())

    async def run():
        with pytest.raises(RiskControlError) as ei:
            await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return ei.value

    err = asyncio.run(run())
    assert err.status_code == 429
    assert "captcha" in err.message.lower()


def test_host_fallback_on_transport_error():
    """第一个域名网络故障 ⇒ 换下一个；**业务失败不回落**。"""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host.startswith("login-row"):
            raise httpx.ConnectError("boom", request=req)
        return _ok_response("sid-fallback")

    login = _login_with(handler)

    async def run():
        sid = await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return sid

    assert asyncio.run(run()) == "sid-fallback"
    assert seen[0].startswith("login-row")
    assert seen[1] == "login.us.capcut.com"
    assert len(LOGIN_HOSTS) == 3


def test_all_hosts_down_maps_to_502():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)

    login = _login_with(handler)

    async def run():
        with pytest.raises(UpstreamUnavailableError) as ei:
            await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return ei.value

    err = asyncio.run(run())
    assert err.status_code == 502
    assert "不可达" in err.message


def test_endpoint_404_mentions_upstream_revision():
    """passport 改版最常见的形态：路径 404。

    错误体必须**自带线索**（哪个域名 + 404 + "改版"），否则当天只能对着
    一句"端点不可达"从头复现 —— 用户口径是"改版时更新脚本就行"，
    前提就是失败现场足够说明该改哪一行。
    """
    login = _login_with(lambda req: httpx.Response(404, text="Not Found"))

    async def run():
        with pytest.raises(UpstreamUnavailableError) as ei:
            await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return ei.value

    err = asyncio.run(run())
    assert err.status_code == 502
    assert "404" in err.message
    assert "改版" in err.message
    assert "app/sg_login.py" in err.message     # 指向要改的文件
    assert err.message.count("404") == 3        # 三个域名都试过、都写进错误里


def test_concurrent_calls_login_once():
    """实测网关会同一秒连发 4~5 条同凭据请求 —— 不锁就是 5 次登录。"""
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return _ok_response(f"sid-{n}")

    login = _login_with(handler)

    async def run():
        sids = await asyncio.gather(*[login.sessionid_for(EMAIL, PWD)
                                      for _ in range(5)])
        await login.aclose()
        return sids

    sids = asyncio.run(run())
    assert n == 1, f"并发登录了 {n} 次"
    assert len(set(sids)) == 1


def test_invalidate_forces_relogin():
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return _ok_response(f"sid-{n}")

    login = _login_with(handler)

    async def run():
        sid1 = await login.sessionid_for(EMAIL, PWD)
        assert login.invalidate(sid1) is True
        sid2 = await login.sessionid_for(EMAIL, PWD)
        assert login.invalidate("not-mine") is False   # 调用方自带 sid：空操作
        await login.aclose()
        return sid1, sid2

    sid1, sid2 = asyncio.run(run())
    assert (sid1, sid2) == ("sid-1", "sid-2")
    assert n == 2


def test_disabled_login_rejects_with_401():
    login = _login_with(lambda req: _ok_response(),
                        settings=Settings(sg_login_enabled=False))

    async def run():
        with pytest.raises(AuthError) as ei:
            await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return ei.value

    assert "SG_LOGIN_ENABLED" in asyncio.run(run()).message


def test_ttl_expiry_triggers_relogin(monkeypatch):
    """TTL 到期后必须重新登录（否则缓存会把失效凭据钉住）。"""
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return _ok_response(f"sid-{n}")

    login = _login_with(handler, settings=Settings(sg_login_cache_ttl=60))
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.sg_login.time.monotonic", lambda: clock["t"])

    async def run():
        a = await login.sessionid_for(EMAIL, PWD)
        clock["t"] += 61
        b = await login.sessionid_for(EMAIL, PWD)
        await login.aclose()
        return a, b

    assert asyncio.run(run()) == ("sid-1", "sid-2")
    assert n == 2


def test_invalidate_is_thread_safe():
    """协调器线程会调 `invalidate`，请求线程同时在写缓存。

    不加 `threading.Lock` 时这里会抛
    `RuntimeError: dictionary changed size during iteration` ——
    而这在生产里就是"偶发 500"，最难查的那种。
    """
    import threading

    login = _login_with(lambda req: _ok_response("sid-x"))
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn() -> None:
        try:
            while not stop.is_set():
                login.invalidate("sid-x")
                login.stats()
        except BaseException as e:
            errors.append(e)

    th = threading.Thread(target=churn, daemon=True)
    th.start()
    try:
        async def run():
            for _ in range(40):
                await login.sessionid_for(EMAIL, PWD)
            await login.aclose()

        asyncio.run(run())
    finally:
        stop.set()
        th.join(timeout=3)
    assert not errors, f"跨线程访问缓存出错：{errors[0]!r}"


def test_stats_shape():
    login = _login_with(lambda req: _ok_response())

    async def run():
        await login.sessionid_for(EMAIL, PWD)
        await login.aclose()

    asyncio.run(run())
    st = login.stats()
    assert st["enabled"] is True
    assert st["cached"] == 1
    assert st["misses"] == 1
    assert st["failures"] == 0


# ---------------------------------------------------------------------------
# ③ 与 Service / main 的接线
# ---------------------------------------------------------------------------

def test_settings_defaults():
    s = Settings.from_env()
    assert s.sg_login_enabled is True
    assert s.sg_login_cache_ttl == 0          # 0 = 模块默认 12h
    assert s.sg_login_cache_max >= 1


def test_service_marks_site_after_login():
    """登录出来的 sid 必须**直接标 sg 归属**，否则每个新 sid 都要白探国内站。

    用 `object.__new__` 而不是 `service` fixture：本用例只验"标记"这一个动作，
    不该为了它去连一次 PG（登录器本身零 DB 依赖）。
    """
    from app.service import Service
    from app.upstream.jimeng.site import SG_SITE

    svc = object.__new__(Service)
    svc.settings = Settings()
    svc._cred_secret = "test-secret"
    svc._site_cache = {}
    svc.sg_login = _login_with(lambda req: _ok_response("sid-login-1"))

    async def run():
        sid = await svc.sessionid_for_account(EMAIL, PWD)
        await svc.sg_login.aclose()       # 只收登录器（未走 __init__ 的 Service 没有 vod/uploader）
        return sid

    sid = asyncio.run(run())
    assert sid == "sid-login-1"
    assert svc._site_cache[sid].name == SG_SITE.name
